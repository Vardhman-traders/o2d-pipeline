# ruff: noqa: F811
"""Bulk upload for Payments, Delegation tasks and Purchase entries: templates, validation messages, import, undo."""
import uuid
from zoneinfo import ZoneInfo

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
    for entity in ("decor_clients", "decor_payments", "decor_expenses", "decor_items", "decor_photos", "decor_quotations", "att_employees", "att_attendance", "att_advances"):
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


def test_decor_import_accepts_the_old_sheets_layout_and_keeps_client_ids(client, db_conn, seed):
    """The old Master / PaymentsLog / ItemsLog sheets: legacy headers, Client ID kept, Added By, timestamps as dates, items leave the contract alone."""
    admin = login(client, db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(client_key), 0) FROM dim_decor_client")
        base = cur.fetchone()[0] + 500
    a, b, c = f"C{base + 1}", f"C{base + 2}", f"c{base + 3}"
    head = "ClientID,Name,Mobile,Address,Product,Amount,Date,Notes,Status,Assigned To,Added By,ProductType\n"
    rep = upload(client, admin, "decor_clients", (head +
        f"{a},Old Sharma,9811000091,Pitampura,Wall,100000,2026-03-04 10:15:00,,Active,,owner,\n"        # empty type = Wallpaper
        f"{b},Old Sharma,9811000091,Rohini,Paint,5000,{d(-9)},,Completed,owner,,Paint Work\n"            # same name+mobile, other ID: fine
        f"{a},Clash,9811000092,,,1,,,,,,Texture\n"                                                       # ID repeated in the file
        f"{c},Nobody,9811000093,,,1,,,,,ghost.user,Texture\n").encode())                                # Added By becomes Assigned To: not a member
    assert rep["total"] == 4 and rep["valid"] == 2, rep["errors"]
    errs = " | ".join(e["error"] for e in rep["errors"])
    assert "is also on row" in errs and "ghost.user" in errs
    done = confirm(client, admin, "decor_clients", rep["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT client_code, contract_amount, start_date, status, added_by, assigned_to, t.type_name FROM dim_decor_client x "
                    "JOIN dim_decor_product_type t ON t.type_key = x.product_type_key WHERE import_batch_id = %s ORDER BY client_key", (done["batch_id"],))
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [f"C{base + 1}", f"C{base + 2}"]
    assert rows[0][2].isoformat() == "2026-03-04" and rows[0][6] == "Wallpaper" and rows[0][4] == "owner"
    assert rows[1][3] == "Completed" and rows[1][5] == "owner"

    # a client added without an ID afterwards is given a code past the imported ones
    nxt = upload(client, admin, "decor_clients", (head + ",Fresh Client,9811000094,,,,,,,,,Texture\n").encode())
    nxt_done = confirm(client, admin, "decor_clients", nxt["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT client_key FROM dim_decor_client WHERE import_batch_id = %s", (nxt_done["batch_id"],))
        assert cur.fetchone()[0] > base + 2

    # payments, expenses and items use the old column names and a timestamp for the date
    pay = upload(client, admin, "decor_payments", ("Timestamp,ClientID,ClientName,Stage,Amount,Mode,Date,PNum,Remarks\n"
                                                  f"2026-03-05 11:00:00,{a},Old Sharma,Stage 1 - Visit,40000,Cash,05-03-2026 11:00,UTR9,adv\n"
                                                  f"2026-03-05 11:00:00,c{base + 1},Old Sharma,,500,Cash,,,\n").encode())
    assert pay["valid"] == 2, pay["errors"]
    pays = confirm(client, admin, "decor_payments", pay["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT ref_no, pay_date FROM fact_decor_payment WHERE import_batch_id = %s AND ref_no IS NOT NULL", (pays["batch_id"],))
        ref, day = cur.fetchone()
    assert ref == "UTR9" and day.isoformat() == "2026-03-05"
    itm = upload(client, admin, "decor_items", ("Timestamp,ClientID,ClientName,Product,Amount,Date,Notes,ItemIndex\n"
                                               f"x,{a},Old Sharma,Roll A,60000,04-03-2026,,\n"
                                               f"x,{a},Old Sharma,Roll B,40000,04-03-2026,,\n"
                                               f"x,{b},Old Sharma,Paint,5000,,,3\n").encode())
    assert itm["valid"] == 3, itm["errors"]
    items = confirm(client, admin, "decor_items", itm["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT c.client_code, i.item_index FROM fact_decor_item i JOIN dim_decor_client c USING (client_key) "
                    "WHERE i.import_batch_id = %s ORDER BY i.item_key", (items["batch_id"],))
        assert cur.fetchall() == [(a, 1), (a, 2), (b, 3)]
        cur.execute("SELECT contract_amount FROM dim_decor_client WHERE client_code = %s", (a,))
        assert float(cur.fetchone()[0]) == 100000.0                        # items did not change the contract
    ph = upload(client, admin, "decor_photos", ("timestamp,client_id,client_name,stage,caption,photo_url,drive_file_id\n"
                                               f"x,{a},Old Sharma,Expense Challan,[Challan] Material,https://lh3.googleusercontent.com/d/abc,abc\n"
                                               f"x,{a},Old Sharma,,Wall,not-a-link,\n").encode())
    assert ph["valid"] == 1 and "https://" in ph["errors"][0]["error"]
    phs = confirm(client, admin, "decor_photos", ph["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT kind, legacy_url FROM fact_decor_photo WHERE import_batch_id = %s", (phs["batch_id"],))
        assert cur.fetchone() == ("challan", "https://lh3.googleusercontent.com/d/abc")
    no = f"VT-9{base}"
    qt = upload(client, admin, "decor_quotations", ("timestamp,quot_id,created_by,client,mobile,addr,gstin,date,subtotal,cgst_pct,sgst_pct,grand,items_json\n"
        f'x,{no},owner,Priyanka,9999392616,Gurugram,,2026-09-20 00:00:00,227500,0,0,227500,"[{{""desc"":""Paint"",""qty"":6500,""mrp"":35,""amt"":227500}}]"\n'
        f"x,{no},owner,Dup,1,,,,1,0,0,1,[]\n"
        "x,VT-9999999,owner,Bad,1,,,,1,0,0,1,{not json\n").encode())
    assert qt["total"] == 3 and qt["valid"] == 1, qt["errors"]
    qts = confirm(client, admin, "decor_quotations", qt["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT quote_date, grand_total, items->0->>'desc' FROM fact_decor_quotation WHERE import_batch_id = %s", (qts["batch_id"],))
        row = cur.fetchone()
    assert row[0] == "2026-09-20" and float(row[1]) == 227500.0 and row[2] == "Paint"
    assert client.post(f"/admin/bulk/batches/{qts['batch_id']}/undo", headers=admin).status_code == 200
    assert client.post(f"/admin/bulk/batches/{phs['batch_id']}/undo", headers=admin).status_code == 200
    for batch in (items, pays, nxt_done, done):                            # undo in reverse order
        assert client.post(f"/admin/bulk/batches/{batch['batch_id']}/undo", headers=admin).status_code == 200


def test_payments_and_purchases_accept_the_old_sheets_layout(client, db_conn, seed):
    """Old Transactions sheet: Txn ID kept, saved amount not divided again, Pending/Rejected kept; old Purchase sheet: Drive photo links kept."""
    admin = login(client, db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(txn_key), 0) FROM fact_payment_txn")
        base = cur.fetchone()[0] + 700
        cur.execute("SELECT username FROM dim_user WHERE role = 'admin' ORDER BY user_key LIMIT 1")
        adm = cur.fetchone()[0]
    head = "TxnID,Date,Type,Company,Party,Mode,Amount,AccountName,Invoice,Remarks,CreatedBy,CreatedAt,Status,IsBackdated,ApprovedBy,ApprovedAt\n"
    rep = upload(client, admin, "payments", (head +
        f"TXN-{base:04d},{d(-9)},Payment Received,VT,Old Party,Cash,250.5,,INV1,old,{adm},{d(-9)} 10:00:00,Approved,No,{adm},{d(-9)} 10:05:00\n"
        f"TXN-{base + 1:04d},{d(-8)},Expense,VT,,PENDING,12,,,wait,{adm},{d(-8)} 09:00:00,Pending,No,,\n"
        f"TXN-{base + 2:04d},{d(-7)},Expense,VT,,Cash,5,,,no,{adm},{d(-7)} 09:00:00,Rejected,No,{adm},{d(-7)} 09:30:00\n"
        f"TXN-{base:04d},{d(-6)},Expense,VT,,Cash,5,,,dup id,,,,,,\n"
        f"TXN-9999999,{d(-6)},Expense,VT,,Cash,5,,,ghost,nobody.here,,,,,\n"
        f",{d(-5)},Expense,VT,,Cash,10000,,,typed,,,,,,\n").encode())
    assert rep["total"] == 6 and rep["valid"] == 4, rep["errors"]
    errs = " | ".join(e["error"] for e in rep["errors"])
    assert "also on row" in errs and "nobody.here" in errs
    done = confirm(client, admin, "payments", rep["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT txn_no, amount, status, created_at, approved_by_user_key IS NOT NULL, approved_at IS NOT NULL FROM fact_payment_txn "
                    "WHERE import_batch_id = %s ORDER BY txn_key", (done["batch_id"],))
        rows = cur.fetchall()
    assert [r[0] for r in rows[:3]] == [f"TXN-{base:04d}", f"TXN-{base + 1:04d}", f"TXN-{base + 2:04d}"]
    assert float(rows[0][1]) == 250.5 and rows[0][3].astimezone(ZoneInfo("Asia/Kolkata")).hour == 10      # saved amount kept as it is
    assert (rows[1][2], rows[1][4], rows[1][5]) == ("Pending", False, False)
    assert rows[2][2] == "Rejected" and rows[2][5]
    assert float(rows[3][1]) == 100.0 and rows[3][0] > f"TXN-{base + 2:04d}"                        # typed amount divided by 100, next free number
    assert client.post(f"/admin/bulk/batches/{done['batch_id']}/undo", headers=admin).status_code == 200

    pur = upload(client, admin, "purchases", ("Source,Vendor name,Material received date,Invoice date,Invoice number,Invoice amount,Remarks,Photos\n"
        f"godown,Old Vendor,{d(-9)},{d(-9)},OV-1,5000,x,https://drive.google.com/uc?export=view&id=a | https://drive.google.com/uc?export=view&id=b\n"
        f"shop,Old Vendor,{d(-9)},{d(-9)},OV-2,5,x,not-a-link\n").encode())
    assert pur["valid"] == 1 and "https://" in pur["errors"][0]["error"]
    purs = confirm(client, admin, "purchases", pur["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT legacy_photo_urls FROM fact_purchase_entry WHERE import_batch_id = %s", (purs["batch_id"],))
        assert cur.fetchone()[0].count("drive.google.com") == 2
    assert client.post(f"/admin/bulk/batches/{purs['batch_id']}/undo", headers=admin).status_code == 200


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


def test_party_list_upload_rejects_duplicates_and_undoes(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:6]
    assert client.get("/admin/bulk/parties/template.xlsx", headers=admin).status_code == 200
    lines = ["Party Name", f"Sharma {u}", f"Gupta {u}", f"sharma  {u}", "", "X" + "y" * 160]
    rep = upload(client, admin, "parties", ("\n".join(lines) + "\n").encode())
    assert rep["valid"] == 2
    errs = " | ".join(e["error"] for e in rep["errors"])
    assert "Duplicate of row" in errs and "longer than 150" in errs
    res = confirm(client, admin, "parties", rep["rows"])
    assert _count(db_conn, "dim_party", res["batch_id"]) == 2
    again = upload(client, admin, "parties", f"Party Name\nSHARMA {u}\n".encode())
    assert "already in the party list" in again["errors"][0]["error"]
    assert client.post(f"/admin/bulk/batches/{res['batch_id']}/undo", headers=admin).json()["removed"] == 2


def test_party_templates_payments_type_company_party_and_purchase_vendors(client, db_conn, seed):
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:6]
    # Payments > Parties: the Type, Company, Party template (Type and Company are checked when given)
    head = client.get("/admin/bulk/parties/template.csv", headers=admin).text.lstrip(chr(0xFEFF)).splitlines()[0]
    assert head.strip() == "Type,Company,Party"
    rep = upload(client, admin, "parties", f"Type,Company,Party\nExpense,VT,Rao {u}\n,,Mehta {u}\nNo such type,VT,Bad {u}\n".encode())
    assert rep["valid"] == 2 and "Type 'No such type' not found" in rep["errors"][0]["error"]
    res = confirm(client, admin, "parties", rep["rows"])
    assert _count(db_conn, "dim_party", res["batch_id"]) == 2
    # Purchase > Parties: one column, Party Name, into the vendor list
    head = client.get("/admin/bulk/vendors/template.csv", headers=admin).text.lstrip(chr(0xFEFF)).splitlines()[0]
    assert head.strip() == "Party Name"
    rep = upload(client, admin, "vendors", f"Party Name\nAsian {u}\nasian  {u}\nKansai {u}\n".encode())
    assert rep["valid"] == 2 and "Duplicate of row" in rep["errors"][0]["error"]
    res = confirm(client, admin, "vendors", rep["rows"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dim_party WHERE import_batch_id = %s AND party_kind = 'vendor' AND review_status = 'approved'", (res["batch_id"],))
        n = cur.fetchone()
    assert (n["count"] if isinstance(n, dict) else n[0]) == 2
    assert upload(client, admin, "vendors", f"Party Name\nASIAN {u}\n".encode())["errors"][0]["error"].endswith("already in the vendor list")
    assert client.post(f"/admin/bulk/batches/{res['batch_id']}/undo", headers=admin).json()["removed"] == 2
