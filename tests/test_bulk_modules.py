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


# ---------------------------------------------------------------- VT Decor and Attendance
def test_decor_and_attendance_templates_are_admin_only(client, db_conn, seed):
    admin, shop = login(client, db_conn), login(client, db_conn, "shop")
    for entity in ("decor_clients", "decor_payments", "decor_expenses", "att_employees", "att_attendance", "att_advances"):
        assert client.get(f"/admin/bulk/{entity}/template.csv", headers=admin).status_code == 200
        assert client.get(f"/admin/bulk/{entity}/template.xlsx", headers=admin).status_code == 200
        assert client.get(f"/admin/bulk/{entity}/template.csv", headers=shop).status_code == 403


def test_decor_clients_payments_expenses_import_and_undo(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:5]
    head = "Client Name,Mobile,Product Type,Contract Amount,Start Date,Address,Product,Notes,Assigned To,Status\n"
    rep = upload(client, admin, "decor_clients", (head + f"Client {u},98{u[:4]}0001,Wallpaper,120000,{d(-3)},Pitampura,,,,Active\n"
                                                         f"Bad {u},98000,Marble,5,,,,,,\n"
                                                         f"Ghost {u},98000,Texture,5,,,,,nobody.here,\n"
                                                         f"Client {u},98{u[:4]}0001,Texture,1,,,,,,\n").encode())
    assert rep["total"] == 4 and rep["valid"] == 1
    errs = " | ".join(e["error"] for e in rep["errors"])
    for needle in ("Product Type 'Marble' not found", "is not a username", "Duplicate of row"):
        assert needle in errs
    clients = confirm(client, admin, "decor_clients", rep["rows"])
    assert _count(db_conn, "dim_decor_client", clients["batch_id"]) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT client_code, contract_amount, assigned_to FROM dim_decor_client WHERE import_batch_id = %s", (clients["batch_id"],))
        code, amount, assigned = cur.fetchone()
    assert float(amount) == 120000.0 and assigned == "owner"

    # payments and expenses attach to that client code; amounts are stored as typed (rupees, not divided by 100)
    pay = upload(client, admin, "decor_payments", ("Client Code,Stage,Amount,Mode,Date,Reference No,Remarks\n"
                                                  f"{code},Stage 1 - Visit,48000,UPI,{d(-2)},UTR1,adv\n"
                                                  f"{code},Old stage name,100,,,,\n"
                                                  "C99999,,100,Cash,,,\n"
                                                  f"{code},,0,Cash,,,\n").encode())
    assert pay["total"] == 4 and pay["valid"] == 2
    assert "Client Code 'C99999' not found" in " | ".join(e["error"] for e in pay["errors"])
    pays = confirm(client, admin, "decor_payments", pay["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT amount, stage_key IS NOT NULL, stage_text FROM fact_decor_payment WHERE import_batch_id = %s ORDER BY amount DESC", (pays["batch_id"],))
        assert [(float(a), k, t) for a, k, t in cur.fetchall()] == [(48000.0, True, None), (100.0, False, "Old stage name")]
    exp = upload(client, admin, "decor_expenses", (f"Client Code,Expense Type,Amount,Date,Notes\n{code},Materials,15000,{d(-1)},glue\n").encode())
    exps = confirm(client, admin, "decor_expenses", exp["rows"])
    assert _count(db_conn, "fact_decor_expense", exps["batch_id"]) == 1

    # the clients cannot be undone while payments / expenses hang on them; undo those first
    assert client.post(f"/admin/bulk/batches/{clients['batch_id']}/undo", headers=admin).status_code == 409
    assert client.post(f"/admin/bulk/batches/{exps['batch_id']}/undo", headers=admin).json()["removed"] == 1
    assert client.post(f"/admin/bulk/batches/{pays['batch_id']}/undo", headers=admin).json()["removed"] == 2
    assert client.post(f"/admin/bulk/batches/{clients['batch_id']}/undo", headers=admin).json()["removed"] == 1


def test_attendance_employees_records_and_advances_import_and_undo(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:6]
    name = f"Worker {u}"
    emp = upload(client, admin, "att_employees", (f"Employee Name,Designation,Monthly Salary,No WhatsApp\n{name},Packer,18000,No\n"
                                                  f"{name.upper()},Packer,,\n{name}2,Driver,-5,maybe\n").encode())
    assert emp["total"] == 3 and emp["valid"] == 1
    errs = " | ".join(e["error"] for e in emp["errors"])
    assert "Duplicate of row" in errs and "must be a number above 0" in errs and "must be Yes or No" in errs
    emps = confirm(client, admin, "att_employees", emp["rows"])
    assert _count(db_conn, "dim_att_employee", emps["batch_id"]) == 1

    head = "Employee Name,Date,Check In,Check Out,Location,Status\n"
    rec = upload(client, admin, "att_attendance", (head + f"{name},{d(-5)},09:05,18:30,Godown,\n"
                                                         f"{name},{d(-4)},10:45,,,\n"
                                                         f"{name},{d(-5)},09:00,,,\n"
                                                         f"{name},{d(-3)},18:00,09:00,,\n"
                                                         f"Nobody Here,{d(-3)},09:00,,,\n"
                                                         f"{name},{d(-2)},9:00 AM,5:00 PM,Moon,\n").encode())
    assert rec["total"] == 6 and rec["valid"] == 2
    errs = " | ".join(e["error"] for e in rec["errors"])
    for needle in ("Duplicate of row", "Check Out must be after Check In", "Employee Name 'Nobody Here' not found", "Location 'Moon' not found"):
        assert needle in errs
    recs = confirm(client, admin, "att_attendance", rec["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT final_status, hours_worked_min, wa_status FROM fact_attendance WHERE import_batch_id = %s ORDER BY work_date", (recs["batch_id"],))
        rows = cur.fetchall()
    assert rows[0] == ("Present", 565, "Done") and rows[1][2] == "Done" and rows[1][1] is None   # no WhatsApp for imported days
    # a day that already has attendance is rejected on the next upload
    again = upload(client, admin, "att_attendance", (head + f"{name},{d(-5)},09:00,,,\n").encode())
    assert "already has attendance" in again["errors"][0]["error"]

    adv = upload(client, admin, "att_advances", (f"Employee Name,Date,Amount,Note\n{name},{d(-1)},5000,festival\n{name},{d(-1)},0,\n").encode())
    assert adv["total"] == 2 and adv["valid"] == 1
    advs = confirm(client, admin, "att_advances", adv["rows"])
    assert _count(db_conn, "fact_att_advance", advs["batch_id"]) == 1

    # the employee cannot be removed while attendance and advances hang on them
    assert client.post(f"/admin/bulk/batches/{emps['batch_id']}/undo", headers=admin).status_code == 409
    assert client.post(f"/admin/bulk/batches/{advs['batch_id']}/undo", headers=admin).json()["removed"] == 1
    assert client.post(f"/admin/bulk/batches/{recs['batch_id']}/undo", headers=admin).json()["removed"] == 2
    assert client.post(f"/admin/bulk/batches/{emps['batch_id']}/undo", headers=admin).json()["removed"] == 1
