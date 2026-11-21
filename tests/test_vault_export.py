# ruff: noqa: F811, E501
"""Encrypted export (app/vault_export.py): every table is classified, secrets never leave, the file is readable only with the private
key, and the gate (admin + password + one-time authenticator code + lockout) cannot be skipped. Throwaway keys only."""
import base64
import hashlib
import json
import sqlite3
import time
import types
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app import auth, vault_crypto, vault_export
from app.main import app
from tests import vault_ref
from tests.conftest import bill_no
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)

HIDDEN_PHONE = "98" + str(uuid.uuid4().int)[:8]   # unique per run: other tests use ordinary phone numbers that DO belong in the export
GATE_PASSWORD = "gate-pass-for-tests-1"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
FP_KEY = "fingerprint-key-for-tests-0123456789"


# ---------------------------------------------------------------- pure pieces
def test_every_table_is_classified_exactly_once(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        in_db = {r[0] for r in cur.fetchall()}
    exported = {t for g in vault_export.GROUPS.values() for t in g}
    assert not (in_db - exported - vault_export.EXCLUDED), "a table has no export decision: add it to GROUPS or EXCLUDED"
    assert (exported - in_db) <= {"v_orders_archive"}, "an exported table no longer exists"   # the one view is allowed
    assert not (exported & vault_export.EXCLUDED), "a table is both exported and excluded"
    for never in ("fact_attendance", "att_punch_check", "fact_att_advance", "dim_att_employee", "app_config", "page_access"):
        assert never in vault_export.EXCLUDED and never not in exported


def test_no_secret_column_is_listed_and_users_are_names_only():
    for group in vault_export.GROUPS.values():
        for cols in group.values():
            assert not (set(cols or []) & vault_export.SECRET_COLUMNS)
    assert vault_export.GROUPS["reference"]["dim_user"] == ["user_key", "username", "display_name", "role"]


def test_totp_matches_rfc6238_vector_and_codes_are_single_use(monkeypatch):
    rfc_secret = base64.b32encode(b"12345678901234567890").decode()
    assert vault_export._totp(rfc_secret, 59 // 30) == "287082"          # RFC 6238 test vector, last 6 digits of 94287082
    monkeypatch.setenv("VAULT_EXPORT_TOTP_SECRET", TOTP_SECRET)
    vault_export.reset_replay_guard()
    now = 1_900_000_000.0
    code = vault_export._totp(TOTP_SECRET, int(now // 30))
    assert vault_export.totp_ok(code, now) is True
    assert vault_export.totp_ok(code, now) is False                       # replay refused
    assert vault_export.totp_ok("000000", now) is False and vault_export.totp_ok("abc", now) is False
    old = vault_export._totp(TOTP_SECRET, int(now // 30) - 5)             # 2.5 minutes old
    assert vault_export.totp_ok(old, now) is False


def test_value_encoding_is_exact():
    from datetime import UTC, date, datetime
    from decimal import Decimal
    assert vault_export.to_sqlite(Decimal("12.50")) == "12.50"             # money keeps its digits
    assert vault_export.to_sqlite(True) == 1 and vault_export.to_sqlite(None) is None
    assert vault_export.to_sqlite(date(2026, 1, 2)) == "2026-01-02"
    assert vault_export.to_sqlite(datetime(2026, 1, 2, 3, 4, tzinfo=UTC)).startswith("2026-01-02T03:04:00")
    assert vault_export.to_sqlite({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'
    assert vault_export.row_digest(("a", 1)) != vault_export.row_digest(("a", 2))
    assert vault_export.row_digest(("ab", "c")) != vault_export.row_digest(("a", "bc"))   # cell boundaries matter


def test_crypto_roundtrip_and_public_key_only_server():
    priv, pub = vault_ref.new_keypair()
    data = b"x" * (2 * 1024 * 1024 + 5)
    blob, export_id = vault_crypto.encrypt_export(data, pub)
    assert vault_ref.decrypt_export(blob, priv) == data
    assert blob[65:81] == export_id and b"x" * 64 not in blob
    assert vault_crypto.encrypt_export(data, pub)[0] != blob                 # fresh keys every export
    other, _ = vault_ref.new_keypair()
    with pytest.raises(InvalidTag):
        vault_ref.decrypt_export(blob, other)                                # only the matching private key opens it
    with pytest.raises((InvalidTag, AssertionError)):
        vault_ref.decrypt_export(blob[:-1], priv)
    with pytest.raises(vault_crypto.BadPublicKey):
        vault_crypto.encrypt_export(b"x", b"\x04" + b"\x01" * 64)
    assert not hasattr(vault_crypto, "decrypt") and "private" not in vault_crypto.__doc__.split("PUBLIC")[0]


# ---------------------------------------------------------------- the snapshot
def _seed_rows(client, db_conn, seed):
    shop = login(client, db_conn, "shop")
    today = __import__("app.o2d_screens", fromlist=["x"]).today_ist()
    ensure_date(db_conn, today)
    sl = add(client, shop, today, bill_no(), remarks="vault test " + uuid.uuid4().hex[:6])["slNo"]
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_att_employee (employee_name, phone_number) VALUES (%s, %s)", ("Hidden Emp " + uuid.uuid4().hex[:5], HIDDEN_PHONE))
        cur.execute("UPDATE fact_orders SET amount_received = 12345.67 WHERE sl_no = %s", (sl,))
    db_conn.commit()
    return sl


def test_snapshot_has_only_allowed_tables_and_matching_counts(client, db_conn, seed, migrated_db_url):
    _seed_rows(client, db_conn, seed)
    data, report = vault_export.build_snapshot(migrated_db_url, vault_export.SELECTABLE_GROUPS)
    lite = sqlite3.connect(":memory:")
    lite.deserialize(data)
    names = {r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected = set(vault_export.tables_for(vault_export.SELECTABLE_GROUPS))
    assert names == expected | {"_vault_meta", "_vault_tables", "_vault_columns", "_vault_files", "_vault_tombstones", "_vault_packs", "_vault_manifest"}
    assert not (names & vault_export.EXCLUDED)
    cols = [r[1] for r in lite.execute("PRAGMA table_info(dim_user)")]
    assert cols == ["user_key", "username", "display_name", "role"]
    for t in names:   # no table of any kind carries a password column
        assert "password_hash" not in [r[1] for r in lite.execute(f'PRAGMA table_info("{t}")')]
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM dim_user")
        real_hashes = [h.encode() for (h,) in cur.fetchall() if h and h != "!"]
    assert real_hashes and not any(h in data for h in real_hashes)           # no actual login hash appears anywhere in the image
    assert b"hidden emp" not in data.lower() and HIDDEN_PHONE.encode() not in data   # the attendance employee never travels
    with db_conn.cursor() as cur:
        for t in ("fact_orders", "dim_party", "fact_payment_txn", "dim_user"):
            cur.execute(f'SELECT count(*) FROM "{t}"')
            assert lite.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == cur.fetchone()[0] == report["tables"][t]["rows"]
        cur.execute("SELECT sum(amount_received) FROM fact_orders")
        pg_sum = cur.fetchone()[0]
    assert report["tables"]["fact_orders"]["sums"]["amount_received"] == str(pg_sum)         # money sums are exact
    assert str(lite.execute("SELECT amount_received FROM fact_orders WHERE amount_received = '12345.67'").fetchone()[0]) == "12345.67"


def test_stored_digests_match_a_recount_from_the_sqlite_values(client, db_conn, seed, migrated_db_url):
    _seed_rows(client, db_conn, seed)
    data, report = vault_export.build_snapshot(migrated_db_url, ["orders"])
    lite = sqlite3.connect(":memory:")
    lite.deserialize(data)
    for table in ("fact_orders", "dim_party", "dim_date"):
        total = sum(vault_export.row_digest(row) for row in lite.execute(f'SELECT * FROM "{table}"')) % vault_export.MOD
        assert f"{total:064x}" == report["tables"][table]["digest"]
    meta = dict(lite.execute("SELECT key, value FROM _vault_meta").fetchall())
    assert meta["format"] == vault_export.PAYLOAD_FORMAT and meta["groups"] == '["orders"]' and meta["last_migration"].endswith(".sql")
    assert "fact_payment_txn" not in {r[0] for r in lite.execute("SELECT name FROM sqlite_master")}   # group not asked for


def test_group_choice_limits_tables():
    assert "fact_payment_txn" not in vault_export.tables_for(["orders"])
    assert {"dim_party", "dim_date"} <= set(vault_export.tables_for(["decor"]))    # reference data always travels


# ---------------------------------------------------------------- the gate and the routes
@pytest.fixture()
def vault_client(client, monkeypatch, tmp_path):
    priv, pub = vault_ref.new_keypair()
    monkeypatch.setenv("VAULT_EXPORT_ENABLED", "1")
    monkeypatch.setenv("VAULT_PUBLIC_KEY", base64.b64encode(pub).decode())
    monkeypatch.setenv("VAULT_EXPORT_PASSWORD_HASH", auth.hash_password(GATE_PASSWORD))
    monkeypatch.setenv("VAULT_EXPORT_TOTP_SECRET", TOTP_SECRET)
    monkeypatch.setenv("VAULT_FINGERPRINT_KEY", FP_KEY)
    monkeypatch.setattr(vault_export, "EXPORT_DIR", tmp_path / "vault")
    vault_export.reset_replay_guard()
    auth._fails.clear()
    vault_export._jobs.clear()
    before = len(app.router.routes)
    app.include_router(vault_export.router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    at = next((i for i, r in enumerate(app.router.routes) if type(r).__name__ == "Mount"), len(app.router.routes))
    app.router.routes[at:at] = added          # same place main.py puts them: before the catch-all static mount
    # The gate's clock is a fake that moves on 30 seconds with every request body, so a test can pass the gate many times
    # (each authenticator code works once, and the real clock only allows three codes at a time).
    clock = {"t": time.time()}
    monkeypatch.setattr(vault_export, "time", types.SimpleNamespace(time=lambda: clock["t"]))

    def body(password=GATE_PASSWORD, code=None, **extra):
        clock["t"] += 30
        if code is None:
            code = vault_export._totp(TOTP_SECRET, int(clock["t"] // 30))
        return {"password": password, "totp": code, **extra}
    yield client, priv, body
    app.router.routes[:] = [r for r in app.router.routes if not any(r is a for a in added)]
    auth._fails.clear()
    vault_export.reset_replay_guard()


def _wait_ready(client, h, job):
    for _ in range(120):
        s = client.get(f"/admin/vault/export/{job}", headers=h).json()
        if s["state"] != "running":
            return s
        time.sleep(0.25)
    raise AssertionError("export did not finish")


def test_settings_decide_whether_the_routes_exist(monkeypatch):
    for k in ("VAULT_EXPORT_ENABLED", "VAULT_PUBLIC_KEY", "VAULT_EXPORT_PASSWORD_HASH", "VAULT_EXPORT_TOTP_SECRET", "VAULT_FINGERPRINT_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert vault_export.enabled() is False
    monkeypatch.setenv("VAULT_EXPORT_ENABLED", "1")
    assert vault_export.enabled() is False                                  # switched on but nothing configured: still off
    _, pub = vault_ref.new_keypair()
    monkeypatch.setenv("VAULT_PUBLIC_KEY", base64.b64encode(pub[:-1]).decode())
    monkeypatch.setenv("VAULT_EXPORT_PASSWORD_HASH", "x")
    monkeypatch.setenv("VAULT_EXPORT_TOTP_SECRET", TOTP_SECRET)
    monkeypatch.setenv("VAULT_FINGERPRINT_KEY", FP_KEY)
    assert any("VAULT_PUBLIC_KEY" in p for p in vault_export.problems())    # a damaged key is caught at startup
    monkeypatch.setenv("VAULT_PUBLIC_KEY", base64.b64encode(pub).decode())
    assert vault_export.enabled() is True
    import app.main
    assert not any(r.path.startswith("/admin/vault") for r in app.main.app.router.routes)   # not mounted in a normal start


def test_only_admin_and_only_with_password_and_code(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin, shop = login(client, db_conn, "admin"), login(client, db_conn, "shop")
    assert client.post("/admin/vault/export", json=body(), headers=shop).status_code == 403
    assert client.post("/admin/vault/export", json=body()).status_code == 401
    assert client.post("/admin/vault/export", json=body(password="wrong"), headers=admin).status_code == 403
    assert client.post("/admin/vault/export", json=body(code="123456"), headers=admin).status_code == 403
    assert client.post("/admin/vault/export", json={"password": GATE_PASSWORD}, headers=admin).status_code == 422
    page = client.get("/admin/vault/page")                                    # a typed address carries no login header, so the form must open
    assert page.status_code == 200 and "Export password" in page.text and page.headers["cache-control"] == "no-store"
    assert "fact_orders" not in page.text                                      # it is only a form: no data in it
    assert "<script>" not in page.text and "<style>" not in page.text          # the main app forbids inline script and style, so they are files
    js, css = client.get("/admin/vault/page.js"), client.get("/admin/vault/page.css")
    assert (js.status_code, js.headers["content-type"].split(";")[0]) == (200, "text/javascript") and "sessionStorage" in js.text
    assert (css.status_code, css.headers["content-type"].split(";")[0]) == (200, "text/css")
    assert 'src="/admin/vault/page.js"' in page.text and 'href="/admin/vault/page.css"' in page.text
    assert client.get("/admin/vault/batches").status_code == 401               # everything it calls is still gated
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM admin_audit_log WHERE action = 'vault_export_denied'")
        assert cur.fetchone()[0] >= 2                                       # every refusal is recorded


def test_gate_locks_after_repeated_failures(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    for _ in range(5):
        assert client.post("/admin/vault/plan", json=body(password="nope"), headers=admin).status_code == 403
    assert client.post("/admin/vault/plan", json=body(), headers=admin).status_code == 429   # even the right answers are refused now


def test_plan_lists_tables_and_what_never_goes(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    r = client.post("/admin/vault/plan", json=body(groups=["money"]), headers=admin)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert "fact_payment_txn" in j["tables"] and "fact_orders" not in j["tables"]
    assert "fact_attendance" in j["never_exported"] and "dim_att_employee" in j["never_exported"]
    assert client.post("/admin/vault/plan", json=body(groups=["attendance"]), headers=admin).status_code == 422


def test_full_export_downloads_decrypts_and_leaves_nothing_behind(vault_client, db_conn, seed, tmp_path):
    client, priv, body = vault_client
    admin = login(client, db_conn, "admin")
    sl = _seed_rows(client, db_conn, seed)
    job = client.post("/admin/vault/export", json=body(), headers=admin).json()["job"]
    s = _wait_ready(client, admin, job)
    assert s["state"] == "ready" and s["summary"]["fact_orders"] >= 1
    on_disk = list((tmp_path / "vault").glob("*.bin"))
    assert len(on_disk) == 1
    raw = on_disk[0].read_bytes()
    assert b"SQLite format" not in raw and b"fact_orders" not in raw and b"vault test" not in raw   # ciphertext only, even on disk
    d = client.get(f"/admin/vault/export/{job}/download", headers=admin)
    assert d.status_code == 200 and d.headers["cache-control"] == "no-store"
    assert hashlib.sha256(d.content).hexdigest() == s["sha256"] and d.content == raw
    plain = vault_ref.decrypt_export(d.content, priv)
    assert plain[:15] == b"SQLite format 3"
    lite = sqlite3.connect(":memory:")
    lite.deserialize(plain)
    assert lite.execute("SELECT count(*) FROM fact_orders WHERE sl_no = ?", (sl,)).fetchone()[0] == 1
    assert not list((tmp_path / "vault").glob("*.bin"))                     # the server's copy is deleted after sending
    assert client.get(f"/admin/vault/export/{job}/download", headers=admin).status_code == 404
    with db_conn.cursor() as cur:
        cur.execute("SELECT action, details FROM admin_audit_log WHERE action LIKE 'vault_export_%%' ORDER BY audit_key DESC LIMIT 4")
        acts = {a: d for a, d in cur.fetchall()}
    assert {"vault_export_started", "vault_export_ready", "vault_export_downloaded"} <= set(acts)
    assert acts["vault_export_ready"]["sha256"] == s["sha256"] and acts["vault_export_ready"]["export_id"] == s["export_id"]
    assert json.dumps(acts).count("vault test") == 0                        # the log holds ids and counts, never data


def test_a_second_export_while_one_runs_is_refused(vault_client, db_conn, seed):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    vault_export._jobs["busy"] = {"state": "running", "created": time.time(), "by": "x"}
    assert client.post("/admin/vault/export", json=body(), headers=admin).status_code == 409
    vault_export._jobs.pop("busy")


def test_failed_export_never_shows_the_error_text(vault_client, db_conn, seed, monkeypatch):
    client, _, body = vault_client
    admin = login(client, db_conn, "admin")
    monkeypatch.setattr(vault_export, "build_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret row 12345")))
    job = client.post("/admin/vault/export", json=body(), headers=admin).json()["job"]
    s = _wait_ready(client, admin, job)
    assert s["state"] == "failed" and s["error"] == "RuntimeError" and "12345" not in json.dumps(s)
