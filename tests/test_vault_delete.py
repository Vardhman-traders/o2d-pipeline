# ruff: noqa: E501
"""The guarded delete: only what was exported can go, only after a confirmed and unchanged export, all or nothing, and nothing outside
its scope may change. Runs in its OWN fresh database (it deletes for real). Throwaway keys, fake photo storage."""
import base64
import io
import time
import types
import uuid
from datetime import timedelta

import psycopg2
import psycopg2.extras
import pytest

from app import attachments, auth, vault_delete, vault_export
from app.main import app
from tests import row_factory as rf
from tests import vault_ref
from tests.conftest import _create_db, _drop_db, run_migrations

GATE_PASSWORD = "gate-pass-for-delete-tests"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
FP_KEY = "fingerprint-key-for-delete-tests-0123"
BOOKKEEPING = vault_delete.BOOKKEEPING


# ---------------------------------------------------------------- a fresh database and a fake photo store
@pytest.fixture(scope="module")
def ddb():
    name, url = _create_db()
    assert run_migrations(url) == 0
    yield url
    _drop_db(name)


class FakeStore:
    def __init__(self):
        self.objects, self.deleted, self.fail = {}, [], set()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_objects(self, Bucket, Delete, **kw):
        keys = [o["Key"] for o in Delete["Objects"]]
        errors = [{"Key": k, "Code": "AccessDenied"} for k in keys if k in self.fail]
        self.deleted += [k for k in keys if k not in self.fail]
        return {"Deleted": [], "Errors": errors}


@pytest.fixture()
def conn(ddb):
    c = psycopg2.connect(ddb)
    c.autocommit = True
    yield c
    c.close()


def cur_of(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def all_counts(conn) -> dict:
    cur = cur_of(conn)
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    out = {}
    for r in cur.fetchall():
        cur.execute(f'SELECT count(*) AS n FROM "{r["table_name"]}"')
        out[r["table_name"]] = cur.fetchone()["n"]
    return out


def wipe(conn):
    cur = cur_of(conn)
    for t in ("vault_delete_files", "vault_delete_plan", "vault_row_state", "vault_batch"):
        cur.execute(f'DELETE FROM "{t}"')
    for t in ("fact_checklist_occurrence", "fact_weekly_plan", "fact_task", "dim_checklist", "att_punch_check", "fact_att_advance",
              "fact_attendance", "att_employee_face", "att_holiday", "dim_att_employee"):      # the "must survive" samples of earlier tests
        cur.execute(f'DELETE FROM "{t}"')
    for t in vault_delete.delete_order(cur):
        cur.execute(f'DELETE FROM "{t}"')
    cur.execute('DELETE FROM "file_attachment"')                                             # including the attendance / face / delegation files


OUTSIDE = ["dim_att_employee", "fact_attendance", "att_employee_face", "att_holiday", "att_punch_check", "fact_att_advance", "fact_task", "dim_checklist",
           "fact_weekly_plan", "fact_checklist_occurrence"]


@pytest.fixture()
def data(conn):
    """One realistic row in every table that gets deleted, and in the tables that must survive (attendance, tasks, checklists ...)."""
    wipe(conn)
    cur = cur_of(conn)
    mod = {m: rf.make_row(cur, "file_attachment", {"module": m, "object_key": f"{m}/{uuid.uuid4().hex}.jpg", "sha256": None})
           for m in ("challan", "purchase", "decor", "attendance", "attface", "delegation")}
    hints = {("fact_challan_upload", "attachment_key"): mod["challan"]["attachment_key"], ("fact_decor_photo", "attachment_key"): mod["decor"]["attachment_key"],
             ("fact_attendance", "photo_in_key"): mod["attendance"]["attachment_key"], ("att_employee_face", "attachment_key"): mod["attface"]["attachment_key"]}
    for t in vault_delete.scope_tables():
        if t != "file_attachment":
            rf.make_row(cur, t, hints=hints)
    cur.execute("SELECT order_key FROM fact_orders LIMIT 1")
    ok_ = cur.fetchone()["order_key"]
    rf.make_row(cur, "order_attachments", {"object_key": f"order/{uuid.uuid4().hex}.jpg"}, hints=hints)
    for t in OUTSIDE:
        rf.make_row(cur, t, hints=hints)
    rf.make_row(cur, "dim_person")
    cur.execute("SELECT object_key FROM order_attachments UNION SELECT object_key FROM file_attachment WHERE module = ANY(%s)", (["challan", "purchase", "decor"],))
    scope_objects = {r["object_key"] for r in cur.fetchall()}
    cur.execute("SELECT object_key FROM file_attachment WHERE module = ANY(%s)", (["attendance", "attface", "delegation"],))
    outside_objects = {r["object_key"] for r in cur.fetchall()}
    return {"objects": scope_objects | outside_objects, "scope_objects": scope_objects, "outside_objects": outside_objects, "order_key": ok_, "attachments": mod}


# ---------------------------------------------------------------- the app, with export and delete switched on
@pytest.fixture()
def dv(ddb, conn, monkeypatch, tmp_path, data):
    from fastapi.testclient import TestClient
    store = FakeStore()
    with cur_of(conn) as cur:
        cur.execute("SELECT object_key FROM file_attachment UNION SELECT object_key FROM order_attachments")
        store.objects = {r["object_key"]: (b"PHOTO-" + r["object_key"].encode()) * 20 for r in cur.fetchall()}
    for k, v in {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "b", "R2_SECRET_ACCESS_KEY": "c", "R2_BUCKET": "bucket"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(attachments, "_client", store, raising=False)
    priv, pub = vault_ref.new_keypair()
    for k, v in {"DATABASE_URL": ddb, "JWT_SECRET": "x" * 48, "VAULT_EXPORT_ENABLED": "1", "VAULT_DELETE_ENABLED": "1",
                 "VAULT_DELETE_COOLING_HOURS": "0", "VAULT_PUBLIC_KEY": base64.b64encode(pub).decode(),
                 "VAULT_EXPORT_PASSWORD_HASH": auth.hash_password(GATE_PASSWORD), "VAULT_EXPORT_TOTP_SECRET": TOTP_SECRET,
                 "VAULT_FINGERPRINT_KEY": FP_KEY}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(vault_export, "EXPORT_DIR", tmp_path / "vault")
    vault_export.reset_replay_guard()
    auth._fails.clear()
    vault_export._jobs.clear()
    clock = {"t": time.time()}
    monkeypatch.setattr(vault_export, "time", types.SimpleNamespace(time=lambda: clock["t"]))
    with cur_of(conn) as cur:
        cur.execute("SELECT 1 FROM dim_user WHERE username = 'delete_admin'")
        if not cur.fetchone():                                              # the audit trail refers to it, so it stays between tests
            cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) VALUES ('delete_admin', %s, 'admin', 'A', false)",
                        (auth.hash_password("Admin-Password-1"),))
    before = len(app.router.routes)
    app.include_router(vault_export.router)
    app.include_router(vault_delete.router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    at = next((i for i, r in enumerate(app.router.routes) if type(r).__name__ == "Mount"), len(app.router.routes))
    app.router.routes[at:at] = added
    with TestClient(app, base_url="https://testserver") as client:
        tok = client.post("/auth/login", json={"username": "delete_admin", "password": "Admin-Password-1"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        def gate(**extra):
            clock["t"] += 30
            return {"password": GATE_PASSWORD, "totp": vault_export._totp(TOTP_SECRET, int(clock["t"] // 30)), **extra}
        yield types.SimpleNamespace(client=client, h=h, gate=gate, store=store, data=data, conn=conn)
    app.router.routes[:] = [r for r in app.router.routes if not any(r is a for a in added)]
    auth._fails.clear()
    vault_export.reset_replay_guard()


def export_and_confirm(dv, **extra):
    st = dv.client.post("/admin/vault/export", json=dv.gate(mode="full", **extra), headers=dv.h)
    assert st.status_code == 200, st.text
    job = st.json()["job"]
    for _ in range(300):
        s = dv.client.get(f"/admin/vault/export/{job}", headers=dv.h).json()
        if s["state"] != "running":
            break
        time.sleep(0.2)
    assert s["state"] == "ready", s
    r = dv.client.post(f"/admin/vault/batches/{s['batch_no']}/confirm", json=dv.gate(), headers=dv.h)
    assert r.status_code == 200, r.text
    return s


def plan(dv, **extra):
    r = dv.client.post("/admin/vault/delete/plan", json=dv.gate(**extra), headers=dv.h)
    assert r.status_code == 200, r.text
    return r.json()


def execute(dv, p, **over):
    body = dv.gate(plan_id=p["plan_id"], phrase=vault_delete.PHRASE, backups_understood=True, **over)
    return dv.client.post("/admin/vault/delete/execute", json=body, headers=dv.h)


# ---------------------------------------------------------------- scope and structure
def test_scope_is_only_ever_what_the_export_covers(conn):
    scope = set(vault_delete.scope_tables())
    exported = set(vault_export.tables_for(vault_export.SELECTABLE_GROUPS))
    assert scope <= exported, "a table would be deleted that the export does not cover"
    for kept in ("dim_party", "dim_decor_client", "dim_person", "dim_user", "dim_date", "dim_company", "fact_attendance", "att_holiday",
                 "dim_att_employee", "fact_att_advance", "app_config", "page_access", "fact_task", "v_orders_archive", "daily_report_run"):
        assert kept not in scope, kept
    assert len(scope) == 21 and "file_attachment" in scope


def test_children_are_deleted_before_their_parents(conn):
    order = vault_delete.delete_order(cur_of(conn))
    assert sorted(order) == sorted(vault_delete.scope_tables())
    for child, parent in (("order_event", "fact_orders"), ("order_doc_read", "order_attachments"), ("order_attachments", "fact_orders"),
                          ("payment_push", "fact_payment_txn"), ("payment_push", "fact_orders"), ("fact_challan_upload", "file_attachment"),
                          ("fact_decor_photo", "file_attachment"), ("fact_decor_photo", "fact_decor_payment")):
        assert order.index(child) < order.index(parent), (child, parent)


def test_nothing_outside_the_scope_points_at_data_that_would_go(conn, data):
    assert vault_delete.outside_references(cur_of(conn)) == []          # attendance photos point at attendance files, which stay


# ---------------------------------------------------------------- switches and the gate
def test_delete_exists_only_when_its_own_switch_is_on(monkeypatch):
    for k in ("VAULT_EXPORT_ENABLED", "VAULT_DELETE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert vault_delete.enabled() is False
    monkeypatch.setenv("VAULT_DELETE_ENABLED", "1")
    assert vault_delete.enabled() is False                                 # export is not configured, so no delete either
    import app.main
    assert not any(r.path.startswith("/admin/vault/delete") for r in app.main.app.router.routes)


def test_form_opens_by_address_and_assets_are_files(dv):
    page = dv.client.get("/admin/vault/delete/page")
    assert page.status_code == 200 and "<script>" not in page.text and "<style>" not in page.text and "fact_orders" not in page.text
    assert dv.client.get("/admin/vault/delete/page.js").status_code == 200
    assert dv.client.get("/admin/vault/delete/page.css").status_code == 200
    assert dv.client.get("/admin/vault/delete/status").status_code == 401   # everything real is gated


def test_every_action_needs_admin_password_and_a_code(dv, conn):
    shop = {"Authorization": "Bearer nope"}
    assert dv.client.post("/admin/vault/delete/plan", json=dv.gate(), headers=shop).status_code == 401
    assert dv.client.post("/admin/vault/delete/plan", json=dv.gate(password="wrong"), headers=dv.h).status_code == 403
    assert dv.client.post("/admin/vault/delete/plan", json={**dv.gate(), "totp": "000000"}, headers=dv.h).status_code == 403
    assert dv.client.post("/admin/vault/delete/execute", json=dv.gate(password="wrong", plan_id="x", phrase="y"), headers=dv.h).status_code == 403
    assert all_counts(conn)["fact_orders"] >= 1                              # and nothing happened


# ---------------------------------------------------------------- blockers
def test_blocked_without_a_confirmed_export(dv):
    p = plan(dv)
    assert p["can_execute"] is False and "confirmed" in p["blockers"][0]
    assert execute(dv, p).status_code == 409


def test_blocked_when_anything_in_scope_changed_after_the_export(dv, conn):
    export_and_confirm(dv)
    assert plan(dv)["can_execute"] is True
    cur = cur_of(conn)
    cur.execute("UPDATE fact_payment_txn SET remarks = 'edited after export' WHERE txn_key = (SELECT min(txn_key) FROM fact_payment_txn)")
    p = plan(dv)
    assert p["can_execute"] is False and "fact_payment_txn" in p["blockers"][0] and "1 changed" in p["blockers"][0]
    rf.make_row(cur, "fact_expense_entry")                                    # a brand-new row is also a change
    assert "fact_expense_entry" in plan(dv)["blockers"][0]


def test_a_changed_reference_list_only_warns(dv, conn):
    export_and_confirm(dv)
    cur_of(conn).execute("UPDATE dim_party SET party_name = party_name || ' (renamed)' WHERE party_key = (SELECT min(party_key) FROM dim_party)")
    p = plan(dv)
    assert p["can_execute"] is True and any("dim_party" in w for w in p["warnings"])


def test_files_must_have_been_exported_intact_before_their_files_are_deleted(dv):
    export_and_confirm(dv, attachments=False)                                  # a confirmed export WITHOUT photos
    p = plan(dv)
    assert p["can_execute"] is False and "photos and documents were not part of" in p["blockers"][0]
    assert plan(dv, delete_files=False)["can_execute"] is True                  # keeping the files is fine
    # a file missing from storage at export time
    gone = sorted(dv.data["scope_objects"])[0]
    del dv.store.objects[gone]
    export_and_confirm(dv)
    p = plan(dv)
    assert p["can_execute"] is False and "missing" in p["blockers"][0]


def test_blocked_when_another_table_depends_on_what_would_go(dv, conn):
    export_and_confirm(dv)
    cur = cur_of(conn)
    decor = dv.data["attachments"]["decor"]["attachment_key"]
    cur.execute("UPDATE fact_attendance SET photo_out_key = %s", (decor,))      # an attendance record now points at a decor photo
    p = plan(dv)
    assert p["can_execute"] is False and "fact_attendance" in " ".join(p["blockers"])


# ---------------------------------------------------------------- execute guards
def test_execute_needs_phrase_tick_valid_plan_and_cooling_off(dv, conn, monkeypatch):
    export_and_confirm(dv)
    p = plan(dv)
    assert dv.client.post("/admin/vault/delete/execute", json=dv.gate(plan_id=p["plan_id"], phrase="delete", backups_understood=True), headers=dv.h).status_code == 422
    assert dv.client.post("/admin/vault/delete/execute", json=dv.gate(plan_id=p["plan_id"], phrase=vault_delete.PHRASE, backups_understood=False), headers=dv.h).status_code == 422
    assert dv.client.post("/admin/vault/delete/execute", json=dv.gate(plan_id="nope", phrase=vault_delete.PHRASE, backups_understood=True), headers=dv.h).status_code == 404
    monkeypatch.setenv("VAULT_DELETE_COOLING_HOURS", "24")
    r = execute(dv, p)
    assert r.status_code == 409 and "Cooling-off" in r.json()["detail"]
    cur = cur_of(conn)
    cur.execute("UPDATE vault_delete_plan SET created_at = now() - interval '8 days' WHERE plan_id = %s", (p["plan_id"],))
    assert "too old" in execute(dv, p).json()["detail"]
    assert all_counts(conn)["fact_orders"] >= 1


# ---------------------------------------------------------------- the delete itself
def test_delete_removes_exactly_the_scope_and_nothing_else(dv, conn):
    export_and_confirm(dv)
    p = plan(dv)
    assert p["can_execute"] and set(p["will_delete"]) == set(vault_delete.scope_tables()) and all(n >= 1 for n in p["will_delete"].values())
    assert set(p["will_not_touch"]).isdisjoint(vault_delete.scope_tables())
    before = all_counts(conn)
    outside_attachments = sum(1 for m in ("attendance", "attface", "delegation"))
    r = execute(dv, p)
    assert r.status_code == 200, r.text
    j = r.json()
    after = all_counts(conn)
    for t in vault_delete.scope_tables():
        assert after[t] == (outside_attachments if t == "file_attachment" else 0), t
        assert j["deleted"][t] == before[t] - after[t]
    for t in before:                                                       # EVERY other table has exactly the rows it had
        if t not in vault_delete.scope_tables() and t not in BOOKKEEPING:
            assert after[t] == before[t], t
    for kept in ("dim_party", "dim_decor_client", "dim_person", "fact_attendance", "att_employee_face", "dim_user", "fact_task", "dim_date"):
        assert after[kept] == before[kept] >= 1, kept
    assert j["rows_total"] == sum(j["deleted"].values()) and isinstance(j["space"]["scope_tables_bytes_after"], int)
    cur = cur_of(conn)
    cur.execute("SELECT status, result FROM vault_delete_plan WHERE plan_id = %s", (p["plan_id"],))
    row = cur.fetchone()
    assert row["status"] == "executed" and row["result"]["deleted"] == j["deleted"]
    cur.execute("SELECT count(*) AS n FROM admin_audit_log WHERE action = 'vault_delete_executed' AND target = %s", (p["plan_id"],))
    assert cur.fetchone()["n"] == 1
    assert execute(dv, p).status_code == 409                               # a plan is single use


def test_stored_files_of_the_scope_are_removed_and_only_those(dv, conn):
    export_and_confirm(dv)
    p = plan(dv)
    scope_keys, outside = dv.data["scope_objects"], dv.data["outside_objects"]
    assert len(scope_keys) >= 4 and len(outside) == 3
    assert p["stored_files"] == len(scope_keys) and p["delete_stored_files"] is True
    j = execute(dv, p).json()
    assert j["stored_files"] == {"deleted": len(scope_keys), "failed": 0}
    assert set(dv.store.deleted) == scope_keys and not (set(dv.store.deleted) & outside)


def test_files_that_could_not_be_removed_are_listed_and_can_be_retried(dv, conn):
    export_and_confirm(dv)
    p = plan(dv)
    stuck = sorted(k for k in dv.data["objects"] if k.startswith("decor/"))[0]
    dv.store.fail = {stuck}
    j = execute(dv, p).json()
    assert j["stored_files"]["failed"] == 1 and j["stored_files"]["deleted"] >= 1
    assert stuck not in dv.store.deleted
    dv.store.fail = set()
    r = dv.client.post("/admin/vault/delete/retry-files", json=dv.gate(plan_id=p["plan_id"]), headers=dv.h)
    assert r.status_code == 200 and r.json() == {"deleted": 1, "failed": 0} and stuck in dv.store.deleted


def test_without_deleting_files_the_rows_go_but_the_stored_files_stay(dv, conn):
    export_and_confirm(dv)
    p = plan(dv, delete_files=False)
    assert p["delete_stored_files"] is False
    assert execute(dv, p).status_code == 200 and dv.store.deleted == []


def test_data_changed_between_preview_and_delete_means_nothing_is_deleted(dv, conn):
    export_and_confirm(dv)
    p = plan(dv)
    before = all_counts(conn)
    rf.make_row(cur_of(conn), "fact_extra_work")                          # somebody adds a row after the preview
    r = execute(dv, p)
    assert r.status_code == 409
    after = all_counts(conn)
    assert after["fact_orders"] == before["fact_orders"] and after["fact_extra_work"] == before["fact_extra_work"] + 1
    cur = cur_of(conn)
    cur.execute("SELECT status FROM vault_delete_plan WHERE plan_id = %s", (p["plan_id"],))
    assert cur.fetchone()["status"] == "failed"                            # that preview can never be used again
    assert dv.store.deleted == []


def test_any_failure_midway_rolls_everything_back(dv, conn, monkeypatch):
    export_and_confirm(dv)
    p = plan(dv)
    real = vault_delete.delete_order
    monkeypatch.setattr(vault_delete, "delete_order", lambda cur: real(cur)[:5] + ["no_such_table"] + real(cur)[5:])
    before = all_counts(conn)
    r = execute(dv, p)
    assert r.status_code == 500 and "nothing was deleted" in r.json()["detail"]
    assert all_counts(conn) == {**before, **{t: all_counts(conn)[t] for t in BOOKKEEPING}}      # every row of every table is still there
    assert dv.store.deleted == []


def test_a_surprise_change_elsewhere_stops_and_rolls_back(dv, conn):
    """A trigger that (wrongly) removes attendance data when orders are deleted: the row-count safety net must catch it."""
    export_and_confirm(dv)
    p = plan(dv)
    cur = cur_of(conn)
    cur.execute("CREATE FUNCTION trg_oops() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN DELETE FROM att_holiday; RETURN OLD; END $$")
    cur.execute("CREATE TRIGGER trg_oops AFTER DELETE ON fact_orders FOR EACH STATEMENT EXECUTE FUNCTION trg_oops()")
    before = all_counts(conn)
    try:
        r = execute(dv, p)
        assert r.status_code == 409 and "Safety stop" in r.json()["detail"] and "att_holiday" in r.json()["detail"]
        assert all_counts(conn) == before                                  # nothing at all was deleted
    finally:
        cur.execute("DROP TRIGGER trg_oops ON fact_orders")
        cur.execute("DROP FUNCTION trg_oops()")


def test_a_second_export_after_the_delete_is_all_removals(dv, conn):
    """Nothing is wrong with exporting again afterwards: the next batch simply says everything in scope was removed."""
    export_and_confirm(dv)
    assert execute(dv, plan(dv)).status_code == 200
    s = dv.client.post("/admin/vault/export", json=dv.gate(), headers=dv.h).json()["job"]
    for _ in range(300):
        st = dv.client.get(f"/admin/vault/export/{s}", headers=dv.h).json()
        if st["state"] != "running":
            break
        time.sleep(0.2)
    assert st["kind"] == "delta" and st["changes"]["fact_orders"]["removed"] >= 1 and "dim_party" not in st["changes"]
    assert timedelta(0) == timedelta(0)
