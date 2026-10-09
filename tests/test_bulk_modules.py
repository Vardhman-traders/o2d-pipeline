# ruff: noqa: F811
"""Bulk upload for Payments, Delegation tasks and Purchase entries: templates, validation messages, import, undo."""
import uuid

from tests.test_admin_tools import client, confirm, d, login, seed, upload  # noqa: F401  (fixtures + helpers)


def _count(db_conn, table, batch_id):
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table} WHERE import_batch_id = %s", (batch_id,))
        return cur.fetchone()[0]


def test_templates_open_for_every_module_and_stay_admin_only(client, db_conn, seed):
    admin, shop = login(client, db_conn), login(client, db_conn, "shop")
    for entity in ("payments", "tasks", "purchases"):
        assert client.get(f"/admin/bulk/{entity}/template.csv", headers=admin).status_code == 200
        assert client.get(f"/admin/bulk/{entity}/template.xlsx", headers=admin).status_code == 200
        assert client.get(f"/admin/bulk/{entity}/template.csv", headers=shop).status_code == 403


def test_payments_validate_import_and_undo(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:5]
    raw = ("Date,Type,Company,Party,Mode,Amount,Account Name,Invoice No,Remarks\n"
           f"{d(-1)},Payment Received,VT,Party {u},Cash,2500,,INV-1,ok\n"
           f"{d(-2)},Expense,VT,,,100,,,mode defaults to Cash\n"
           f"{d(-1)},Nonsense,VT,,Cash,10,,,\n"
           f"{d(-1)},Expense,VT,,Cash,0,,,\n"
           f"31-02-2026,Expense,VT,,Cash,5,,,\n").encode()
    rep = upload(client, admin, "payments", raw)
    assert rep["total"] == 5 and rep["valid"] == 2
    errs = " | ".join(e["error"] for e in rep["errors"])
    for needle in ("Type 'Nonsense' not found", "must be a number above 0", "is not a date"):
        assert needle in errs
    res = confirm(client, admin, "payments", rep["rows"])
    assert _count(db_conn, "fact_payment_txn", res["batch_id"]) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, is_backdated FROM fact_payment_txn WHERE import_batch_id = %s", (res["batch_id"],))
        assert {(r[0], r[1]) for r in cur.fetchall()} == {("Approved", True)}
    assert client.post(f"/admin/bulk/batches/{res['batch_id']}/undo", headers=admin).json()["removed"] == 2


def test_tasks_validate_import_and_undo(client, db_conn, seed):
    admin = login(client, db_conn)
    login(client, db_conn, "shop")   # a staff account to assign to
    with db_conn.cursor() as cur:
        cur.execute("SELECT username FROM dim_user WHERE role = 'shop' ORDER BY user_key DESC LIMIT 1")
        who = cur.fetchone()[0]
    raw = ("Staff Username,Task,Assigned Date,Deadline\n"
           f"{who},Count stock,,{d(2)}\n"
           f"nobody.here,Do a thing,,{d(2)}\n"
           f"{who},Late deadline,{d(1)},{d(-1)}\n").encode()
    rep = upload(client, admin, "tasks", raw)
    assert rep["total"] == 3 and rep["valid"] == 1
    errs = " | ".join(e["error"] for e in rep["errors"])
    assert "not found" in errs and "Deadline is before the Assigned Date" in errs
    res = confirm(client, admin, "tasks", rep["rows"])
    assert _count(db_conn, "fact_task", res["batch_id"]) == 1
    assert client.post(f"/admin/bulk/batches/{res['batch_id']}/undo", headers=admin).json()["removed"] == 1


def test_purchases_validate_duplicates_import_and_undo(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:5]
    head = "Site,Vendor,Material Received Date,Invoice Date,Invoice No,Invoice Amount,Remarks\n"
    raw = (head + f"Godown,Vendor {u},{d(-1)},{d(-1)},B-{u},5000,\n"
           f"Shop,Vendor {u},,,,,\n"
           f"Warehouse,Vendor {u},,,,,\n"
           f"Godown,Vendor {u},{d(-1)},{d(-1)},B-{u},5000,again\n"
           f"Godown,,,,,,\n").encode()
    rep = upload(client, admin, "purchases", raw)
    assert rep["total"] == 5 and rep["valid"] == 2
    errs = " | ".join(e["error"] for e in rep["errors"])
    for needle in ("Site 'Warehouse' not found", "Duplicate of row", "Vendor is required"):
        assert needle in errs
    res = confirm(client, admin, "purchases", rep["rows"])
    assert _count(db_conn, "fact_purchase_entry", res["batch_id"]) == 2
    again = upload(client, admin, "purchases", (head + f"Godown,Vendor {u},{d(-1)},{d(-1)},B-{u},5000,\n").encode())
    assert "already exists" in again["errors"][0]["error"]
    assert client.post(f"/admin/bulk/batches/{res['batch_id']}/undo", headers=admin).json()["removed"] == 2
