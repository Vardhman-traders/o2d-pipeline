# ruff: noqa: E501
"""Guarded delete of the data that was exported (orders, payments, expenses, purchases, VT Decor and their stored photos).

OFF BY DEFAULT: these routes exist only when the encrypted export is configured AND VAULT_DELETE_ENABLED=1. Nothing happens in one step:

  1. PREVIEW (a "plan"): password + authenticator code. Checks every condition below and records exactly what would be deleted.
  2. EXECUTE: a plan older than VAULT_DELETE_COOLING_HOURS (default 24) and not older than 7 days, the typed phrase, a tick that
     backups still hold the data, and password + a fresh authenticator code again.

Conditions (all must hold, and are checked again inside the delete itself while the tables are locked):
  - the last export batch was CONFIRMED (an admin said it was uploaded and checked), and covered orders, money and decor
  - NOTHING in those tables changed, was added or was removed since that confirmed batch (compared by the same keyed fingerprints)
  - photos/documents: every stored file was part of the confirmed export, with none missing or damaged (only if their files are to be deleted)
  - no table outside this scope holds a row that points at data that would be deleted
  - only tables the export covers can ever be deleted (enforced by a test)

The delete is ONE transaction: if anything differs from the plan, any table fails, or any table outside the scope would change its row
count, everything is rolled back and nothing is deleted. Afterwards the space is reclaimed (VACUUM FULL) and the stored files are removed.
Attendance, salary, logins, permissions, settings, tasks, and the customer and reference lists are never touched.
Database backups and storage versioning keep the old data until they expire: that is outside this app and is stated in the preview.
"""
import logging
import os
import secrets
import time
from datetime import timedelta

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel, ConfigDict, Field

from . import db, vault_export, vault_sync
from .admin import ADMIN, audit

log = logging.getLogger("vault_delete")

PHRASE = "DELETE ALL ORDERS AND MONEY DATA"
PLAN_MAX_AGE = timedelta(days=7)
SCOPE_GROUPS = list(vault_export.SELECTABLE_GROUPS)
# Tables that legitimately change during a delete (bookkeeping and the audit trail); every OTHER table outside the scope must not.
BOOKKEEPING = {"admin_audit_log", "vault_batch", "vault_row_state", "vault_delete_plan", "vault_delete_files"}
PAGE_DIR = os.path.dirname(__file__)


def scope_tables() -> list:
    """The tables whose rows are deleted. Always a subset of what the export covers (a test enforces it)."""
    names = [t for g in SCOPE_GROUPS for t in vault_export.GROUPS[g] if t != "v_orders_archive"]   # the view is derived data
    return names + ["file_attachment"]                                                            # only its challan / purchase / decor rows


def enabled() -> bool:
    return vault_export._env("VAULT_DELETE_ENABLED") == "1" and vault_export.enabled()


def cooling_hours() -> float:
    try:
        return max(0.0, float(vault_export._env("VAULT_DELETE_COOLING_HOURS") or 24))
    except ValueError:
        return 24.0


def _where(table: str):
    return vault_export.row_filter(table, SCOPE_GROUPS)


def _count(cur, table: str) -> int:
    where, params = _where(table)
    q = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
    cur.execute(q + sql.SQL(" WHERE " + where) if where else q, params)
    return cur.fetchone()["n"]


def delete_order(cur) -> list:
    """Children before parents, from the real foreign keys among the scope tables."""
    scope = set(scope_tables())
    cur.execute("SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent FROM pg_constraint WHERE contype = 'f'")
    below: dict = {t: set() for t in scope}
    for r in cur.fetchall():
        if r["child"] in scope and r["parent"] in scope and r["child"] != r["parent"]:
            below[r["parent"]].add(r["child"])
    order, remaining = [], set(scope)
    while remaining:
        ready = sorted(t for t in remaining if not (below[t] & remaining))
        if not ready:
            raise RuntimeError("the tables to delete reference each other in a circle")
        order += ready
        remaining -= set(ready)
    return order


def outside_references(cur) -> list:
    """Rows in tables OUTSIDE the scope that point at rows that would be deleted. Empty is required."""
    scope = set(scope_tables())
    cur.execute("""SELECT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent, c.confdeltype AS action,
                          cardinality(c.conkey) AS ncols,
                          (SELECT attname FROM pg_attribute WHERE attrelid = c.conrelid AND attnum = c.conkey[1]) AS child_col,
                          (SELECT attname FROM pg_attribute WHERE attrelid = c.confrelid AND attnum = c.confkey[1]) AS parent_col
                   FROM pg_constraint c WHERE c.contype = 'f'""")
    found = []
    for r in cur.fetchall():
        if r["parent"] in scope and r["child"] not in scope:
            if r["ncols"] != 1:
                found.append({"table": r["child"], "reason": "a multi-column link into the data to delete: not supported, so refused"})
                continue
            where, params = _where(r["parent"])
            inner = sql.SQL("SELECT {} FROM {}").format(sql.Identifier(r["parent_col"]), sql.Identifier(r["parent"]))
            if where:
                inner = inner + sql.SQL(" WHERE " + where)
            q = sql.SQL("SELECT count(*) AS n FROM {} WHERE {} IN (").format(sql.Identifier(r["child"]), sql.Identifier(r["child_col"])) + inner + sql.SQL(")")
            cur.execute(q, params)
            n = cur.fetchone()["n"]
            if n:
                found.append({"table": r["child"], "column": r["child_col"], "rows": n,
                              "reason": f"{n} row(s) in {r['child']} point at data that would be deleted"})
    return found


def other_counts(cur) -> dict:
    scope = set(scope_tables())
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1")
    out = {}
    for r in cur.fetchall():
        t = r["table_name"]
        if t not in scope and t not in BOOKKEEPING:
            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(t)))
            out[t] = cur.fetchone()["n"]
    return out


def current_fingerprints(conn, groups, key: bytes) -> dict:
    """The keyed fingerprint of every exported row as it is right now, in the SAME way the export computed them."""
    out: dict = {}
    for table, only in vault_export.tables_for(groups).items():
        with conn.cursor() as cur:
            cols = vault_export._columns(cur, table, only)
            names = [c for c, _ in cols]
            key_idx = [names.index(c) for c in vault_export.key_columns(cur, table, names)]
        where, params = vault_export.row_filter(table, groups)
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(sql.Identifier(c) for c in names), sql.Identifier(table))
        if where:
            query = query + sql.SQL(" WHERE " + where)
        fps: dict = {}
        with conn.cursor(name="vdel_" + secrets.token_hex(4)) as stream:
            stream.itersize = 5000
            stream.execute(query, params)
            for row in stream:
                vals = tuple(vault_export.to_sqlite(v) for v in row)
                fps[vault_export.row_key_text(vals[i] for i in key_idx)] = vault_export.fingerprint(key, table, vals)
        out[table] = fps
    return out


def changes_since(base: dict, now: dict) -> dict:
    out = {}
    for table in set(base) | set(now):
        b, n = base.get(table, {}), now.get(table, {})
        new = sum(1 for k in n if k not in b)
        gone = sum(1 for k in b if k not in n)
        changed = sum(1 for k, fp in n.items() if k in b and b[k] != fp)
        if new or gone or changed:
            out[table] = {"new": new, "removed": gone, "changed": changed}
    return out


def files_coverage(cur) -> list:
    """Reasons the stored files were NOT fully exported (empty = every file of the confirmed export chain was included and intact)."""
    cur.execute("SELECT batch_no, kind, summary FROM vault_batch WHERE status = 'confirmed' ORDER BY batch_no")
    chain = cur.fetchall()
    start = max((i for i, b in enumerate(chain) if b["kind"] == "full"), default=0)
    problems = []
    for b in chain[start:]:
        f = (b["summary"] or {}).get("files")
        if f is None:
            problems.append(f"photos and documents were not part of batch {b['batch_no']}")
        elif f.get("missing") or f.get("checksum_differs"):
            problems.append(f"batch {b['batch_no']}: {f.get('missing', 0)} file(s) were missing and {f.get('checksum_differs', 0)} damaged when exported")
    return problems


def object_keys(cur) -> list:
    keys: set = set()
    for t in ("order_attachments", "file_attachment"):
        where, params = _where(t)
        q = sql.SQL("SELECT object_key FROM {} WHERE object_key IS NOT NULL").format(sql.Identifier(t))
        cur.execute(q + sql.SQL(" AND " + where) if where else q, params)
        keys.update(r["object_key"] for r in cur.fetchall())
    return sorted(keys)


def evaluate(conn, want_files: bool) -> dict:
    """Everything the preview shows and the delete re-checks. `blockers` must be empty to proceed."""
    from . import attachments
    key = vault_export._env("VAULT_FINGERPRINT_KEY").encode()
    out: dict = {"blockers": [], "warnings": []}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        base = vault_sync.confirmed(cur)
        out["confirmed_batch"] = base["batch_no"] if base else None
        if not base:
            out["blockers"].append("No export batch has been confirmed yet. Export, upload, validate, then confirm the batch first.")
        else:
            groups = sorted((base["summary"] or {}).get("groups", []))
            if groups != sorted(SCOPE_GROUPS):
                out["blockers"].append(f"The confirmed batch covered {groups}; the delete needs a confirmed export of all of {sorted(SCOPE_GROUPS)}.")
            else:
                changes = changes_since(vault_sync.load_state(cur, base["batch_no"]), current_fingerprints(conn, SCOPE_GROUPS, key))
                scope = set(scope_tables())
                hard = {t: c for t, c in changes.items() if t in scope}
                soft = {t: c for t, c in changes.items() if t not in scope and t != "v_orders_archive"}
                if hard:
                    out["blockers"].append("Data changed after the confirmed export: " + "; ".join(
                        f"{t} ({c['new']} new, {c['changed']} changed, {c['removed']} removed)" for t, c in sorted(hard.items()))
                        + ". Export again, upload, validate and confirm the new batch.")
                if soft:
                    out["warnings"].append("Reference lists (kept, not deleted) changed after the export: " + ", ".join(sorted(soft))
                                           + ". Export again if you want the destination copy to have the latest names.")
        counts = {t: _count(cur, t) for t in scope_tables()}
        out["counts"] = counts
        refs = outside_references(cur)
        if refs:
            out["blockers"].append("Other data depends on what would be deleted: " + "; ".join(r["reason"] for r in refs))
        files = object_keys(cur)
        out["files_total"] = len(files)
        will_delete_files = want_files and bool(files)
        if will_delete_files:
            if not attachments.configured():
                out["warnings"].append("Photo storage is not configured on this server, so the stored files cannot be deleted from here; only the database rows go.")
                will_delete_files = False
            else:
                gaps = files_coverage(cur)
                if gaps:
                    out["blockers"].append("Stored files were not all exported: " + "; ".join(gaps) + ". Export with photos included first, or choose not to delete the files.")
        out["delete_files"] = will_delete_files
        out["other_tables"] = other_counts(cur)
    return out


# ------------------------------------------------------------------ routes
router = APIRouter(prefix="/admin/vault/delete", tags=["vault-delete"])


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=200)
    totp: str = Field(max_length=12)
    delete_files: bool = True


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=200)
    totp: str = Field(max_length=12)
    plan_id: str = Field(max_length=60)
    phrase: str = Field(max_length=100)
    backups_understood: bool = False


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=200)
    totp: str = Field(max_length=12)
    plan_id: str = Field(max_length=60)


def _gate(request: Request, admin: dict, body) -> None:
    vault_export._gate(request, admin, vault_export.GateIn(password=body.password, totp=body.totp))


def _json(data, status: int = 200):
    resp = JSONResponse(data, status_code=status)
    return vault_export._no_store(resp)


def _asset(name: str, media: str):
    with open(os.path.join(PAGE_DIR, name), "rb") as f:
        return vault_export._no_store(Response(f.read(), media_type=media))


@router.get("/page", response_class=HTMLResponse)
def page():
    """Only the form (a typed address cannot carry a login header). It holds no data; every action behind it is gated."""
    with open(os.path.join(PAGE_DIR, "vault_delete_page.html"), encoding="utf-8") as f:
        return vault_export._no_store(HTMLResponse(f.read()))


@router.get("/page.js")
def page_script():
    return _asset("vault_delete_page.js", "text/javascript")


@router.get("/page.css")
def page_style():
    return _asset("vault_export_page.css", "text/css")


@router.get("/status")
def status(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT plan_id, created_at, created_by, status, confirmed_batch, executed_at FROM vault_delete_plan ORDER BY created_at DESC LIMIT 10")
        plans = [{**r, "created_at": r["created_at"].isoformat(), "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None}
                 for r in cur.fetchall()]
    return _json({"cooling_hours": cooling_hours(), "phrase_required": PHRASE, "plans": plans})


@router.post("/plan")
def make_plan(request: Request, body: PlanIn, admin=Depends(ADMIN)):
    """Preview: nothing is deleted. Records what WOULD be deleted and why it may be blocked."""
    _gate(request, admin, body)
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        ev = evaluate(conn, body.delete_files)
    finally:
        conn.close()
    plan_id = secrets.token_urlsafe(18)
    with db.cursor() as cur:
        cur.execute("INSERT INTO vault_delete_plan (plan_id, created_by, status, confirmed_batch, counts, files_total, delete_files, blockers) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (plan_id, admin["username"], "blocked" if ev["blockers"] else "planned", ev["confirmed_batch"],
                     Json(ev["counts"]), ev["files_total"], ev["delete_files"], Json(ev["blockers"])))
        audit(cur, admin, "vault_delete_plan", plan_id, {"blocked": bool(ev["blockers"]), "counts": ev["counts"], "files": ev["files_total"]})
        cur.execute("SELECT now() + (%s * interval '1 hour') AS t", (cooling_hours(),))
        earliest = cur.fetchone()["t"]
    return _json({"plan_id": plan_id, "can_execute": not ev["blockers"], "blockers": ev["blockers"], "warnings": ev["warnings"],
                  "confirmed_batch": ev["confirmed_batch"], "will_delete": ev["counts"], "stored_files": ev["files_total"],
                  "delete_stored_files": ev["delete_files"], "will_not_touch": ev["other_tables"],
                  "earliest_execute_at": earliest.isoformat(), "phrase_required": PHRASE,
                  "after_it": ["Backups of the database and any storage versioning keep the old data until they expire: purge them too for a complete removal.",
                               "Screens, reports and alerts on this server that read the deleted data will show nothing; switch off anything that depends on them first."]})


def _fail(plan_id: str, error: str) -> None:
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE vault_delete_plan SET status = 'failed', result = %s WHERE plan_id = %s AND status = 'planned'",
                        (Json({"error": error}), plan_id))
    except Exception:
        log.exception("could not record the failure of plan %s", plan_id)


@router.post("/execute")
def execute(request: Request, body: ExecuteIn, admin=Depends(ADMIN)):
    _gate(request, admin, body)
    if body.phrase != PHRASE:
        raise HTTPException(422, "The confirmation phrase does not match.")
    if not body.backups_understood:
        raise HTTPException(422, "Tick that you understand backups still hold the data.")
    with db.cursor() as cur:
        cur.execute("SELECT *, now() - created_at AS age FROM vault_delete_plan WHERE plan_id = %s", (body.plan_id,))
        plan = cur.fetchone()
    if not plan:
        raise HTTPException(404, "No such preview. Make a new one.")
    if plan["status"] != "planned":
        raise HTTPException(409, f"This preview cannot be used (it is {plan['status']}). Make a new one.")
    if plan["age"] > PLAN_MAX_AGE:
        raise HTTPException(409, "This preview is too old. Make a new one.")
    if plan["age"] < timedelta(hours=cooling_hours()):
        wait = timedelta(hours=cooling_hours()) - plan["age"]
        raise HTTPException(409, f"Cooling-off: this preview can be used in {int(wait.total_seconds() // 60) + 1} minute(s).")

    started = time.time()
    order = []
    deleted: dict = {}
    keys: list = []
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL lock_timeout = '20s'")
            order = delete_order(cur)
            for t in order:     # stop all writes to these tables for the few seconds the check and the delete take
                cur.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(t)))
            before_other = other_counts(cur)
            ev = evaluate(conn, plan["delete_files"])
            if ev["blockers"]:
                raise _Refused("Conditions no longer hold: " + " ".join(ev["blockers"]))
            if ev["counts"] != plan["counts"]:
                raise _Refused("The data changed since the preview (the row counts differ). Make a new preview.")
            keys = object_keys(cur) if plan["delete_files"] else []
            for k in keys:
                cur.execute("INSERT INTO vault_delete_files (plan_id, object_key) VALUES (%s, %s)", (plan["plan_id"], k))
            for t in order:
                where, params = _where(t)
                q = sql.SQL("DELETE FROM {}").format(sql.Identifier(t))
                cur.execute(q + sql.SQL(" WHERE " + where) if where else q, params)
                deleted[t] = cur.rowcount
                if deleted[t] != plan["counts"][t]:
                    raise _Refused(f"{t}: deleted {deleted[t]} rows but the preview said {plan['counts'][t]}")
            for t, n in other_counts(cur).items():
                if before_other.get(t) != n:
                    raise _Refused(f"Safety stop: {t} would have changed from {before_other.get(t)} to {n} rows")
            cur.execute("UPDATE vault_delete_plan SET status = 'executed', executed_at = now(), result = %s WHERE plan_id = %s",
                        (Json({"deleted": deleted, "files_queued": len(keys)}), plan["plan_id"]))
            audit(cur, admin, "vault_delete_executed", plan["plan_id"], {"deleted": deleted, "files_queued": len(keys), "confirmed_batch": plan["confirmed_batch"]})
        conn.commit()
    except _Refused as e:
        conn.rollback()
        _fail(plan["plan_id"], str(e))          # a preview that no longer matches reality is never reusable
        raise HTTPException(409, str(e))
    except Exception as e:
        conn.rollback()
        log.exception("vault delete failed and was rolled back")
        _fail(plan["plan_id"], type(e).__name__)
        raise HTTPException(500, "The delete failed and was rolled back: nothing was deleted.")
    finally:
        conn.close()

    result = {"deleted": deleted, "rows_total": sum(deleted.values()), "stored_files": file_cleanup(plan["plan_id"], admin), "space": reclaim(order)}
    result["seconds"] = round(time.time() - started, 1)
    return _json(result)


class _Refused(Exception):
    pass


def reclaim(tables) -> dict:
    """VACUUM FULL cannot run inside a transaction, so it follows the commit. Returns table sizes now (bytes)."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    out: dict = {}
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(sql.SQL("VACUUM (FULL) {}").format(sql.Identifier(t)))
            cur.execute("SELECT sum(pg_total_relation_size(c)) FROM unnest(%s::regclass[]) c", (list(tables),))
            out["scope_tables_bytes_after"] = int((cur.fetchone() or (0,))[0] or 0)
    except Exception:
        log.exception("could not reclaim space; the delete itself is complete")
        out["note"] = "The delete is complete, but reclaiming the disk space failed; run it again later."
    finally:
        conn.close()
    return out


def file_cleanup(plan_id: str, admin: dict) -> dict:
    """Remove the stored photo/document files listed for this plan. Failures stay 'pending' so they can be retried."""
    from . import attachments
    with db.cursor() as cur:
        cur.execute("SELECT object_key FROM vault_delete_files WHERE plan_id = %s AND status <> 'deleted' ORDER BY object_key", (plan_id,))
        todo = [r["object_key"] for r in cur.fetchall()]
    if not todo:
        return {"deleted": 0, "failed": 0}
    done = failed = 0
    if not attachments.configured():
        return {"deleted": 0, "failed": len(todo), "note": "photo storage is not configured here; retry from a server that has it"}
    for i in range(0, len(todo), 1000):
        chunk = todo[i:i + 1000]
        try:
            resp = attachments.storage().delete_objects(Bucket=attachments._bucket(), Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": False})
            bad = {e["Key"] for e in resp.get("Errors", [])}
        except Exception:
            log.exception("could not delete a batch of stored files")
            bad = set(chunk)
        good = [k for k in chunk if k not in bad]
        done += len(good)
        failed += len(bad)
        with db.cursor() as cur:
            if good:
                cur.execute("UPDATE vault_delete_files SET status = 'deleted' WHERE plan_id = %s AND object_key = ANY(%s)", (plan_id, good))
            if bad:
                cur.execute("UPDATE vault_delete_files SET status = 'failed' WHERE plan_id = %s AND object_key = ANY(%s)", (plan_id, sorted(bad)))
    with db.cursor() as cur:
        audit(cur, admin, "vault_delete_files", plan_id, {"deleted": done, "failed": failed})
    return {"deleted": done, "failed": failed}


@router.post("/retry-files")
def retry_files(request: Request, body: RetryIn, admin=Depends(ADMIN)):
    _gate(request, admin, body)
    with db.cursor() as cur:
        cur.execute("SELECT status FROM vault_delete_plan WHERE plan_id = %s", (body.plan_id,))
        row = cur.fetchone()
    if not row or row["status"] != "executed":
        raise HTTPException(404, "No executed delete with that id.")
    return _json(file_cleanup(body.plan_id, admin))

