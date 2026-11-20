# ruff: noqa: F811, E501
"""Incremental export: row fingerprints, deltas with tombstones, batch lifecycle (pending -> confirmed), and the rule that a batch which
was made but never confirmed can never cause data to be skipped. Throwaway keys and fake storage only."""
import hashlib
import sqlite3
import uuid

from app import attachments, o2d_screens, vault_attachments, vault_export
from tests import vault_ref
from tests.conftest import bill_no
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)
from tests.test_vault_attachments import _attach_rows
from tests.test_vault_export import FP_KEY, GATE_PASSWORD, _seed_rows, _wait_ready, vault_client  # noqa: F401  (fixtures + helpers)

KEY = FP_KEY.encode()
ALL = list(vault_export.SELECTABLE_GROUPS)


def _lite(data):
    c = sqlite3.connect(":memory:")
    c.deserialize(data)
    return c


# ---------------------------------------------------------------- fingerprints and keys
def test_every_exported_table_has_a_row_key(db_conn):
    with db_conn.cursor() as cur:
        for table, only in vault_export.tables_for(ALL).items():
            cols = vault_export._columns(cur, table, only)
            keys = vault_export.key_columns(cur, table, [c for c, _ in cols])
            assert keys, table
    assert vault_export.KEY_OVERRIDES == {"v_orders_archive": ["order_key"]}    # the one view, named explicitly


def test_fingerprint_is_keyed_and_notices_any_change():
    row = (1, "Party A", "100.50", None)
    fp = vault_export.fingerprint(KEY, "fact_orders", row)
    assert len(fp) == 16 and fp == vault_export.fingerprint(KEY, "fact_orders", row)
    assert fp != vault_export.fingerprint(b"another-secret-key-another-secret-key", "fact_orders", row)   # useless without the key
    assert fp != vault_export.fingerprint(KEY, "fact_payment_txn", row)                                      # the table is part of it
    assert fp != vault_export.fingerprint(KEY, "fact_orders", (1, "Party A", "100.51", None))
    assert fp != vault_export.fingerprint(KEY, "fact_orders", (1, "Party A", "100.50", ""))                  # NULL is not an empty string


# ---------------------------------------------------------------- delta contents
def test_delta_carries_only_changes_and_tombstones_and_lands_on_the_same_totals(client, db_conn, seed, migrated_db_url):
    shop = login(client, db_conn, "shop")
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    keep, gone = add(client, shop, today, bill_no())["slNo"], add(client, shop, today, bill_no())["slNo"]
    _seed_rows(client, db_conn, seed)
    url = migrated_db_url
    full, r1 = vault_export.build_snapshot(url, ALL, fp_key=KEY, batch_no=1)
    state = r1["state"]
    assert r1["meta"]["kind"] == "full" and all(t["tombstones"] == 0 for t in r1["tables"].values())

    # nothing changed -> an empty delta that still reports the same totals
    d0, r0 = vault_export.build_snapshot(url, ALL, fp_key=KEY, base_state=state, batch_no=2, base_batch=1)
    assert r0["meta"]["kind"] == "delta" and r0["meta"]["base_batch"] == "1"
    assert all(t["delta_rows"] == 0 and t["tombstones"] == 0 for t in r0["tables"].values())
    assert {t: (v["rows"], v["digest"]) for t, v in r0["tables"].items()} == {t: (v["rows"], v["digest"]) for t, v in r1["tables"].items()}

    # change one order, add one, delete one
    with db_conn.cursor() as cur:
        cur.execute("UPDATE fact_orders SET amount_received = 777.25 WHERE sl_no = %s", (keep,))
        cur.execute("SELECT order_key FROM fact_orders WHERE sl_no = %s", (gone,))
        gone_key = cur.fetchone()[0]
        cur.execute("DELETE FROM fact_orders WHERE sl_no = %s", (gone,))
    db_conn.commit()
    new = add(client, shop, today, bill_no())["slNo"]
    d1, r2 = vault_export.build_snapshot(url, ALL, fp_key=KEY, base_state=state, batch_no=3, base_batch=1)
    lite = _lite(d1)
    in_delta = {r[0] for r in lite.execute("SELECT sl_no FROM fact_orders")}
    assert in_delta == {keep, new}                                         # the changed one and the new one; nothing else
    assert r2["tables"]["fact_orders"]["delta_rows"] == 2 and r2["tables"]["fact_orders"]["tombstones"] == 1
    assert lite.execute("SELECT row_key FROM _vault_tombstones WHERE table_name = 'fact_orders'").fetchall() == [(f"[{gone_key}]",)]
    assert lite.execute("SELECT key_columns FROM _vault_tables WHERE name = 'fact_orders'").fetchone()[0] == '["order_key"]'
    # the batch states what the other side must hold afterwards: exactly what a brand-new full export would say
    _, r3 = vault_export.build_snapshot(url, ALL, fp_key=KEY, batch_no=4)
    assert {t: (v["rows"], v["digest"], v["sums"]) for t, v in r2["tables"].items()} == {t: (v["rows"], v["digest"], v["sums"]) for t, v in r3["tables"].items()}
    assert r2["state"] == r3["state"]                                      # and the fingerprints to remember are the same too


def test_a_delta_needs_the_fingerprint_key(migrated_db_url):
    import pytest
    with pytest.raises(ValueError):
        vault_export.build_snapshot(migrated_db_url, ["orders"], base_state={})


# ---------------------------------------------------------------- batches, confirmation and the safety rule
def _export(client, h, body, **kw):
    job = client.post("/admin/vault/export", json=body(**kw), headers=h).json()["job"]
    return job, _wait_ready(client, h, job)


def _fetch(client, h, job):
    return client.get(f"/admin/vault/export/{job}/download", headers=h).content


def test_batch_lifecycle_pending_confirm_delta_and_supersede(vault_client, db_conn, seed):
    client, priv, body = vault_client
    admin, shop = login(client, db_conn, "admin"), login(client, db_conn, "shop")
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    add(client, shop, today, bill_no())
    # 1. nothing confirmed yet: a full batch, waiting for confirmation
    job1, s1 = _export(client, admin, body)
    assert s1["kind"] == "full" and s1["base_batch"] == 0
    n1 = s1["batch_no"]
    _fetch(client, admin, job1)
    # 2. a second export before anything was confirmed is a full one again; the first is superseded, its fingerprints dropped
    job2, s2 = _export(client, admin, body)
    n2 = s2["batch_no"]
    assert s2["kind"] == "full" and n2 > n1
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM vault_batch WHERE batch_no = %s", (n1,))
        assert cur.fetchone()[0] == "superseded"
        cur.execute("SELECT count(*) FROM vault_row_state WHERE batch_no = %s", (n1,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM vault_row_state WHERE batch_no = %s", (n2,))
        assert cur.fetchone()[0] > 0
    _fetch(client, admin, job2)
    # 3. confirmation: gated, admin only, only for a pending batch
    assert client.post(f"/admin/vault/batches/{n2}/confirm", json=body(password="wrong"), headers=admin).status_code == 403
    assert client.post(f"/admin/vault/batches/{n2}/confirm", json=body(), headers=shop).status_code == 403
    assert client.post("/admin/vault/batches/99999999/confirm", json=body(), headers=admin).status_code == 404
    assert client.post(f"/admin/vault/batches/{n1}/confirm", json=body(), headers=admin).status_code == 409      # superseded
    assert client.post(f"/admin/vault/batches/{n2}/confirm", json=body(), headers=admin).json() == {"ok": True, "batch_no": n2}
    assert client.post(f"/admin/vault/batches/{n2}/confirm", json=body(), headers=admin).status_code == 409      # already confirmed
    listing = {b["batch_no"]: b for b in client.get("/admin/vault/batches", headers=admin).json()}
    assert listing[n2]["status"] == "confirmed" and listing[n1]["status"] == "superseded"
    # 4. data changes; the next export is a delta on the confirmed batch
    changed = add(client, shop, today, bill_no())["slNo"]
    job3, s3 = _export(client, admin, body)
    n3 = s3["batch_no"]
    assert s3["kind"] == "delta" and s3["base_batch"] == n2
    assert s3["changes"]["fact_orders"]["changed"] >= 1
    lite = _lite(vault_ref.decrypt_export(_fetch(client, admin, job3), priv))
    meta = dict(lite.execute("SELECT key, value FROM _vault_meta").fetchall())
    assert meta["kind"] == "delta" and meta["batch_no"] == str(n3) and meta["base_batch"] == str(n2)
    assert lite.execute("SELECT count(*) FROM fact_orders WHERE sl_no = ?", (changed,)).fetchone()[0] == 1
    # 5. the safety rule: n3 was made but NOT confirmed. Another change, another export: it builds on n2 again, so it repeats n3's rows
    again = add(client, shop, today, bill_no())["slNo"]
    job4, s4 = _export(client, admin, body)
    assert s4["kind"] == "delta" and s4["base_batch"] == n2                 # not n3: it was never confirmed
    rows = {r[0] for r in _lite(vault_ref.decrypt_export(_fetch(client, admin, job4), priv)).execute("SELECT sl_no FROM fact_orders")}
    assert {changed, again} <= rows                                          # nothing was skipped
    # 6. confirming the newest drops the older fingerprints
    assert client.post(f"/admin/vault/batches/{s4['batch_no']}/confirm", json=body(), headers=admin).status_code == 200
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT batch_no) FROM vault_row_state")
        assert cur.fetchone()[0] == 1


def test_changed_groups_or_full_mode_start_a_new_baseline(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    _seed_rows(client, db_conn, seed)
    job, s = _export(client, admin, body)
    _fetch(client, admin, job)
    assert client.post(f"/admin/vault/batches/{s['batch_no']}/confirm", json=body(), headers=admin).status_code == 200
    job, other = _export(client, admin, body, groups=["money"])          # different groups cannot be a delta on this baseline
    assert other["kind"] == "full" and other["base_batch"] == 0
    _fetch(client, admin, job)
    job, forced = _export(client, admin, body, mode="full")             # asked for a fresh baseline
    assert forced["kind"] == "full"
    _fetch(client, admin, job)


def test_failed_export_leaves_no_state_and_is_marked_failed(vault_client, db_conn, seed, monkeypatch):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    monkeypatch.setattr(vault_export, "build_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    job = client.post("/admin/vault/export", json=body(), headers=admin).json()["job"]
    s = _wait_ready(client, admin, job)
    assert s["state"] == "failed"
    with db_conn.cursor() as cur:
        cur.execute("SELECT batch_no, status FROM vault_batch ORDER BY batch_no DESC LIMIT 1")
        n, status = cur.fetchone()
        cur.execute("SELECT count(*) FROM vault_row_state WHERE batch_no = %s", (n,))
        assert status == "failed" and cur.fetchone()[0] == 0


def test_fingerprints_in_the_database_are_keyed_and_short(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    _seed_rows(client, db_conn, seed)
    job, s = _export(client, admin, body)
    _fetch(client, admin, job)
    with db_conn.cursor() as cur:
        cur.execute("SELECT row_key, fp FROM vault_row_state WHERE batch_no = %s AND table_name = 'fact_orders' LIMIT 1", (s["batch_no"],))
        row_key, fp = cur.fetchone()
    assert row_key.startswith("[") and len(bytes(fp)) == 16                  # an id and 16 opaque bytes: no business data


def test_delta_carries_only_new_photos(vault_client, db_conn, seed, monkeypatch):
    client, priv, body = vault_client
    admin = login(client, db_conn, "admin")
    sl = _seed_rows(client, db_conn, seed)
    _attach_rows(db_conn, sl)
    store, asked = {}, []

    def fake_fetch(key):
        asked.append(key)
        return store.setdefault(key, (b"PIC-" + key.encode()) * 20)
    real_init = vault_attachments.PackWriter.__init__
    monkeypatch.setattr(attachments, "configured", lambda: True)
    monkeypatch.setattr(vault_attachments.PackWriter, "__init__", lambda self, d, pub, **kw: real_init(self, d, pub, fetch=fake_fetch, target=None))
    job, s1 = _export(client, admin, body)
    assert s1["files"]["files"] >= 3
    _fetch(client, admin, job)
    for p in s1["packs"]:
        client.get(f"/admin/vault/export/{job}/pack/{p['n']}", headers=admin)
    assert client.post(f"/admin/vault/batches/{s1['batch_no']}/confirm", json=body(), headers=admin).status_code == 200
    asked.clear()
    new_key = f"decor/9/{uuid.uuid4().hex}.jpg"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO file_attachment (module, record_key, kind, object_key, content_type, size_bytes) VALUES ('decor', 9, 'photo', %s, 'image/jpeg', 10)", (new_key,))
    db_conn.commit()
    job, s2 = _export(client, admin, body)
    assert s2["kind"] == "delta" and asked == [new_key]                      # only the new photo was fetched
    lite = _lite(vault_ref.decrypt_export(_fetch(client, admin, job), priv))
    index = lite.execute("SELECT source, batch_no, status FROM _vault_files").fetchall()
    assert index == [("file_attachment", s2["batch_no"], "ok")]
    pack = client.get(f"/admin/vault/export/{job}/pack/0", headers=admin).content
    off, ln, sha = lite.execute("SELECT offset, length, sha256 FROM _vault_files").fetchone()
    plain = vault_ref.decrypt_file(pack[off:off + ln], priv)
    assert hashlib.sha256(plain).hexdigest() == sha and plain == store[new_key]
