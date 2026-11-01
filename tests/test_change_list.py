# ruff: noqa: F811, E501
"""The change list of 31 Oct: holidays, the employee's own salary view, Excel reports, Regular expenses edit / delete, the Payments bank card and
changed-entry notice, VT Decor entry photos and WhatsApp messages, the photo check, the godown's sent-on box and the Overview filter lists."""
import io
from datetime import date, time

from app import attachments, db, o2d_screens
from tests.test_attendance import own, owner_user
from tests.test_dashboard_batch import _party  # noqa: F401  (a row helper, reused)
from tests.test_decor import rpc as decor_rpc, staff
from tests.test_edit_access_2026_10 import _expense, days, grant  # noqa: F401  (fixture + helpers)
from tests.test_modules import FakeStore, user
from tests.test_o2d_cashier import _dc
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)
from tests.test_order_journey import _dispatch, _status, people  # noqa: F401  (fixture + helpers)
from tests.test_whatsapp_templates import groups, wa  # noqa: F401  (fixture + helper)


# ------------------------------------------------------------------ holidays
def test_a_holiday_is_not_an_absent_day(client, db_conn, seed):
    boss = owner_user(client, db_conn)
    admin = login(client, db_conn, "admin")
    name = "Holiday Person"
    client.post("/admin/masters/att_employee", json={"name": name, "is_active": True}, headers=admin)
    assert own(client, boss, "setEmployeeSalary", name, 30000)["action"] == "set"
    with db_conn.cursor() as cur:
        cur.execute("SELECT employee_key FROM dim_att_employee WHERE employee_name = %s", (name,))
        key = cur.fetchone()[0]
        for d in range(1, 31):              # June 2026: present every day except the Mondays (weekly off), the 9th and the 10th
            dt = date(2026, 6, d)
            if dt.weekday() == 0 or d in (9, 10):
                continue
            cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                        "VALUES (%s, %s, %s, %s, 'Present', 480) ON CONFLICT DO NOTHING", (key, dt, time(9, 0), time(18, 0)))
    db_conn.commit()
    before = own(client, boss, "getSalaryReport", name, 2026, 6)
    assert before["stats"]["absent"] == 2 and before["deductionAmount"] == 2000

    assert own(client, boss, "addHoliday", "2026-06-09", "Test holiday")["status"] == "ok"
    assert own(client, boss, "addHoliday", "not a date", "x")["status"] == "error" and own(client, boss, "addHoliday", "2026-06-12", "")["status"] == "error"
    after = own(client, boss, "getSalaryReport", name, 2026, 6)
    nine = next(d for d in after["days"] if d["day"] == 9)
    assert nine["status"] == "Holiday" and after["stats"]["holiday"] == 1 and after["stats"]["absent"] == 1
    assert after["deductionAmount"] == 1000 and after["netSalary"] == 29000                      # the holiday costs nothing
    assert after["countedDays"] == before["countedDays"] - 1                                    # and is not one of the days the % is out of
    month = own(client, boss, "getMonthlyReport", name, 2026, 6)
    assert next(d for d in month["days"] if d["day"] == 9)["status"] == "Holiday" and month["stats"]["holiday"] == 1 and month["stats"]["absent"] == 1
    assert [h["name"] for h in own(client, boss, "getHolidays", 2026)["holidays"] if h["date"] == "2026-06-09"] == ["Test holiday"]
    assert any(h["name"] == "Gandhi Jayanti" for h in own(client, boss, "getHolidays", 2026)["holidays"])      # the national days are there from the start
    # someone who works on a holiday is shown as they checked in
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                    "VALUES (%s, '2026-06-09', %s, %s, 'Present', 480)", (key, time(9, 0), time(18, 0)))
    db_conn.commit()
    assert next(d for d in own(client, boss, "getSalaryReport", name, 2026, 6)["days"] if d["day"] == 9)["status"] == "Present"
    assert own(client, boss, "deleteHoliday", "2026-06-09")["status"] == "ok" and own(client, boss, "deleteHoliday", "2026-06-09")["status"] == "error"
    view_only = owner_user(client, db_conn, view_only=True)
    assert client.post("/attendance/owner/rpc", json={"action": "addHoliday", "args": ["p", "2026-06-20", "x"]}, headers=view_only).status_code == 403


# ------------------------------------------------------------------ Excel reports
def test_every_report_can_be_downloaded_as_excel_too(client, people):
    from openpyxl import load_workbook
    h, _ = people
    for kind in ("godown-received-cash", "shop-received-cash", "received-bank", "received-other-all", "godown-pending", "receiving-pending", "cartage-by-employee"):
        r = client.get(f"/o2d/reports/{kind}.xlsx", headers=h["admin"])
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats"), kind
        assert r.headers["content-disposition"].endswith('.xlsx"')
        ws = load_workbook(io.BytesIO(r.content)).active
        assert ws["A1"].value and "Prepared by" in str(ws["A2"].value), kind
    ws = load_workbook(io.BytesIO(client.get("/o2d/reports/godown-received-cash.xlsx", headers=h["admin"]).content)).active
    heads = [c.value for row in ws.iter_rows(min_row=1, max_row=8) for c in row if c.value]
    assert "Delivery status" in heads and "Amount received (Rs)" in heads
    assert client.get("/o2d/reports/nonsense.xlsx", headers=h["admin"]).status_code == 404
    pdf = client.get("/o2d/reports/godown-pending.pdf", headers=h["admin"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")                              # the PDF is unchanged


# ------------------------------------------------------------------ Regular expenses: edit, and an administrator can delete
def test_regular_expense_entries_can_be_edited_and_an_admin_can_delete_them(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    pers_a, name_a = user(client, db_conn, "cashier")
    pers_b, name_b = user(client, db_conn, "cashier")
    grant(db_conn, name_a, "pay_expense")
    grant(db_conn, name_b, "pay_expense")
    cat = client.get("/payments/expenses/bootstrap", headers=admin).json()["categories"][0]
    key = _expense(client, pers_a, category=cat, amount=50).json()["key"]
    mine = next(e for e in client.get("/payments/expenses/entries", headers=pers_a).json()["entries"] if e["key"] == key)
    assert mine["canEdit"] is True and mine["canDelete"] is False
    edit = {"date": days.isoformat(), "amount": 75.5, "category": cat, "spentBy": name_a, "paidTo": "New shop", "description": "corrected"}
    assert client.put(f"/payments/expenses/entries/{key}", json=edit, headers=pers_b).status_code == 403                 # B cannot edit A's entry
    r = client.put(f"/payments/expenses/entries/{key}", json=edit, headers=pers_a)
    assert r.status_code == 200, r.text
    now = next(e for e in client.get("/payments/expenses/entries", headers=pers_a).json()["entries"] if e["key"] == key)
    assert now["amount"] == 75.5 and now["paidTo"] == "New shop" and now["description"] == "corrected"
    assert client.put(f"/payments/expenses/entries/{key}", json={**edit, "amount": 0}, headers=pers_a).status_code == 422
    assert client.put(f"/payments/expenses/entries/{key}", json={**edit, "category": "No such thing"}, headers=pers_a).status_code == 422
    with db_conn.cursor() as cur:
        cur.execute("SELECT details FROM admin_audit_log WHERE action = 'expense.edit' ORDER BY audit_key DESC LIMIT 1")
        assert cur.fetchone()[0]["before"]["amount"] == 50          # the old and the new values are kept
    # an administrator edits any entry and is the only one who may delete
    assert client.put(f"/payments/expenses/entries/{key}", json={**edit, "amount": 80}, headers=admin).status_code == 200
    assert client.delete(f"/payments/expenses/entries/{key}", headers=pers_a).status_code == 403
    gone = client.delete(f"/payments/expenses/entries/{key}", headers=admin)
    assert gone.status_code == 200 and key not in [e["key"] for e in client.get("/payments/expenses/entries", headers=admin).json()["entries"]]
    assert client.delete(f"/payments/expenses/entries/{key}", headers=admin).status_code == 404
    # a cancelled entry cannot be edited
    other = _expense(client, pers_a, category=cat).json()["key"]
    assert client.post(f"/payments/expenses/entries/{other}/void", json={"reason": "test"}, headers=pers_a).status_code == 200
    assert client.put(f"/payments/expenses/entries/{other}", json=edit, headers=pers_a).status_code == 409
    assert client.delete(f"/payments/expenses/entries/{other}", headers=admin).status_code == 200      # (the test database is shared: leave the account as it was)


# ------------------------------------------------------------------ Payments: the bank card, the changed-entry notice, the WhatsApp message
def test_payments_bank_card_counts_only_entries_made_there_and_changed_merges_are_announced(client, db_conn, days, seed, wa, monkeypatch):
    monkeypatch.setenv("VT_ALERTS_INLINE", "1")
    admin = login(client, db_conn, "admin")
    groups(client, admin, dispatch_godown="X@g.us")
    client.put("/admin/whatsapp/pay_entry", json={"group_id": "PAY@g.us", "enabled": True, "template": __import__("app.whatsapp", fromlist=["x"]).ALERTS["pay_entry"]["template"]}, headers=admin)
    base = client.get("/payments/dashboard", headers=admin).json()
    assert "changedAfterMerge" in base and base["changedAfterMerge"] >= 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT type_name FROM dim_txn_type WHERE entry_allowed AND direction = 'in' ORDER BY txn_type_key LIMIT 1")
        txn_type = cur.fetchone()[0]
        cur.execute("SELECT company_name FROM dim_company ORDER BY company_key LIMIT 1")
        company = cur.fetchone()[0]
        cur.execute("SELECT mode_name FROM dim_payment_mode WHERE NOT is_cash ORDER BY mode_key LIMIT 1")
        bank_mode = cur.fetchone()[0]
    r = client.post("/payments/transactions", json={"date": days.isoformat(), "type": txn_type, "company": company, "party": "Bank Party", "amount": 500000,
                                                    "mode": bank_mode, "remarks": "wire"}, headers=admin)
    assert r.json()["success"], r.text
    assert [s["to"] for s in wa] == ["PAY@g.us"]
    text = wa[0]["text"]
    assert f"Type: {txn_type}" in text and "Party: Bank Party" in text and "Amount: 5,000.00" in text and f"Mode: {bank_mode}" in text and "Entry Punch By:" in text
    assert client.get("/payments/dashboard", headers=admin).json()["bankBalance"] - base["bankBalance"] == 5000                 # a direct bank entry counts
    # an entry that came from VT Decor / O2D does not, and a change after the merge raises the notice
    with db_conn.cursor() as cur:
        cur.execute("SELECT txn_key FROM fact_payment_txn WHERE party_key = (SELECT party_key FROM dim_party WHERE party_name = 'Bank Party' LIMIT 1) LIMIT 1")
        txn = cur.fetchone()[0]
        cur.execute("UPDATE fact_payment_txn SET order_warning = 'Order changed in O2D after this entry was merged - please review' WHERE txn_key = %s", (txn,))
    db_conn.commit()
    assert client.get("/payments/dashboard", headers=admin).json()["changedAfterMerge"] == base["changedAfterMerge"] + 1


# ------------------------------------------------------------------ VT Decor: photos with the entry, WhatsApp, the line in Payments
def test_decor_photo_stays_with_its_payment_and_expense_and_the_entry_is_announced(client, db_conn, seed, wa, monkeypatch):
    monkeypatch.setenv("VT_ALERTS_INLINE", "1")
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    attachments.set_storage(FakeStore())
    try:
        owner = login(client, db_conn, "admin")
        groups(client, owner, dispatch_godown="X@g.us")
        client.put("/admin/whatsapp/decor_entry", json={"group_id": "DECOR@g.us", "enabled": True,
                                                        "template": __import__("app.whatsapp", fromlist=["x"]).ALERTS["decor_entry"]["template"]}, headers=owner)
        asha, _ = staff(client, db_conn)
        cfg = decor_rpc(client, owner, "getConfig")
        wall = next(t for t in cfg["productTypes"] if t["name"] == "Wallpaper")
        code = decor_rpc(client, asha, "addClient", name="Mrs Photo", mobile="9811100000", amount=100000, productType="Wallpaper")["clientId"]
        jpeg = "/9j/4AAQSkZJRg=="
        assert decor_rpc(client, asha, "addPayment", clientId=code, stage=wall["stages"][0]["name"], amount=2500, mode="UPI", base64=jpeg, mimeType="image/jpeg")["ok"]
        assert decor_rpc(client, asha, "addPayment", clientId=code, stage=wall["stages"][0]["name"], amount=100, mode="Cash")["ok"]
        assert decor_rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Materials", amount=300, base64=jpeg, mimeType="image/jpeg")["ok"]
        d = decor_rpc(client, asha, "getClientDetail", clientId=code)
        with_photo = [p for p in d["payments"] if p["photos"]]
        assert len(with_photo) == 1 and with_photo[0]["amount"] == 2500 and with_photo[0]["photos"][0]["url"].startswith("https://")
        assert [e["amount"] for e in d["expenses"] if e["photos"]] == [300]
        # the messages: a payment and an expense, in the format of the change list
        texts = [s["text"] for s in wa if s["to"] == "DECOR@g.us"]
        assert len(texts) == 3 and all(t.startswith("Payment - ") and "Product Type: Wallpaper" in t and "Party: Mrs Photo 9811100000" in t for t in texts)
        expense = next(t for t in texts if "Type: Expense" in t)
        assert "Expense Type: Materials" in expense and "Entry Punch By:" in expense
        assert "Expense Type" not in next(t for t in texts if "Type: Payment" in t)
        # in Payments the row says how the money came: cash or bank, and by which mode
        rows = client.get("/payments/merge?status=awaiting", headers=owner).json()
        lines = [r["paymentStatus"] for tab in rows.get("rows", rows.get("items", [])) for r in [tab]] if isinstance(rows, dict) else []
        assert any("UPI (Bank)" in x for x in lines) and any("- Cash (Cash)" in x for x in lines)
    finally:
        attachments.set_storage(None)


# ------------------------------------------------------------------ the photo check
def test_photo_check_lists_missing_and_reused_photos(client, people, db_conn, monkeypatch):
    h, today = people
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    attachments.set_storage(FakeStore())
    try:
        day = today
        ensure_date(db_conn, day)
        a, b, c = (add(client, h["shop"], day, _dc())["slNo"] for _ in range(3))
        for sl in (a, b, c):
            _status(client, h, sl, "Sent to the Company")
            _dispatch(client, h, "godown_dispatch", sl)
        same = b"\xff\xd8same-picture"
        for sl in (a, b):                       # the same picture on two orders; the third has none
            up = client.post(f"/o2d/orders/{sl}/photos?kind=dispatch", content=same, headers={**h["godown_dispatch"], "Content-Type": "image/jpeg"})
            assert up.status_code == 200, up.text
        d = client.get("/admin/orders/photo-check", params={"date_from": day.isoformat(), "date_to": day.isoformat()}, headers=h["admin"]).json()
        assert d["enabled"] is True
        missing = {r["slNo"] for r in d["noDispatchPhoto"]}
        assert c in missing and a not in missing and b not in missing          # (other tests' orders of the same day may be listed too)
        pairs = [sorted(x["slNo"] for x in g) for g in d["samePhotoOnManyOrders"]]
        assert [a, b] in pairs
        assert client.get("/admin/orders/photo-check", headers=h["shop"]).status_code == 403
    finally:
        attachments.set_storage(None)


# ------------------------------------------------------------------ godown: sent on by mistake, can still be put right; Overview filter lists
def test_godown_sees_orders_it_sent_on_until_they_are_dispatched_and_can_change_them(client, people, db_conn):
    h, today = people
    sl = add(client, h["shop"], today, _dc())["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    g = client.get("/o2d/godown", headers=h["godown"]).json()
    assert sl in [o["slNo"] for o in g["sent"]] and sl not in [o["slNo"] for o in g["pending"]]
    fixed = client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": "Sent to the Company"}).json()
    assert fixed["success"] and next(o for o in client.get("/o2d/godown", headers=h["godown"]).json()["sent"] if o["slNo"] == sl)["deliveryStatus"] == "Sent to the Company"
    _dispatch(client, h, "godown_dispatch", sl)
    assert sl not in [o["slNo"] for o in client.get("/o2d/godown", headers=h["godown"]).json()["sent"]]       # dispatched: it leaves the box


def test_a_person_with_the_overview_page_gets_the_filter_lists(client, db_conn, seed):
    who, name = user(client, db_conn, "godown")
    assert client.get("/o2d/admin/form-options", headers=who).status_code == 403
    grant(db_conn, name, "o2d_overview")
    r = client.get("/o2d/admin/form-options", headers=who)
    assert r.status_code == 200 and "orderVia" in r.json()["dropdowns"] and "createdBy" in r.json()["dropdowns"]


def test_the_purchase_page_does_not_fill_a_password_box_any_more():
    from pathlib import Path
    page = (Path(o2d_screens.__file__).parent / "static" / "purchase" / "index.html").read_text(encoding="utf-8")
    assert "passwordInput').value = 'portal'" not in page and "signInToPurchase('portal', 'portal')" in page
    assert db is not None
