# ruff: noqa: E501
"""Encrypted data export for moving the O2D / money / VT Decor data to a separate destination.

OFF BY DEFAULT: none of these routes exist unless VAULT_EXPORT_ENABLED=1 AND the three secrets below are set. Turn it on for the export
window only, then remove the flag again.

Gate (all four must pass on every request - nothing is remembered between requests):
  1. a signed-in admin (the normal login)
  2. the export password   (VAULT_EXPORT_PASSWORD_HASH, a bcrypt hash kept in the environment, not in the database)
  3. a current 6-digit authenticator code (VAULT_EXPORT_TOTP_SECRET, base32), each code usable once
  4. failures are counted and lock the gate (same rule as logins: 5 wrong attempts, 15 minutes)

What leaves this server is encrypted to VAULT_PUBLIC_KEY (base64 of a 65-byte P-256 point). The private key is never here, so this app
cannot read its own exports. The plain data exists only in memory while the file is built; no plain file is ever written to disk.

Which tables go (and which never do) is CLASSIFICATION below. A table that is not listed there fails the test suite, so a new migration
cannot leak a new table, or silently drop one, without someone deciding.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import struct
import threading
import time
from datetime import UTC, date, datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from psycopg2 import sql
from pydantic import BaseModel, ConfigDict, Field

from . import auth, db, vault_crypto, vault_sync
from .admin import ADMIN, audit

log = logging.getLogger("vault_export")

PAYLOAD_FORMAT = "vault-payload-1"
EXPORT_DIR = Path(os.environ.get("VAULT_EXPORT_DIR", "/tmp/vault-export"))
FILE_TTL_SECONDS = 30 * 60

# ------------------------------------------------------------------ what is exported
# group -> tables. "reference" is always included (names the other groups point at).
# A value of None means every column; a list restricts the columns (secrets are never listed).
GROUPS: dict[str, dict[str, list[str] | None]] = {
    "orders": {
        "fact_orders": None, "order_event": None, "order_attachments": None, "order_doc_read": None, "order_reminder": None,
        "fact_challan_upload": None, "doc_gap_void": None, "daily_delivery_report": None, "v_orders_archive": None,
    },
    "money": {
        "fact_payment_txn": None, "payment_push": None, "fact_expense_entry": None, "fact_purchase_entry": None,
        "fact_extra_work": None,
    },
    "decor": {
        "fact_decor_expense": None, "fact_decor_item": None, "fact_decor_payment": None, "fact_decor_photo": None,
        "fact_decor_quotation": None, "fact_decor_revision": None, "decor_fixed_expense": None,
    },
    "reference": {
        "dim_party": None, "dim_company": None, "dim_person": None, "dim_order_channel": None, "dim_payment_mode": None,
        "dim_payment_status": None, "dim_txn_type": None, "dim_expense_account": None, "dim_expense_category": None,
        "dim_purchase_site": None, "dim_delivery_status": None, "dim_submission_type": None, "dim_date": None,
        "dim_decor_client": None, "dim_decor_expense_type": None, "dim_decor_product_type": None, "dim_decor_stage": None,
        "file_attachment": None,
        # only who-did-it names; the password hash and every other login column stay behind
        "dim_user": ["user_key", "username", "display_name", "role"],
    },
}

# Never exported. Every table in the database must be in GROUPS or here (tests enforce it).
EXCLUDED = {
    # attendance and salary
    "att_auto_run", "att_employee_face", "att_employee_location", "att_holiday", "att_punch_check", "att_wa_log",
    "dim_att_employee", "dim_att_location", "fact_att_advance", "fact_attendance",
    # logins, permissions, settings, internals
    "access_request", "admin_audit_log", "app_config", "app_links", "app_notification", "import_batch", "notify_log",
    "page_access", "role_field_permissions", "role_view_access", "schema_migrations", "user_page_access", "whatsapp_alert",
    "vault_batch", "vault_row_state", "vault_delete_plan", "vault_delete_files",
    "daily_report_run",
    # tasks and checklists (operations, not money or customers)
    "dim_checklist", "dim_checklist_category", "fact_checklist_occurrence", "fact_task", "fact_weekly_plan",
}
SELECTABLE_GROUPS = ("orders", "money", "decor")
# file_attachment is shared with attendance photos, face photos (biometrics) and delegation proofs. Only these modules may travel, and
# only for the groups that were asked for. order_attachments (dispatch / receiving photos) belongs to the orders group.
ATTACHMENT_MODULES = {"orders": ["challan"], "money": ["purchase"], "decor": ["decor"]}
ATTACHMENT_TABLES = ("order_attachments", "file_attachment")
SECRET_COLUMNS = {"password_hash", "token", "secret", "api_key"}


def tables_for(groups) -> dict[str, list[str] | None]:
    """table -> column list (or None) for the chosen groups plus the reference tables."""
    chosen: dict[str, list[str] | None] = {}
    for g in [*groups, "reference"]:
        chosen.update(GROUPS[g])
    return chosen


def row_filter(table: str, groups):
    """(SQL fragment, params) limiting which rows of a table may leave, or (None, [])."""
    if table == "file_attachment":
        return "module = ANY(%s)", [sorted({m for g in groups for m in ATTACHMENT_MODULES.get(g, [])})]
    return None, []


# ------------------------------------------------------------------ settings
def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def public_key() -> bytes:
    return base64.b64decode(_env("VAULT_PUBLIC_KEY"), validate=True)


def problems() -> list:
    """Everything that stops the export from running. Empty list = ready."""
    out = []
    if _env("VAULT_EXPORT_ENABLED") != "1":
        out.append("VAULT_EXPORT_ENABLED is not 1")
    for name in ("VAULT_PUBLIC_KEY", "VAULT_EXPORT_PASSWORD_HASH", "VAULT_EXPORT_TOTP_SECRET"):
        if not _env(name):
            out.append(f"{name} is not set")
    if len(_env("VAULT_FINGERPRINT_KEY")) < 32:
        out.append("VAULT_FINGERPRINT_KEY must be a secret of at least 32 characters")
    if _env("VAULT_PUBLIC_KEY"):
        try:
            vault_crypto.load_public_key(public_key())
        except Exception:
            out.append("VAULT_PUBLIC_KEY is not a valid 65-byte P-256 public key in base64")
    if _env("VAULT_EXPORT_TOTP_SECRET"):
        try:
            base64.b32decode(_env("VAULT_EXPORT_TOTP_SECRET").replace(" ", "").upper() + "=" * (-len(_env("VAULT_EXPORT_TOTP_SECRET").replace(" ", "")) % 8))
        except Exception:
            out.append("VAULT_EXPORT_TOTP_SECRET is not valid base32")
    return out


def enabled() -> bool:
    """True only when switched on AND fully configured. A half-configured switch logs why and stays off."""
    if _env("VAULT_EXPORT_ENABLED") != "1":
        return False
    p = problems()
    if p:
        log.warning("vault export is switched on but NOT mounted: %s", "; ".join(p))
    return not p


# ------------------------------------------------------------------ authenticator code (RFC 6238, SHA-1, 30 s, 6 digits)
_last_counter = 0
_totp_lock = threading.Lock()


def _totp(secret_b32: str, counter: int) -> str:
    s = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 15
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_ok(code: str, now: float | None = None) -> bool:
    """Accepts the current code and one step either side; a code that was accepted once is never accepted again."""
    global _last_counter
    code = (code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return False
    counter = int((now if now is not None else time.time()) // 30)
    with _totp_lock:
        for c in (counter - 1, counter, counter + 1):
            if c > _last_counter and hmac.compare_digest(_totp(_env("VAULT_EXPORT_TOTP_SECRET"), c), code):
                _last_counter = c
                return True
    return False


def reset_replay_guard() -> None:  # for tests
    global _last_counter
    _last_counter = 0


# ------------------------------------------------------------------ Postgres value -> SQLite value (also the checksum encoding)
def to_sqlite(v):
    if v is None or isinstance(v, (int, float, str, bytes)):
        return int(v) if isinstance(v, bool) else v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return (v.astimezone(UTC) if v.tzinfo else v).isoformat()
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)
    return str(v)


def _cell(v) -> bytes:
    if v is None:
        return b"N"
    if isinstance(v, int):
        return b"I" + str(v).encode() + b";"
    if isinstance(v, float):
        return b"F" + repr(v).encode() + b";"
    b = v if isinstance(v, bytes) else v.encode("utf-8")
    return (b"B" if isinstance(v, bytes) else b"S") + str(len(b)).encode() + b":" + b


def row_digest(values) -> int:
    h = hashlib.sha256()
    for v in values:
        h.update(_cell(v))
    return int.from_bytes(h.digest(), "big")


MOD = 1 << 256


def _sqlite_type(pg_type: str) -> str:
    if pg_type in ("integer", "bigint", "smallint", "boolean"):
        return "INTEGER"
    if pg_type in ("real", "double precision"):
        return "REAL"
    if pg_type == "bytea":
        return "BLOB"
    return "TEXT"  # numeric stays TEXT on purpose: exact money digits, no float rounding


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------ build the plain SQLite image (in memory only)
def _columns(cur, table: str, only):
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position", (table,))
    cols = [(r[0], r[1]) for r in cur.fetchall()]
    if only is not None:
        by = dict(cols)
        missing = [c for c in only if c not in by]
        if missing:
            raise RuntimeError(f"{table}: expected columns are missing: {missing}")
        cols = [(c, by[c]) for c in only]
    bad = [c for c, _ in cols if c in SECRET_COLUMNS]
    if bad:
        raise RuntimeError(f"{table}: refusing to export secret columns {bad}")
    return cols


# A view has no primary key; this says which column identifies its rows.
KEY_OVERRIDES = {"v_orders_archive": ["order_key"]}


def key_columns(cur, table: str, selected) -> list:
    """The columns that identify a row of this table (its primary key). A table without one must be listed in KEY_OVERRIDES."""
    if table in KEY_OVERRIDES:
        cols = KEY_OVERRIDES[table]
    else:
        cur.execute("SELECT kcu.column_name FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                    "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
                    "WHERE tc.table_schema = 'public' AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY' "
                    "ORDER BY kcu.ordinal_position", (table,))
        cols = [r[0] for r in cur.fetchall()]
    if not cols or not set(cols) <= set(selected):
        raise RuntimeError(f"{table}: no usable row key; add it to KEY_OVERRIDES")
    return cols


def fingerprint(key: bytes, table: str, vals) -> bytes:
    """Keyed fingerprint of one row (16 bytes). Without the key it says nothing about the row; with it, any change shows."""
    h = hmac.new(key, table.encode() + bytes(1), hashlib.sha256)
    for v in vals:
        h.update(_cell(v))
    return h.digest()[:16]


def row_key_text(vals_of_key) -> str:
    return json.dumps(list(vals_of_key), separators=(",", ":"))


def _add_attachments(lite, tables, writer, progress, only_new=None, batch_no=0) -> int:
    """Encrypt every listed photo/document into the packs and record where it went in _vault_files (inside the encrypted database)."""
    rows = []
    for t in ATTACHMENT_TABLES:
        if t in tables:
            for r in lite.execute(f"SELECT attachment_key, object_key, sha256 FROM {_q(t)} WHERE object_key IS NOT NULL").fetchall():
                if only_new is None or row_key_text([r[0]]) in only_new.get(t, ()):   # a delta carries only files the other side has not got
                    rows.append((t, *r))
    for i, (t, key, obj, sha) in enumerate(rows):
        e = writer.add(obj, sha)
        lite.execute("INSERT INTO _vault_files VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (e.get("file_id"), t, key, batch_no, e.get("pack"), e.get("offset"), e.get("length"), e.get("size"), e.get("sha256"), e["status"]))
        if progress:
            progress(i + 1, len(rows))
    return len(rows)


def build_snapshot(database_url: str, groups, pack_writer=None, progress=None, *, fp_key: bytes | None = None,
                   base_state: dict | None = None, batch_no: int = 0, base_batch: int = 0) -> tuple[bytes, dict]:
    """One consistent read-only snapshot of the chosen tables -> (SQLite database bytes, report). Nothing touches the disk.
    With base_state (fingerprints as of the last confirmed batch) the image is a DELTA: only new or changed rows, plus tombstones
    for rows that no longer exist. Either way the report holds the full-table row counts, checksums and sums AFTER the batch, so the
    other side can verify what it ends up with, and "state" (the fingerprints to remember) when fp_key is given.
    With a pack_writer, the photos and documents listed in the snapshot are then encrypted into packs (after the database
    transaction is closed, so a slow download never holds a snapshot open on the live database)."""
    tables = tables_for(groups)
    pg = psycopg2.connect(database_url, connect_timeout=15)
    try:
        pg.set_session(isolation_level="REPEATABLE READ", readonly=True)
        lite = sqlite3.connect(":memory:")
        lite.executescript(
            "CREATE TABLE _vault_meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE _vault_tables (name TEXT PRIMARY KEY, row_count INTEGER, digest TEXT, sums TEXT, row_filter TEXT, key_columns TEXT, delta_rows INTEGER, tombstones INTEGER);"
            "CREATE TABLE _vault_tombstones (table_name TEXT, row_key TEXT);"
            "CREATE TABLE _vault_files (file_id TEXT, source TEXT, attachment_key INTEGER, batch_no INTEGER, pack INTEGER, offset INTEGER, length INTEGER, size INTEGER, sha256 TEXT, status TEXT);"
            "CREATE TABLE _vault_columns (table_name TEXT, ordinal INTEGER, column_name TEXT, pg_type TEXT);"
            "CREATE TABLE _vault_packs (batch_no INTEGER, pack INTEGER, sha256 TEXT, size INTEGER);"      # which uploaded pack file is which, by checksum
            "CREATE TABLE _vault_manifest (key TEXT PRIMARY KEY, value TEXT);")                            # what the other side shows (see vault_manifest)
        report: dict[str, dict] = {}
        delta = base_state is not None
        if delta and fp_key is None:
            raise ValueError("a delta needs the fingerprint key")
        new_state: dict[str, dict[str, bytes]] = {}
        new_keys: dict[str, set] = {}
        with pg.cursor() as cur:
            cur.execute("SELECT now()")
            snapshot_at = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SELECT count(*), max(filename) FROM schema_migrations")
            n_migrations, last_migration = cur.fetchone()  # type: ignore[misc]
            for table, only in tables.items():
                cols = _columns(cur, table, only)
                if not cols:
                    raise RuntimeError(f"{table}: not found in the database")
                lite.execute(f"CREATE TABLE {_q(table)} (" + ", ".join(f"{_q(c)} {_sqlite_type(t)}" for c, t in cols) + ")")
                for i, (c, t) in enumerate(cols):
                    lite.execute("INSERT INTO _vault_columns VALUES (?,?,?,?)", (table, i, c, t))
                numeric_idx = [i for i, (_, t) in enumerate(cols) if t == "numeric"]
                sums = {cols[i][0]: Decimal(0) for i in numeric_idx}
                names = [c for c, _ in cols]
                kcols = key_columns(cur, table, names)
                key_idx = [names.index(c) for c in kcols]
                prev = (base_state or {}).get(table, {})
                fps: dict[str, bytes] = {}
                fresh: set = set()
                digest, count, sent = 0, 0, 0
                insert = f"INSERT INTO {_q(table)} VALUES ({', '.join('?' * len(cols))})"
                with pg.cursor(name="vault_" + secrets.token_hex(4)) as stream:
                    stream.itersize = 5000
                    where, params = row_filter(table, groups)
                    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols), sql.Identifier(table))
                    if where:
                        query = query + sql.SQL(" WHERE " + where)
                    stream.execute(query, params)
                    while True:
                        batch = stream.fetchmany(5000)
                        if not batch:
                            break
                        out = []
                        for row in batch:
                            for i in numeric_idx:
                                if row[i] is not None:
                                    sums[cols[i][0]] += row[i]
                            vals = tuple(to_sqlite(v) for v in row)
                            digest = (digest + row_digest(vals)) % MOD
                            count += 1
                            if fp_key is not None:
                                rk = row_key_text(vals[i] for i in key_idx)
                                fp = fingerprint(fp_key, table, vals)
                                fps[rk] = fp
                                if delta:
                                    old = prev.get(rk)
                                    if old == fp:
                                        continue          # the other side already has this exact row
                                    if old is None:
                                        fresh.add(rk)
                            out.append(vals)
                        lite.executemany(insert, out)
                        sent += len(out)
                gone = [k for k in prev if k not in fps] if delta else []
                lite.executemany("INSERT INTO _vault_tombstones VALUES (?,?)", [(table, k) for k in gone])
                if fp_key is not None:
                    new_state[table] = fps
                    new_keys[table] = fresh
                shown_filter = (where.replace("%s", json.dumps(params[0])) if where else None)
                lite.execute("INSERT INTO _vault_tables VALUES (?,?,?,?,?,?,?,?)",
                             (table, count, f"{digest:064x}", json.dumps({k: str(v) for k, v in sums.items()}), shown_filter,
                              json.dumps(kcols), sent, len(gone)))
                report[table] = {"rows": count, "digest": f"{digest:064x}", "sums": {k: str(v) for k, v in sums.items()}, "filter": shown_filter,
                                 "delta_rows": sent, "tombstones": len(gone)}
        pg.rollback()   # the snapshot is copied; release it before any slow photo downloads
        pg.close()
        packs: list = []
        attach_meta = None
        if pack_writer is not None:
            total = _add_attachments(lite, tables, pack_writer, progress, new_keys if delta else None, batch_no)
            packs = pack_writer.finish()
            lite.executemany("INSERT INTO _vault_packs VALUES (?,?,?,?)", [(batch_no, i, p["sha256"], p["size"]) for i, p in enumerate(packs)])
            attach_meta = {**pack_writer.counts, "listed": total, "packs": len(packs)}
        meta: dict = {
            "format": PAYLOAD_FORMAT, "kind": "delta" if delta else "full", "batch_no": str(batch_no), "base_batch": str(base_batch),
            "groups": list(groups),
            "snapshot_at": snapshot_at.astimezone(UTC).isoformat(), "migrations_applied": str(n_migrations),
            "last_migration": last_migration, "tables": str(len(tables)),
            "rows_total": str(sum(r["rows"] for r in report.values())),
        }
        if attach_meta is not None:
            meta["attachments"] = attach_meta
        lite.executemany("INSERT INTO _vault_meta VALUES (?,?)", [(k, json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in meta.items()])
        from . import vault_manifest
        lite.execute("INSERT INTO _vault_manifest VALUES ('dashboards', ?)", (vault_manifest.build_json(),))
        lite.commit()
        data = lite.serialize()
        lite.close()
        return data, {"meta": meta, "tables": report, "packs": packs, "state": new_state if fp_key is not None else None}
    finally:
        pg.close()


# ------------------------------------------------------------------ jobs (one at a time; the encrypted file waits briefly on disk)
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _sweep() -> None:
    now = time.time()
    for jid, j in list(_jobs.items()):
        if now - j["created"] > FILE_TTL_SECONDS and j["state"] != "running":
            _discard(jid)


def _discard(jid: str) -> None:
    j = _jobs.pop(jid, None)
    for path in ((j or {}).get("parts") or {}).values():
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", path)


def _part_sent(jid: str, part: str) -> None:
    """One encrypted file has gone out: delete it from the server; when all have, forget the job."""
    j = _jobs.get(jid)
    if not j:
        return
    path = j["parts"].pop(part, None)
    if path:
        Path(path).unlink(missing_ok=True)
    if not j["parts"]:
        _jobs.pop(jid, None)


def _run(jid: str, admin: dict, groups, with_files: bool = False, mode: str = "auto") -> None:
    j = _jobs[jid]
    batch_no = None
    try:
        key = _env("VAULT_FINGERPRINT_KEY").encode()
        with db.cursor() as cur:
            vault_sync.supersede_unconfirmed(cur)
            base = vault_sync.confirmed(cur)
            # a delta only makes sense on the same set of groups as the batch it builds on; anything else starts a new full baseline
            use_delta = bool(base) and mode == "auto" and sorted(((base or {}).get("summary") or {}).get("groups", [])) == sorted(groups)
            base_batch = int((base or {}).get("batch_no", 0)) if use_delta else 0
            base_state = vault_sync.load_state(cur, base_batch) if use_delta else None
            batch_no = vault_sync.new_batch(cur, "delta" if use_delta else "full", base_batch)
        j.update(batch_no=batch_no, kind="delta" if use_delta else "full", base_batch=base_batch)
        writer = None
        if with_files:
            from . import attachments, vault_attachments
            if attachments.configured():
                writer = vault_attachments.PackWriter(EXPORT_DIR, public_key())
            else:
                j["files_note"] = "skipped: photo storage is not configured on this server"

        def progress(done, total):
            j["progress"] = {"files_done": done, "files_total": total}
        plain, report = build_snapshot(os.environ["DATABASE_URL"], groups, writer, progress,
                                       fp_key=key, base_state=base_state, batch_no=batch_no, base_batch=base_batch)
        blob, export_id = vault_crypto.encrypt_export(plain, public_key())
        del plain
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / (secrets.token_hex(12) + ".bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        parts = {"db": str(path)}
        packs = []
        for n, pk in enumerate(report["packs"]):
            parts[f"pack{n}"] = pk["path"]
            packs.append({"n": n, "size": pk["size"], "sha256": pk["sha256"]})
        changes = {t: {"changed": r["delta_rows"], "removed": r["tombstones"]} for t, r in report["tables"].items()
                   if r["delta_rows"] or r["tombstones"]}
        sha = hashlib.sha256(blob).hexdigest()
        summary = {"groups": list(groups), "kind": j["kind"], "base_batch": base_batch, "bytes": len(blob), "sha256": sha,
                   "rows": {t: r["rows"] for t, r in report["tables"].items()}, "changes": changes, "packs": packs,
                   "files": report["meta"].get("attachments")}
        with db.cursor() as cur:   # fingerprints, batch status and audit commit together
            vault_sync.save_state(cur, batch_no, report["state"])
            vault_sync.mark_pending(cur, batch_no, export_id.hex(), summary)
            audit(cur, admin, "vault_export_ready", jid, {"batch_no": batch_no, "export_id": export_id.hex(), **summary})
        j.update(parts=parts, size=len(blob), export_id=export_id.hex(), sha256=sha, summary=summary["rows"], changes=changes,
                 snapshot_at=report["meta"]["snapshot_at"], packs=packs, files=report["meta"].get("attachments"), state="ready")
    except Exception as e:  # never put the exception text on screen: it could carry data
        log.exception("vault export failed")
        if batch_no is not None:
            try:
                with db.cursor() as cur:
                    vault_sync.mark_failed(cur, batch_no)
            except Exception:
                log.exception("could not mark batch %s failed", batch_no)
        j.update(state="failed", error=type(e).__name__)


# ------------------------------------------------------------------ routes
router = APIRouter(prefix="/admin/vault", tags=["vault-export"])


class GateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=200)
    totp: str = Field(max_length=12)
    groups: list[str] = Field(default_factory=lambda: list(SELECTABLE_GROUPS))
    attachments: bool = True   # also export the photos and documents (each encrypted on its own)
    mode: Literal["auto", "full"] = "auto"   # auto = only what changed since the last confirmed batch (or everything if there is none)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _gate(request: Request, admin: dict, body: GateIn) -> list:
    who = "vault-export:" + admin["username"]
    ip = request.client.host if request.client else None
    auth.check_not_locked(who, ip)
    ok = auth.verify_password(body.password, _env("VAULT_EXPORT_PASSWORD_HASH"))
    ok = totp_ok(body.totp) and ok  # always evaluate both so timing does not say which one was wrong
    if not ok:
        auth.record_failure(who, ip)
        with db.cursor() as cur:
            audit(cur, admin, "vault_export_denied", None, {"ip": ip})
        raise HTTPException(403, "Gate check failed")
    auth.clear_failures(who, ip)
    groups = list(dict.fromkeys(body.groups))
    if not groups or any(g not in SELECTABLE_GROUPS for g in groups):
        raise HTTPException(422, f"groups must be some of {list(SELECTABLE_GROUPS)}")
    return groups


@router.get("/page", response_class=HTMLResponse)
def page():
    """Just the form. A browser cannot send a login header when an address is typed, so this must open without one; it holds no data,
    and everything it does goes through the gated routes below. (It still only exists when the export is switched on.)"""
    return _no_store(HTMLResponse((Path(__file__).parent / "vault_export_page.html").read_text(encoding="utf-8")))


@router.get("/page.js")
def page_script():
    """The app's content security policy forbids inline script and style, so the page's code and look are separate files, same address."""
    return _no_store(Response((Path(__file__).parent / "vault_export_page.js").read_bytes(), media_type="text/javascript"))


@router.get("/page.css")
def page_style():
    return _no_store(Response((Path(__file__).parent / "vault_export_page.css").read_bytes(), media_type="text/css"))


@router.post("/plan")
def plan(request: Request, body: GateIn, admin=Depends(ADMIN)):
    """Dry run: exactly which tables would go and how many rows each - and which never go. Nothing is read out."""
    groups = _gate(request, admin, body)
    wanted = tables_for(groups)
    with db.cursor() as cur:
        counts = {}
        for t in wanted:
            where, params = row_filter(t, groups)
            q = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(t))
            cur.execute(q + sql.SQL(" WHERE " + where) if where else q, params)
            counts[t] = cur.fetchone()["n"]
        audit(cur, admin, "vault_export_plan", None, {"groups": groups})
    return _no_store_json({"groups": groups, "tables": counts, "never_exported": sorted(EXCLUDED)})


def _no_store_json(data):
    from fastapi.responses import JSONResponse
    return _no_store(JSONResponse(data))


@router.post("/export")
def start(request: Request, body: GateIn, admin=Depends(ADMIN)):
    groups = _gate(request, admin, body)
    with _jobs_lock:
        _sweep()
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "An export is already running")
        jid = secrets.token_urlsafe(24)
        _jobs[jid] = {"state": "running", "created": time.time(), "by": admin["username"]}
    with db.cursor() as cur:
        audit(cur, admin, "vault_export_started", jid, {"groups": groups})
    threading.Thread(target=_run, args=(jid, admin, groups, body.attachments, body.mode), daemon=True).start()
    return _no_store_json({"job": jid})


@router.get("/export/{job}")
def status(job: str, admin=Depends(ADMIN)):
    j = _jobs.get(job)
    if not j:
        raise HTTPException(404, "No such export")
    return _no_store_json({k: j[k] for k in ("state", "size", "export_id", "sha256", "summary", "snapshot_at", "error", "packs", "files", "progress", "files_note", "batch_no", "kind", "base_batch", "changes") if k in j})


def _send(job: str, part: str, filename: str, admin):
    j = _jobs.get(job)
    if not j or j["state"] != "ready" or part not in j["parts"]:
        raise HTTPException(404, "Not ready")
    with db.cursor() as cur:
        audit(cur, admin, "vault_export_downloaded", job, {"export_id": j["export_id"], "part": part})
    resp = FileResponse(j["parts"][part], media_type="application/octet-stream", filename=filename)
    from starlette.background import BackgroundTask
    resp.background = BackgroundTask(_part_sent, job, part)  # each encrypted file is deleted from the server once it has been sent
    return _no_store(resp)


@router.get("/export/{job}/download")
def download(job: str, admin=Depends(ADMIN)):
    j = _jobs.get(job)
    return _send(job, "db", f"export-{j['export_id'][:8]}.bin" if j and "export_id" in j else "export.bin", admin)


@router.get("/export/{job}/pack/{n}")
def download_pack(job: str, n: int, admin=Depends(ADMIN)):
    j = _jobs.get(job)
    return _send(job, f"pack{n}", f"files-{j['export_id'][:8]}-{n}.bin" if j and "export_id" in j else "files.bin", admin)


@router.get("/batches")
def batches(admin=Depends(ADMIN)):
    """Every export batch and where it stands (pending = made but not yet confirmed as uploaded and checked)."""
    with db.cursor() as cur:
        rows = vault_sync.listing(cur)
    return _no_store_json([{**r, "created_at": r["created_at"].isoformat(),
                            "confirmed_at": r["confirmed_at"].isoformat() if r["confirmed_at"] else None} for r in rows])


@router.post("/batches/{batch_no}/confirm")
def confirm_batch(batch_no: int, request: Request, body: GateIn, admin=Depends(ADMIN)):
    """Say that this batch was uploaded and checked on the other side. Only then does the next export build on it."""
    _gate(request, admin, body)
    with db.cursor() as cur:
        result = vault_sync.confirm(cur, batch_no)
        if result == "ok":
            audit(cur, admin, "vault_batch_confirmed", str(batch_no), None)
    if result == "unknown":
        raise HTTPException(404, "No such batch")
    if result == "not_pending":
        raise HTTPException(409, "Only a batch that is waiting for confirmation can be confirmed")
    return _no_store_json({"ok": True, "batch_no": batch_no})
