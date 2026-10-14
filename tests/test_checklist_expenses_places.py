"""Checklist (Delegation), Regular expenses (Payments) and per-employee work places (Attendance)."""
from datetime import date, timedelta

from app import bulk_modules, checklist, modcommon
from tests.test_modules import days, user  # noqa: F401  (days: a fixture)
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)

GODOWN = (28.7004323, 77.1288457)
SHOP = (28.7010667, 77.1290033)


def grant(db_conn, username, *pages, view_only=False):
    with db_conn.cursor() as cur:
        for p in pages:
            cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, view_only) "
                        "SELECT user_key, %s, true, %s FROM dim_user WHERE username = %s ON CONFLICT (user_key, page_key) DO UPDATE "
                        "SET allowed = true, view_only = EXCLUDED.view_only", (p, view_only, username))
    db_conn.commit()


def pub(http, action, *args):
    r = http.post("/attendance/public/rpc", json={"action": action, "args": list(args)})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ the schedule arithmetic
def test_due_dates_for_each_frequency():
    lo, hi = date(2026, 1, 1), date(2026, 12, 31)
    mon_off = dict(off_dow=1)
    daily = checklist.due_dates("Daily", date(2026, 10, 5), None, date(2026, 10, 5), date(2026, 10, 18), **mon_off)
    assert len(daily) == 12 and all(d.weekday() != 0 for d in daily)           # the two Mondays are skipped
    weekly = checklist.due_dates("Weekly", date(2026, 10, 1), None, date(2026, 10, 1), date(2026, 10, 31), dow=5)
    assert [d.day for d in weekly] == [2, 9, 16, 23, 30]                       # Fridays
    assert checklist.due_dates("Monthly", date(2026, 1, 1), None, lo, hi, day=31)[1] == date(2026, 2, 28)   # clamped to the month end
    q = checklist.due_dates("Quarterly", date(2026, 1, 1), None, lo, hi, day=20, month=1)
    assert [d.month for d in q] == [1, 4, 7, 10]
    assert checklist.due_dates("Yearly", date(2026, 1, 1), None, date(2026, 1, 1), date(2028, 12, 31), day=31, month=3) == \
        [date(2026, 3, 31), date(2027, 3, 31), date(2028, 3, 31)]
    assert checklist.due_dates("Once", date(2026, 5, 5), None, lo, hi) == [date(2026, 5, 5)]
    assert checklist.due_dates("Monthly", date(2026, 6, 1), date(2026, 7, 31), lo, hi, day=10) == [date(2026, 6, 10), date(2026, 7, 10)]


# ------------------------------------------------------------------ attendance work places
def test_employee_tagged_to_a_work_place_cannot_mark_from_another(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    for name in ("Tagged Person", "Free Person"):
        assert client.post("/admin/masters/att_employee", json={"name": name, "role_title": "T", "is_active": True, "skip_whatsapp": True},
                           headers=admin).status_code == 201

    def own(action, *args):
        r = client.post("/attendance/owner/rpc", json={"action": action, "args": ["portal", *args]}, headers=admin)
        assert r.status_code == 200, r.text
        return r.json()

    assert {"name": "Godown", "icon": "🏭"} in own("getWorkPlaces")["places"]
    assert own("setEmployeePlaces", "Tagged Person", ["Nowhere"])["status"] == "error"
    assert own("setEmployeePlaces", "Tagged Person", ["Godown"])["status"] == "ok"
    listed = {e["name"]: e["places"] for e in own("getAllEmployees")["employees"]}
    assert listed["Tagged Person"] == ["Godown"] and listed["Free Person"] == []

    ok = pub(client, "checkLocation", *GODOWN, "Tagged Person")
    assert ok["status"] == "ok" and ok["site"] == "Godown"
    wrong = pub(client, "checkLocation", *SHOP, "Tagged Person")
    assert wrong["status"] == "wrong_location" and wrong["allowed"] == ["Godown"] and "Godown" in wrong["msg"] and "Shop" in wrong["msg"]
    saved = pub(client, "saveAttendance", {"name": "Tagged Person", "lat": SHOP[0], "lng": SHOP[1]})
    assert saved["status"] == "wrong_location" and "Godown" in saved["msg"]
    assert pub(client, "getTodayStatus", "Tagged Person")["checkedIn"] is False          # nothing was recorded

    # someone not tagged to anything keeps working from any work place
    assert pub(client, "checkLocation", *SHOP, "Free Person")["status"] == "ok"
    assert pub(client, "saveAttendance", {"name": "Free Person", "lat": SHOP[0], "lng": SHOP[1]})["status"] == "ok"
    # the tagged person can mark from their own place, and clearing the tags frees them again
    assert pub(client, "saveAttendance", {"name": "Tagged Person", "lat": GODOWN[0], "lng": GODOWN[1]})["status"] == "ok"
    assert own("setEmployeePlaces", "Tagged Person", [])["status"] == "ok"
    assert pub(client, "checkLocation", *SHOP, "Tagged Person")["status"] == "ok"


# ------------------------------------------------------------------ checklist
def test_checklist_create_assign_mark_and_calendar(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    mgr, mgr_name = user(client, db_conn, "manager")
    staff, staff_name = user(client, db_conn, "cashier")      # a role with no Delegation pages of its own
    other, other_name = user(client, db_conn, "cashier")
    grant(db_conn, mgr_name, "chk_manage", "chk_calendar")
    grant(db_conn, staff_name, "chk_mine", "chk_calendar")
    grant(db_conn, other_name, "chk_mine")
    today = modcommon.today_ist()

    # the screen opens for a person who holds only checklist pages
    me = client.get("/delegation/me", headers=staff).json()
    assert "chk_mine" in me["pages"] and client.get("/checklist/me", headers=staff).json()["role"] == "Staff"
    assert client.get("/checklist/mine", headers=mgr).status_code == 403            # the manager page is not "My checklist"

    # only a manager creates; the person must hold My checklist; the schedule must be complete
    body = {"title": "File GSTR-3B", "category": "GST", "assignee": staff_name, "frequency": "Once", "startDate": today.isoformat()}
    assert client.post("/checklist/checklists", json=body, headers=staff).status_code == 403
    assert client.post("/checklist/checklists", json={**body, "assignee": mgr_name}, headers=mgr).status_code == 422
    assert client.post("/checklist/checklists", json={**body, "frequency": "Weekly"}, headers=mgr).status_code == 422
    assert client.post("/checklist/checklists", json={**body, "startDate": (today - timedelta(days=1)).isoformat()}, headers=mgr).status_code == 422
    assert client.post("/checklist/checklists", json={**body, "category": "No such"}, headers=mgr).status_code == 422
    made = client.post("/checklist/checklists", json=body, headers=mgr)
    assert made.status_code == 201, made.text
    client.post("/checklist/checklists", json={**body, "title": "Monthly stock count", "frequency": "Monthly", "dueDay": today.day},
                headers=admin)

    mine = client.get("/checklist/mine", headers=staff).json()
    titles = [i["title"] for i in mine["dueToday"]]
    assert "File GSTR-3B" in titles and "Monthly stock count" in titles
    item = next(i for i in mine["dueToday"] if i["title"] == "File GSTR-3B")

    # somebody else's item cannot be marked; Not Done needs a reason; Done sticks
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=other).status_code == 403
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Not Done"}, headers=staff).status_code == 422
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=staff).status_code == 200
    again = client.get("/checklist/mine", headers=staff).json()
    assert "File GSTR-3B" not in [i["title"] for i in again["dueToday"]]
    assert any(i["title"] == "File GSTR-3B" and i["status"] == "Done" for i in again["finished"])
    # a person cannot re-open what they closed; a manager can
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=staff).status_code == 403
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=mgr).status_code == 200

    # the calendar: the manager sees everyone, the person only their own
    cal = client.get(f"/checklist/calendar?year={today.year}&month={today.month}", headers=mgr).json()
    assert today.isoformat() in cal["days"] and cal["scope"] == "all"
    own_cal = client.get(f"/checklist/calendar?year={today.year}&month={today.month}", headers=staff).json()
    assert own_cal["scope"] == "person" and all(i["assigneeId"] == staff_name for v in own_cal["days"].values() for i in v)
    assert client.get("/checklist/calendar", headers=other).status_code == 403          # no calendar page

    # stopping a checklist removes its not-yet-done future rows; a marked checklist cannot be deleted
    rows = client.get("/checklist/checklists", headers=mgr).json()
    gst = next(r for r in rows if r["title"] == "File GSTR-3B")
    assert gst["when"] == "One time"
    monthly = next(r for r in rows if r["title"] == "Monthly stock count")
    assert client.post(f"/checklist/checklists/{monthly['key']}/active", json={"active": False}, headers=mgr).status_code == 200
    assert "Monthly stock count" not in [i["title"] for i in client.get("/checklist/mine", headers=staff).json()["dueToday"]]
    marked = client.get("/checklist/mine", headers=staff).json()["dueToday"]
    mark_key = next(i["key"] for i in marked if i["title"] == "File GSTR-3B")
    client.post(f"/checklist/occurrences/{mark_key}/mark", json={"status": "Done"}, headers=staff)
    assert client.delete(f"/checklist/checklists/{gst['key']}", headers=mgr).status_code == 409


def test_checklist_page_off_means_module_part_off(client, db_conn, days, seed):
    staff, staff_name = user(client, db_conn, "cashier")
    assert client.get("/checklist/mine", headers=staff).status_code == 403
    assert client.get("/delegation/me", headers=staff).status_code == 403
    grant(db_conn, staff_name, "chk_mine", view_only=True)
    assert client.get("/checklist/mine", headers=staff).status_code == 200
    with db_conn.cursor() as cur:
        cur.execute("UPDATE user_page_access SET allowed = false WHERE page_key = 'chk_mine' AND user_key = "
                    "(SELECT user_key FROM dim_user WHERE username = %s)", (staff_name,))
    db_conn.commit()
    assert client.get("/checklist/mine", headers=staff).status_code == 403


def test_checklist_bulk_upload_validation(client, db_conn, days, seed):
    staff, staff_name = user(client, db_conn, "cashier")
    grant(db_conn, staff_name, "chk_mine")
    today = modcommon.today_ist().strftime("%d-%m-%Y")
    rows = [
        {"Title": "File TDS", "Category": "GST", "Frequency": "Monthly", "Day": "7", "Assigned To": staff_name, "_row": 2},
        {"Title": "Open shop", "Frequency": "Weekly", "Day": "Mon", "Assigned To": staff_name, "Start Date": today, "_row": 3},
        {"Title": "Renew licence", "Frequency": "Yearly", "Day": "Last", "Month": "Mar", "Assigned To": staff_name, "_row": 4},
        {"Title": "Bad", "Frequency": "Weekly", "Assigned To": staff_name, "_row": 5},
        {"Title": "Nobody", "Frequency": "Daily", "Assigned To": "ghost", "_row": 6},
        {"Title": "Old", "Frequency": "Daily", "Assigned To": staff_name, "Start Date": "01-01-2025", "_row": 7},
        {"Title": "Odd", "Frequency": "Fortnightly", "Assigned To": staff_name, "_row": 8},
    ]
    rep = bulk_modules.ENTITIES["checklists"]["validate"](rows)
    errs = {e["row"]: e["error"] for e in rep["errors"]}
    assert set(errs) == {5, 6, 7, 8}, errs
    assert "day of the week" in errs[5] and "not found" in errs[6] and "past" in errs[7] and "Frequency must be" in errs[8]


# ------------------------------------------------------------------ regular expenses
def test_regular_expenses_balance_roles_and_void(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    clerk, clerk_name = user(client, db_conn, "staff")          # roles with no Payments pages of their own
    boss, boss_name = user(client, db_conn, "manager")
    nobody, _ = user(client, db_conn, "purchase_godown")
    grant(db_conn, clerk_name, "pay_expense")
    grant(db_conn, boss_name, "pay_expense_manage")
    assert client.get("/payments/expenses/bootstrap", headers=nobody).status_code == 403

    # a person holding only the expenses page opens the Payments screen (lists), but never the transaction entries
    b = client.get("/payments/bootstrap", headers=clerk)
    assert b.status_code == 200 and b.json()["role"] == "Expenses" and b.json()["expenseLevel"] == "enter"
    assert client.get("/payments/transactions", headers=clerk).status_code == 403

    # the opening balance is a Setup value
    boot = client.get("/payments/expenses/bootstrap", headers=boss).json()
    assert boot["level"] == "manage" and boot["accounts"][0]["name"] == "Petty cash" and boot["categories"]
    assert client.put(f"/admin/masters/expense_account/{boot['accounts'][0]['key']}",
                      json={"name": "Petty cash", "opening_balance": 1000}, headers=admin).status_code == 200
    cat = boot["categories"][0]

    def add(h, **kw):
        return client.post("/payments/expenses/entries", json={"kind": "Expense", "amount": 100, "category": cat, "paidTo": "Chai wala", **kw}, headers=h)

    assert client.post("/payments/expenses/entries", json={"kind": "Topup", "amount": 500}, headers=clerk).status_code == 403
    assert client.post("/payments/expenses/entries", json={"kind": "Topup", "amount": 500}, headers=boss).json()["balance"] == 1500
    r = add(clerk, amount=250.5)
    assert r.status_code == 201 and r.json()["balance"] == 1249.5 and r.json()["overdrawn"] is False
    assert add(clerk, category="").status_code == 422 and add(clerk, paidTo="").status_code == 422
    assert add(clerk, amount=0).status_code == 422
    assert add(clerk, date=(modcommon.today_ist() + timedelta(days=1)).isoformat()).status_code == 422

    s = client.get("/payments/expenses/summary", headers=clerk).json()
    assert s["balance"] == 1249.5 and s["period"]["spent"] == 250.5 and s["period"]["topups"] == 500 and s["period"]["opening"] == 1000
    assert s["byCategory"][0]["amount"] == 250.5

    # the ledger keeps a running balance; a spend that goes below zero is saved and flagged
    big = add(clerk, amount=5000)
    assert big.json()["overdrawn"] is True and big.json()["balance"] == -3750.5
    led = client.get("/payments/expenses/entries", headers=clerk).json()
    assert [e["balanceAfter"] for e in led["entries"]] == [1500, 1249.5, -3750.5]

    # voiding: own entry on the same day, anyone else's needs the manager; a cancelled entry leaves the balance
    other = add(boss, amount=10).json()
    assert client.post(f"/payments/expenses/entries/{other['key']}/void", json={"reason": "typo"}, headers=clerk).status_code == 403
    mine = client.post(f"/payments/expenses/entries/{big.json()['key']}/void", json={"reason": "entered twice"}, headers=clerk)
    assert mine.status_code == 200 and mine.json()["balance"] == 1239.5
    assert client.post(f"/payments/expenses/entries/{big.json()['key']}/void", json={"reason": "again"}, headers=boss).status_code == 409
    assert client.post(f"/payments/expenses/entries/{other['key']}/void", json={"reason": "x"}, headers=boss).status_code == 422
    led = client.get("/payments/expenses/entries?include_voided=false", headers=boss).json()
    assert all(not e["voided"] for e in led["entries"])
    # view-only access reads but cannot save
    grant(db_conn, clerk_name, "pay_expense", view_only=True)
    assert client.get("/payments/expenses/summary", headers=clerk).status_code == 200
    assert add(clerk).status_code == 403
