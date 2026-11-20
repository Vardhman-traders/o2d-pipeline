# ruff: noqa: F811, E501
"""Photos and documents in the encrypted export: only allowed modules travel, every file is encrypted on its own with a padded size,
a missing or mismatching file is recorded rather than fatal, and the packs hold ciphertext only. Throwaway keys, fake storage."""
import hashlib
import sqlite3
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app import attachments, vault_attachments, vault_crypto, vault_export
from tests import vault_ref
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)
from tests.test_vault_export import _seed_rows, _wait_ready, vault_client  # noqa: F401  (fixtures + helpers)


def _attach_rows(db_conn, sl):
    """One file_attachment row per module (attendance, face and delegation must never travel) plus an order photo."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT order_key FROM fact_orders WHERE sl_no = %s", (sl,))
        order_key = cur.fetchone()[0]
        for module in ("attendance", "attface", "delegation", "decor", "purchase", "challan"):
            cur.execute("INSERT INTO file_attachment (module, record_key, kind, object_key, content_type, size_bytes, sha256) "
                        "VALUES (%s, 1, 'photo', %s, 'image/jpeg', 10, NULL)", (module, f"{module}/1/{uuid.uuid4().hex}.jpg"))
        cur.execute("INSERT INTO order_attachments (order_key, kind, object_key, content_type, size_bytes) "
                    "VALUES (%s, 'dispatch', %s, 'image/jpeg', 10)", (order_key, f"order/{order_key}/{uuid.uuid4().hex}.jpg"))
    db_conn.commit()


def test_file_attachment_rows_are_limited_to_allowed_modules(client, db_conn, seed, migrated_db_url):
    _attach_rows(db_conn, _seed_rows(client, db_conn, seed))
    for groups, allowed in ((["orders", "money", "decor"], {"challan", "purchase", "decor"}), (["orders"], {"challan"}), (["decor"], {"decor"})):
        data, report = vault_export.build_snapshot(migrated_db_url, groups)
        lite = sqlite3.connect(":memory:")
        lite.deserialize(data)
        assert {r[0] for r in lite.execute("SELECT DISTINCT module FROM file_attachment")} == allowed
        flt = lite.execute("SELECT row_filter FROM _vault_tables WHERE name = 'file_attachment'").fetchone()[0]
        assert "module = ANY" in flt and "attendance" not in flt and "attface" not in flt   # recorded for the validation step
        assert report["tables"]["file_attachment"]["rows"] == lite.execute("SELECT count(*) FROM file_attachment").fetchone()[0]


def test_bucketed_per_file_encryption():
    priv, pub = vault_ref.new_keypair()
    a, id_a = vault_crypto.encrypt_file(b"x", pub)
    b, id_b = vault_crypto.encrypt_file(b"y" * 60_000, pub)
    c, _ = vault_crypto.encrypt_file(b"z" * 70_000, pub)
    assert len(a) == len(b) and len(c) > len(a)                                           # 1 byte and 60 KB look the same size
    assert vault_crypto.bucket_for(0) == 65536 and vault_crypto.bucket_for(65532) == 65536 and vault_crypto.bucket_for(65533) == 131072
    assert vault_ref.decrypt_file(a, priv) == b"x" and vault_ref.decrypt_file(b, priv) == b"y" * 60_000
    assert vault_ref.decrypt_file(c, priv) == b"z" * 70_000
    assert id_a != id_b and a[93:141] != b[93:141]                                        # every file has its own key
    other, _ = vault_ref.new_keypair()
    with pytest.raises(InvalidTag):
        vault_ref.decrypt_file(a, other)
    with pytest.raises(InvalidTag):
        vault_ref.decrypt_file(a[:-1] + bytes([a[-1] ^ 1]), priv)
    with pytest.raises(InvalidTag):
        vault_ref.decrypt_file(a[:65] + id_b + a[81:], priv)                              # a record cannot pose as another file
    with pytest.raises(ValueError):
        vault_crypto.encrypt_file(b"q" * (vault_crypto.MAX_FILE_BYTES + 1), pub)


def test_pack_writer_survives_a_missing_file_and_splits_packs(tmp_path):
    priv, pub = vault_ref.new_keypair()
    store = {"a": b"A" * 1000, "b": b"B" * 2000, "c": b"C" * 3000}
    w = vault_attachments.PackWriter(tmp_path, pub, fetch=lambda key: store[key], target=100_000)   # one record is ~65 KB: one per pack
    e = [w.add(k, None) for k in ("a", "nope", "b", "c")]
    packs = w.finish()
    assert [x["status"] for x in e] == ["ok", "missing", "ok", "ok"] and w.counts == {"files": 3, "missing": 1, "checksum_differs": 0}
    assert len(packs) == 3 and all(p["size"] > 0 for p in packs)
    for x, original in ((e[0], store["a"]), (e[2], store["b"]), (e[3], store["c"])):
        raw = open(packs[x["pack"]]["path"], "rb").read()
        assert hashlib.sha256(raw).hexdigest() == packs[x["pack"]]["sha256"]
        assert vault_ref.decrypt_file(raw[x["offset"]:x["offset"] + x["length"]], priv) == original
        assert x["sha256"] == hashlib.sha256(original).hexdigest() and b"AAAA" not in raw and b"BBBB" not in raw
    assert w.add("a", "0" * 64)["status"] == "checksum_differs"                              # the database says one thing, storage holds another


def test_export_with_photos_end_to_end(vault_client, db_conn, seed, tmp_path, monkeypatch):
    client, priv, body = vault_client
    admin = login(client, db_conn, "admin")
    _attach_rows(db_conn, _seed_rows(client, db_conn, seed))
    with db_conn.cursor() as cur:
        cur.execute("SELECT 'order_attachments', attachment_key, object_key FROM order_attachments UNION ALL "
                    "SELECT 'file_attachment', attachment_key, object_key FROM file_attachment")
        originals = {obj: (b"PHOTO-" + obj.encode()) * 50 for _, _, obj in cur.fetchall()}
        decor_key = next(k for k in originals if k.startswith("decor/1/"))
        cur.execute("UPDATE file_attachment SET sha256 = %s WHERE object_key = %s", (hashlib.sha256(originals[decor_key]).hexdigest(), decor_key))
        missing_key = next(k for k in originals if k.startswith("purchase/1/"))
    db_conn.commit()
    asked = []

    def fake_fetch(key):
        asked.append(key)
        if key == missing_key:
            raise OSError("gone")
        return originals[key]
    real_init = vault_attachments.PackWriter.__init__
    monkeypatch.setattr(attachments, "configured", lambda: True)
    monkeypatch.setattr(vault_attachments.PackWriter, "__init__", lambda self, d, pub, **kw: real_init(self, d, pub, fetch=fake_fetch, target=100_000))
    job = client.post("/admin/vault/export", json=body(), headers=admin).json()["job"]
    s = _wait_ready(client, admin, job)
    assert s["state"] == "ready" and s["files"]["missing"] == 1 and s["files"]["files"] >= 3 and len(s["packs"]) >= 2
    assert not any(k.startswith(("attendance/", "attface/", "delegation/")) for k in asked)   # never even fetched
    db_blob = client.get(f"/admin/vault/export/{job}/download", headers=admin).content
    lite = sqlite3.connect(":memory:")
    lite.deserialize(vault_ref.decrypt_export(db_blob, priv))
    objs = {(r[0], r[1]): r[2] for r in lite.execute("SELECT 'order_attachments', attachment_key, object_key FROM order_attachments UNION ALL "
                                                     "SELECT 'file_attachment', attachment_key, object_key FROM file_attachment")}
    assert not any(o.startswith(("attendance/", "attface/", "delegation/")) for o in objs.values())   # other tests may leave real order photos; these never
    index = {(r[0], r[1]): r for r in lite.execute("SELECT source, attachment_key, file_id, pack, offset, length, size, sha256, status FROM _vault_files")}
    statuses = {objs[k]: r[8] for k, r in index.items()}
    assert statuses[missing_key] == "missing" and statuses[decor_key] == "ok"
    packs = {p["n"]: client.get(f"/admin/vault/export/{job}/pack/{p['n']}", headers=admin).content for p in s["packs"]}
    for p in s["packs"]:
        assert hashlib.sha256(packs[p["n"]]).hexdigest() == p["sha256"] and b"PHOTO-" not in packs[p["n"]]
    done = 0
    for key, row in index.items():
        if row[8] == "missing":
            continue
        _, _, _, pack, off, ln, size, sha, _ = row
        plain = vault_ref.decrypt_file(packs[pack][off:off + ln], priv)
        assert plain == originals[objs[key]] and len(plain) == size and hashlib.sha256(plain).hexdigest() == sha
        done += 1
    assert done >= 3
    assert not list((tmp_path / "vault").glob("*.bin")) and job not in vault_export._jobs   # all deleted once every part was sent
