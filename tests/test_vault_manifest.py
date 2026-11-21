# ruff: noqa: F811, E501
"""The dashboard definitions that travel inside every export: well-formed, read-only, and every query really runs over a real exported copy."""
import json
import re
import sqlite3

import pytest

from app import vault_export, vault_manifest
from tests.test_vault_delete import conn, data, ddb  # noqa: F401  (fixtures: a fresh database with a realistic row in every table)

ALL = list(vault_export.SELECTABLE_GROUPS)
FORMATS = {"int", "money", "hours", "pct", "date", "datetime", "text"}
BLOCKS = {"kpis", "bars", "line", "table"}
SECTIONS = {"fields", "table", "files"}


def all_sql(m):
    """(where, sql, kind, extra) for every query in the manifest."""
    for tab in m["tabs"]:
        for b in tab.get("blocks", []):
            if b["type"] == "kpis":
                for k in b["items"]:
                    yield f"{tab['id']}/kpi {k['label']}", k["sql"], "kpi", k
            else:
                yield f"{tab['id']}/{b['title']}", b["sql"], b["type"], b
        lst = tab.get("list")
        if lst:
            yield f"{tab['id']}/list", lst["sql"], "list", lst
            for s in (lst.get("detail") or {}).get("sections", []):
                yield f"{tab['id']}/detail {s['title']}", s["sql"], "d_" + s["type"], s


def test_manifest_is_well_formed():
    m = json.loads(vault_manifest.build_json())
    assert m["version"] == vault_manifest.MANIFEST_VERSION and m["tabs"]
    ids = [t["id"] for t in m["tabs"]]
    assert len(ids) == len(set(ids))
    for tab in m["tabs"]:
        assert ("blocks" in tab) != ("list" in tab)
        for b in tab.get("blocks", []):
            assert b["type"] in BLOCKS
        for s in ((tab.get("list") or {}).get("detail") or {}).get("sections", []):
            assert s["type"] in SECTIONS
    for where, sql, kind, extra in all_sql(m):
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), where
        assert ";" not in sql, where
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|PRAGMA|REPLACE|VACUUM)\b", sql, re.I), where
        for c in extra.get("columns", []):
            assert c["format"] in FORMATS, (where, c)
        if kind == "kpi":
            assert extra["format"] in FORMATS


def test_every_query_runs_over_a_real_exported_copy(ddb, data):
    blob, report = vault_export.build_snapshot(ddb, ALL)
    lite = sqlite3.connect(":memory:")
    lite.deserialize(blob)
    lite.execute("PRAGMA query_only = ON")                                 # what the viewer does: the definitions can read, never write
    m = json.loads(lite.execute("SELECT value FROM _vault_manifest WHERE key = 'dashboards'").fetchone()[0])
    assert m == json.loads(vault_manifest.build_json())
    ran = 0
    for where, sql, kind, extra in all_sql(m):
        params = {"key": 1} if ":key" in sql else {}
        cur = lite.execute(sql, params)
        names = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if kind == "kpi":
            assert len(names) == 1 and len(rows) == 1, where
        elif kind == "bars":
            assert names == ["label", "value"], where
        elif kind == "line":
            assert names == ["x", "y"], where
        elif kind == "table" or kind == "d_table":
            assert names == [c["name"] for c in extra["columns"]], (where, names)
        elif kind == "list":
            assert names == [c["name"] for c in extra["columns"]], where
            for s in extra["search"] + [extra["key"] or extra["columns"][0]["name"]]:
                assert s in names, (where, s)
            for part in extra["order"].split(","):                          # the viewer only accepts "column [ASC|DESC]" in order
                assert part.split()[0] in names, (where, part)
            lite.execute(f"SELECT * FROM ({sql}) WHERE ({' OR '.join(f'{c} LIKE ?' for c in extra['search'])}) ORDER BY {extra['order']} LIMIT 5 OFFSET 0", ["%x%"] * len(extra["search"])).fetchall()
        elif kind == "d_files":
            assert names == ["id", "label"], where
        elif kind == "d_fields":
            assert len(rows) <= 1, where
        ran += 1
    assert ran >= 40
    with pytest.raises(sqlite3.OperationalError):
        lite.execute("DELETE FROM fact_orders")                             # and read-only really is enforced


def test_headline_numbers_match_the_data(ddb, data, conn):
    blob, _ = vault_export.build_snapshot(ddb, ALL)
    lite = sqlite3.connect(":memory:")
    lite.deserialize(blob)
    m = json.loads(vault_manifest.build_json())
    kpis = {k["label"]: lite.execute(k["sql"]).fetchone()[0] for tab in m["tabs"] for b in tab.get("blocks", []) if b["type"] == "kpis" for k in b["items"]}
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM v_orders_archive")
    assert kpis["Orders"] == cur.fetchone()[0] >= 1
    cur.execute("SELECT count(*) FROM fact_payment_txn")
    assert kpis["Payment entries"] == cur.fetchone()[0] >= 1
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM fact_decor_payment")
    assert abs(kpis["VT Decor received"] - float(cur.fetchone()[0])) < 0.01


def test_pack_index_names_every_pack_by_checksum(ddb, data, tmp_path):
    from app import vault_attachments
    priv_pub = __import__("tests.vault_ref", fromlist=["x"]).new_keypair()[1]
    w = vault_attachments.PackWriter(tmp_path, priv_pub, fetch=lambda k: b"X" * 70_000, target=100_000)
    blob, report = vault_export.build_snapshot(ddb, ALL, w, None, batch_no=7)
    lite = sqlite3.connect(":memory:")
    lite.deserialize(blob)
    rows = lite.execute("SELECT batch_no, pack, sha256, size FROM _vault_packs ORDER BY pack").fetchall()
    assert rows and [(r[0], r[1]) for r in rows] == [(7, i) for i in range(len(rows))]
    assert [(r[2], r[3]) for r in rows] == [(p["sha256"], p["size"]) for p in report["packs"]]
