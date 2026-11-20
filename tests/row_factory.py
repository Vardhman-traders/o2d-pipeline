# ruff: noqa: E501
"""Test helper: make one realistic row in any table of the real schema, creating whatever parent rows it needs.
It reads the catalogue (NOT NULL columns without a default, foreign keys, types), so it keeps working as migrations add tables."""
import uuid

import psycopg2.extras

SPECIAL = {   # (table, column) -> value or callable(cur) -> value, for columns where the generic guess is not enough (check constraints)
    ("dim_date", "date_key"): 20260115, ("dim_date", "full_date"): "2026-01-15", ("dim_date", "day"): 15, ("dim_date", "month"): 1,
    ("dim_date", "year"): 2026, ("dim_date", "day_of_week"): "Thursday",
    ("doc_gap_void", "doc_type"): "Challan", ("payment_push", "kind"): "sales", ("decor_fixed_expense", "month"): 1,
    ("att_punch_check", "kind"): "in", ("order_attachments", "kind"): "dispatch", ("fact_expense_entry", "kind"): "Topup",
    ("dim_checklist", "frequency"): "Daily", ("fact_weekly_plan", "green_pct"): 100, ("fact_weekly_plan", "yellow_pct"): 0,
    ("fact_weekly_plan", "red_pct"): 0, ("order_doc_read", "method"): "none",
}


def _columns(cur, table):
    cur.execute("""SELECT column_name, data_type, is_nullable, column_default, is_identity, is_generated, character_maximum_length, udt_name
                   FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))
    return cur.fetchall()


def _fks(cur, table):
    cur.execute("""SELECT (SELECT attname FROM pg_attribute WHERE attrelid = c.conrelid AND attnum = c.conkey[1]) AS col,
                          c.confrelid::regclass::text AS parent,
                          (SELECT attname FROM pg_attribute WHERE attrelid = c.confrelid AND attnum = c.confkey[1]) AS pcol
                   FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid = %s::regclass AND cardinality(c.conkey) = 1""", (table,))
    return {r["col"]: (r["parent"], r["pcol"]) for r in cur.fetchall()}


def _pk(cur, table):
    cur.execute("""SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = %s::regclass AND i.indisprimary""", (table,))
    return [r["attname"] for r in cur.fetchall()]


def _sample(col):
    t, n = col["data_type"], col["character_maximum_length"]
    if t in ("integer", "bigint", "smallint"):
        return uuid.uuid4().int % 900_000 + 100_000 if t != "smallint" else uuid.uuid4().int % 30_000
    if t in ("character varying", "character", "text"):
        s = "t" + uuid.uuid4().hex
        return s[: n or 24]
    if t in ("numeric", "real", "double precision"):
        return 12.5
    if t == "boolean":
        return False
    if t == "date":
        return "2026-01-15"
    if t.startswith("timestamp"):
        return "2026-01-15 10:00:00+00"
    if t.startswith("time"):
        return "10:00:00"
    if t == "jsonb" or t == "json":
        return psycopg2.extras.Json({})
    if t == "ARRAY":
        return []
    if t == "bytea":
        return b"x"
    raise ValueError(f"no sample for {t}")


def make_row(cur, table, overrides=None, hints=None, _depth=0):
    """Insert one row into `table` and return the full row. `hints` {(table, column): value} pins foreign keys to chosen parents."""
    if _depth > 8:
        raise RuntimeError(f"foreign keys too deep at {table}")
    overrides, hints = overrides or {}, hints or {}
    fks, values = _fks(cur, table), {}
    for col in _columns(cur, table):
        name = col["column_name"]
        if name in overrides:
            values[name] = overrides[name]
            continue
        required = col["is_nullable"] == "NO" and col["column_default"] is None and col["is_identity"] == "NO" and col["is_generated"] == "NEVER"
        if (table, name) in hints:
            values[name] = hints[(table, name)]
        elif (table, name) in SPECIAL:
            v = SPECIAL[(table, name)]
            values[name] = v(cur) if callable(v) else v
        elif name in fks and required:
            parent, pcol = fks[name]
            cur.execute(sql_pick(parent, pcol))
            got = cur.fetchone()
            values[name] = got[pcol] if got else make_row(cur, parent, hints=hints, _depth=_depth + 1)[pcol]
        elif required:
            values[name] = _sample(col)
    cols = list(values)
    q = (f'INSERT INTO "{table}" (' + ", ".join(f'"{c}"' for c in cols) + ") VALUES (" + ", ".join(["%s"] * len(cols)) + ") RETURNING *") if cols \
        else f'INSERT INTO "{table}" DEFAULT VALUES RETURNING *'
    cur.execute(q, [values[c] for c in cols])
    return cur.fetchone()


def sql_pick(parent, pcol):
    return f'SELECT "{pcol}" FROM "{parent}" ORDER BY 1 LIMIT 1'
