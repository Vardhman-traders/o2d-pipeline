"""Master-data reconciliation (admin only): every distinct value in the shared lists (channels, submission types,
delivery/payment statuses, people by role) with how many orders use it, likely duplicates, rename in place, and merge
with a preview. Orders point at the dimension row, so a rename or merge shows up everywhere at once.

Rows the order logic keys off by name (delivery status 'Shop' and 'Cancelled', submission type 'Cancelled') are
protected: they cannot be renamed or merged away, though spelling variants can be merged INTO them."""
import re
from difflib import SequenceMatcher
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .admin import ADMIN, audit

router = APIRouter(prefix="/admin/reconcile", tags=["reconcile"])
LOCK_ID = 7002
SIMILAR = 0.84
MAX_SUGGESTIONS = 200

# kind -> table, key column, name column, label, protected (lower-case) names, optional role column
KINDS: dict[str, dict[str, Any]] = {
    "channel": {"table": "dim_order_channel", "key": "channel_key", "name": "channel_name",
                "label": "Order via (channel)", "protected": set(), "role": None, "maxlen": 50},
    "submission_type": {"table": "dim_submission_type", "key": "submission_type_key", "name": "type_name",
                        "label": "Type of submission", "protected": {"cancelled"}, "role": None, "maxlen": 50},
    "delivery_status": {"table": "dim_delivery_status", "key": "status_key", "name": "status_name",
                        "label": "Delivery status", "protected": {"shop", "cancelled"}, "role": None, "maxlen": 50},
    "payment_status": {"table": "dim_payment_status", "key": "payment_status_key", "name": "status_name",
                       "label": "Payment status", "protected": set(), "role": None, "maxlen": 50},
    "company": {"table": "dim_company", "key": "company_key", "name": "company_name", "label": "Companies (payments)",
                "protected": set(), "role": None, "maxlen": 100},
    "payment_mode": {"table": "dim_payment_mode", "key": "mode_key", "name": "mode_name", "label": "Payment modes",
                     "protected": set(), "role": None, "maxlen": 50},
    "txn_type": {"table": "dim_txn_type", "key": "txn_type_key", "name": "type_name", "label": "Payment transaction types",
                 "protected": set(), "role": None, "maxlen": 50},
    "party": {"table": "dim_party", "key": "party_key", "name": "party_name", "label": "Parties and vendors",
              "protected": set(), "role": "party_kind", "maxlen": 150},
    "person": {"table": "dim_person", "key": "person_key", "name": "full_name",
               "label": "People (ready by, colour making, delivery)", "protected": set(), "role": "person_role",
               "maxlen": 100},
}
ROLE_LABEL = {"ready_by": "Ready by", "colour_making": "Colour making", "delivery": "Delivery", "payment": "Payment parties", "vendor": "Vendors"}


def _kind(kind: str) -> dict[str, Any]:
    spec = KINDS.get(kind)
    if not spec:
        raise HTTPException(404, "Unknown list")
    return spec


def clean_name(raw: str) -> str:
    return " ".join(raw.split())


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _references(cur, table: str) -> list[tuple[str, str]]:
    """Every (table, column) with a foreign key to this dimension - found from the catalog, so a new dashboard that
    points at the same master list is updated by a merge without changing this file."""
    cur.execute("""SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
                   FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
                   WHERE c.contype = 'f' AND c.confrelid = %s::regclass AND array_length(c.conkey, 1) = 1""",
                (table,))
    return [(r["tbl"], r["col"]) for r in cur.fetchall()]


# person role -> the order column that holds that role
PERSON_COLUMN = {"ready_by": "ready_by_person_key", "colour_making": "colour_making_person_key",
                 "delivery": "delivered_by_person_key"}


def _rows(cur, spec) -> list[dict]:
    t, k, n = spec["table"], spec["key"], spec["name"]
    role = f", d.{spec['role']} AS role" if spec["role"] else ", NULL AS role"
    phone = ", d.phone_number AS phone" if spec["table"] == "dim_person" else ", NULL AS phone"
    cur.execute(f'SELECT d.{k} AS key, d.{n} AS name{role}{phone} FROM "{t}" d')
    rows = cur.fetchall()
    counts: dict[int, int] = {}
    for tbl, col in _references(cur, t):
        cur.execute(f'SELECT "{col}" AS key, count(*) AS n FROM "{tbl}" WHERE "{col}" IS NOT NULL GROUP BY 1')
        for r in cur.fetchall():
            counts[r["key"]] = counts.get(r["key"], 0) + r["n"]
    for r in rows:
        r["orders"] = counts.get(r["key"], 0)
        r["protected"] = r["name"].strip().lower() in spec["protected"]
    rows.sort(key=lambda r: (r["name"].lower(), r["role"] or ""))
    return rows


def suggest(rows: list[dict]) -> list[dict]:
    """Pairs that look like the same thing spelled two ways (same role for people)."""
    out = []
    norm = [(r, _norm(r["name"])) for r in rows]
    for i, (a, na) in enumerate(norm):
        for b, nb in norm[i + 1:]:
            if a["role"] != b["role"] or not na or not nb:
                continue
            same = na == nb
            sm = SequenceMatcher(None, na, nb)
            if same or (min(len(na), len(nb)) >= 3 and sm.real_quick_ratio() >= SIMILAR
                        and sm.quick_ratio() >= SIMILAR and sm.ratio() >= SIMILAR):
                out.append({"keys": [a["key"], b["key"]], "names": [a["name"], b["name"]], "role": a["role"],
                            "score": 1.0 if same else round(sm.ratio(), 2)})
    out.sort(key=lambda s: -s["score"])
    return out[:MAX_SUGGESTIONS]


@router.get("")
def overview(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        result = []
        for kind, spec in KINDS.items():
            rows = _rows(cur, spec)
            result.append({"kind": kind, "label": spec["label"], "distinct": len(rows),
                           "unused": sum(1 for r in rows if not r["orders"]),
                           "possible_duplicates": len(suggest(rows))})
    return result


@router.get("/{kind}")
def values(kind: str, admin=Depends(ADMIN)):
    spec = _kind(kind)
    with db.cursor() as cur:
        rows = _rows(cur, spec)
    return {"kind": kind, "label": spec["label"], "protected": sorted(spec["protected"]),
            "role_labels": ROLE_LABEL if spec["role"] else None, "values": rows, "suggestions": suggest(rows)}


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: int
    name: str = Field(min_length=1, max_length=100)


def _fetch(cur, spec, key):
    cols = f", {spec['role']} AS role" if spec["role"] else ", NULL AS role"
    cur.execute(f'SELECT {spec["key"]} AS key, {spec["name"]} AS name{cols} FROM "{spec["table"]}" '
                f'WHERE {spec["key"]} = %s', (key,))
    return cur.fetchone()


@router.post("/{kind}/rename")
def rename(kind: str, body: RenameIn, admin=Depends(ADMIN)):
    spec = _kind(kind)
    name = clean_name(body.name)
    if not name or len(name) > spec["maxlen"]:
        raise HTTPException(422, f"Name must be 1 to {spec['maxlen']} characters.")
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        row = _fetch(cur, spec, body.key)
        if not row:
            raise HTTPException(404, "That value no longer exists. Refresh the list.")
        if row["name"].strip().lower() in spec["protected"]:
            raise HTTPException(409, f"'{row['name']}' is used by the order screens' rules and cannot be renamed.")
        if name.lower() in spec["protected"]:
            raise HTTPException(409, f"'{name}' is reserved for a built-in meaning. Merge into the existing "
                                     f"'{name}' instead if this is a spelling variant.")
        role_sql = f" AND {spec['role']} = %s" if spec["role"] else ""
        params: list = [name.lower(), body.key] + ([row["role"]] if spec["role"] else [])
        cur.execute(f'SELECT {spec["name"]} AS name FROM "{spec["table"]}" WHERE lower(btrim({spec["name"]})) = %s '
                    f'AND {spec["key"]} <> %s{role_sql}', params)
        clash = cur.fetchone()
        if clash:
            raise HTTPException(409, f"'{clash['name']}' already exists. Use Merge to combine them.")
        cur.execute(f'UPDATE "{spec["table"]}" SET {spec["name"]} = %s WHERE {spec["key"]} = %s', (name, body.key))
        audit(cur, admin, f"reconcile.rename.{kind}", row["name"], {"from": row["name"], "to": name,
                                                                     "key": body.key})
    return {"ok": True, "name": name}


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_key: int
    source_keys: list[int] = Field(min_length=1, max_length=50)
    confirm: bool = False


@router.post("/{kind}/merge")
def merge(kind: str, body: MergeIn, admin=Depends(ADMIN)):
    """Preview (confirm=false) or perform (confirm=true) a merge: orders move to the target, the others are deleted."""
    spec = _kind(kind)
    sources = sorted(set(body.source_keys))
    if body.target_key in sources:
        raise HTTPException(422, "The value to keep cannot also be one of the values to merge away.")
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        target = _fetch(cur, spec, body.target_key)
        found = [_fetch(cur, spec, k) for k in sources]
        if not target or not all(found):
            raise HTTPException(404, "One of the values no longer exists. Refresh the list.")
        if spec["role"] and any(f["role"] != target["role"] for f in found):
            raise HTTPException(422, "People can only be merged within the same role.")
        locked = [f["name"] for f in found if f["name"].strip().lower() in spec["protected"]]
        if locked:
            raise HTTPException(409, f"{', '.join(repr(n) for n in locked)} can't be merged away: the order "
                                     "screens' rules depend on it. Merge the variants into it instead.")
        refs = _references(cur, spec["table"])
        moved = 0
        for tbl, col in refs:
            cur.execute(f'SELECT count(*) AS n FROM "{tbl}" WHERE "{col}" = ANY(%s)', (sources,))
            moved += cur.fetchone()["n"]
        preview = {"target": target["name"], "merging": [f["name"] for f in found], "orders_moved": moved}
        if not body.confirm:
            return {**preview, "preview": True}
        for tbl, col in refs:
            cur.execute(f'UPDATE "{tbl}" SET "{col}" = %s WHERE "{col}" = ANY(%s)', (body.target_key, sources))
        if spec["table"] == "dim_person":                # keep a phone number if only the merged-away row had one
            cur.execute("UPDATE dim_person SET phone_number = (SELECT phone_number FROM dim_person WHERE person_key "
                        "= ANY(%s) AND phone_number IS NOT NULL ORDER BY person_key LIMIT 1) "
                        "WHERE person_key = %s AND phone_number IS NULL", (sources, body.target_key))
        if kind == "delivery_status" and target["name"].strip().lower() == "cancelled":
            cur.execute("UPDATE fact_orders SET is_cancelled = true WHERE delivery_status_key = %s", (body.target_key,))
        cur.execute(f'DELETE FROM "{spec["table"]}" WHERE {spec["key"]} = ANY(%s)', (sources,))
        audit(cur, admin, f"reconcile.merge.{kind}", target["name"], preview)
    return {**preview, "preview": False}
