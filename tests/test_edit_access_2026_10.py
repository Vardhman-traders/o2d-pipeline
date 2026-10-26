"""Who may edit, delete and cancel: VT Decor, Regular expenses and My checklist, with the add-on pages the admin gives in Setup > Access."""
from datetime import timedelta

from app import modcommon
from tests.test_checklist_expenses_places import grant
from tests.test_decor import rpc, staff
from tests.test_modules import days, user  # noqa: F401  (days: a fixture)
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


def _audit(db_conn, action):
    with db_conn.cursor() as cur:
        cur.execute("SELECT admin_username, target, details FROM admin_audit_log WHERE action = %s ORDER BY audit_key", (action,))
        return [{"user": r[0], "target": r[1], "details": r[2]} for r in cur.fetchall()]


# ------------------------------------------------------------------ the add-on pages exist in Setup > Access
def test_the_three_add_on_pages_are_listed_for_the_admin(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    text = str(client.get("/admin/access/matrix", headers=admin).json())
    assert "decor_edit" in text and "pay_expense_void" in text and "chk_edit" in text


# ------------------------------------------------------------------ VT Decor
def _client_with_payment_and_expense(client, asha):
    code = rpc(client, asha, "addClient", name="Edit Test", mobile="9999911111", amount=100000, productType="Wallpaper")["clientId"]
    cfg = rpc(client, asha, "getConfig")
    stage = next(t for t in cfg["productTypes"] if t["name"] == "Wallpaper")["stages"][0]["name"]
    assert rpc(client, asha, "addPayment", clientId=code, stage=stage, amount=3000, mode="Cash")["ok"]
    assert rpc(client, asha, "addExpense", clientId=code, type="Material", amount=2140)["ok"]
    d = rpc(client, asha, "getClientDetail", clientId=code)
    return code, stage, d["payments"][0]["rowIndex"], d["expenses"][0]["rowIndex"]


def test_staff_can_edit_their_own_payment_and_expense_but_not_delete_them(client, db_conn, seed):
    asha, _ = staff(client, db_conn)
    code, stage, pay, exp = _client_with_payment_and_expense(client, asha)
    cfg = rpc(client, asha, "getConfig")
    assert cfg["edit"] is True and cfg["manage"] is False                   # the page uses these to show Edit / Del

    assert rpc(client, asha, "editPayment", clientId=code, rowIndex=pay, stage=stage, amount=3500, mode="UPI", pnum="R1", remarks="fixed")["ok"]
    assert rpc(client, asha, "editExpense", clientId=code, rowIndex=exp, type="Labour", amount=2500, notes="changed")["ok"]
    d = rpc(client, asha, "getClientDetail", clientId=code)
    assert d["payments"][0]["amount"] == 3500 and d["payments"][0]["mode"] == "UPI" and d["payments"][0]["pnum"] == "R1"
    assert d["expenses"][0]["amount"] == 2500 and d["expenses"][0]["type"] == "Labour"
    # every change leaves who, old and new values behind
    # (only this client's rows: the audit log is shared by every test of the session)
    edits = [a for a in _audit(db_conn, "decor.edit_payment") if a["target"] == f"{code}#{pay}"]
    assert len(edits) == 1 and edits[0]["details"]["before"]["amount"] == 3000 and edits[0]["details"]["after"]["amount"] == 3500
    assert len([a for a in _audit(db_conn, "decor.edit_expense") if a["target"] == f"{code}#{exp}"]) == 1

    # a wrong amount is refused, not defaulted
    assert rpc(client, asha, "editPayment", clientId=code, rowIndex=pay, stage=stage, amount=0)["ok"] is False
    assert rpc(client, asha, "editPayment", clientId=code, rowIndex=pay, stage=stage, amount=-5)["ok"] is False
    assert rpc(client, asha, "editExpense", clientId=code, rowIndex=exp, type="", amount=5)["ok"] is False
    assert rpc(client, asha, "getClientDetail", clientId=code)["payments"][0]["amount"] == 3500

    # deleting stays with the owner side
    assert rpc(client, asha, "deletePayment", clientId=code, rowIndex=pay)["msg"] == "Not authorized"
    assert rpc(client, asha, "deleteExpense", clientId=code, rowIndex=exp)["msg"] == "Not authorized"


def test_the_decor_edit_page_lets_a_staff_member_delete_and_revise(client, db_conn, seed):
    asha, asha_name = staff(client, db_conn)
    code, _, pay, exp = _client_with_payment_and_expense(client, asha)
    grant(db_conn, asha_name, "decor_edit")
    assert rpc(client, asha, "getConfig")["manage"] is True
    assert rpc(client, asha, "reviseAmount", clientId=code, newAmount=90000, reason="scope")["ok"]
    assert rpc(client, asha, "deleteExpense", clientId=code, rowIndex=exp)["ok"]
    assert rpc(client, asha, "deletePayment", clientId=code, rowIndex=pay)["ok"]
    deleted = [a for a in _audit(db_conn, "decor.delete_payment") if a["target"] == f"{code}#{pay}"]   # this client's rows only
    assert len(deleted) == 1 and deleted[0]["details"]["amount"] == 3000


def test_decor_edit_view_only_gives_no_delete_and_it_does_not_widen_what_is_visible(client, db_conn, seed):
    asha, asha_name = staff(client, db_conn)
    ravi, ravi_name = staff(client, db_conn)
    code, _, pay, _ = _client_with_payment_and_expense(client, asha)
    grant(db_conn, ravi_name, "decor_edit")
    assert rpc(client, ravi, "getClientDetail", clientId=code)["msg"] == "Access denied"           # still only their own clients
    assert rpc(client, ravi, "deletePayment", clientId=code, rowIndex=pay)["ok"] is False
    grant(db_conn, asha_name, "decor_edit", view_only=True)
    assert rpc(client, asha, "getConfig")["manage"] is False
    assert rpc(client, asha, "deletePayment", clientId=code, rowIndex=pay)["msg"] == "Not authorized"


def test_a_completed_client_is_read_only_for_everyone_but_the_admin(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    asha, asha_name = staff(client, db_conn)
    code, stage, pay, exp = _client_with_payment_and_expense(client, asha)
    grant(db_conn, asha_name, "decor_edit")
    assert rpc(client, asha, "markCompleted", clientId=code)["ok"]
    assert rpc(client, asha, "getClientDetail", clientId=code)["locked"] is True
    for action, extra in (("editPayment", dict(rowIndex=pay, stage=stage, amount=1)), ("editExpense", dict(rowIndex=exp, type="x", amount=1)),
                          ("deletePayment", dict(rowIndex=pay)), ("deleteExpense", dict(rowIndex=exp)), ("addPayment", dict(stage=stage, amount=5))):
        out = rpc(client, asha, action, clientId=code, **extra)
        assert out["ok"] is False and "Completed" in out["msg"], action
    assert rpc(client, admin, "editPayment", clientId=code, rowIndex=pay, stage=stage, amount=4000, mode="Cash")["ok"]
    assert rpc(client, admin, "deleteExpense", clientId=code, rowIndex=exp)["ok"]


# ------------------------------------------------------------------ Regular expenses
def _expense(client, headers, **kw):
    body = {"kind": "Expense", "amount": 50, "category": "", "paidTo": "Shop", "description": "x", **kw}
    return client.post("/payments/expenses/entries", json=body, headers=headers)


def test_cancel_anyones_entry_page_cancels_without_topping_up(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    pers_a, name_a = user(client, db_conn, "cashier")
    pers_b, name_b = user(client, db_conn, "cashier")
    grant(db_conn, name_a, "pay_expense")
    grant(db_conn, name_b, "pay_expense")
    boot = client.get("/payments/expenses/bootstrap", headers=admin).json()
    cat = boot["categories"][0]
    made = _expense(client, pers_a, category=cat)
    assert made.status_code == 201, made.text
    key = made.json()["key"]
    # B cannot cancel A's entry (the old rule) ...
    assert client.post(f"/payments/expenses/entries/{key}/void", json={"reason": "wrong one"}, headers=pers_b).status_code == 403
    # ... until the admin gives B the add-on page; that is not the manager page, so B still cannot add money
    grant(db_conn, name_b, "pay_expense_void")
    assert client.get("/payments/expenses/bootstrap", headers=pers_b).json()["canVoidAny"] is True
    assert _expense(client, pers_b, kind="Topup", amount=100).status_code == 403
    assert any(e["canVoid"] for e in client.get("/payments/expenses/entries", headers=pers_b).json()["entries"] if e["key"] == key)
    assert client.post(f"/payments/expenses/entries/{key}/void", json={"reason": "wrong one"}, headers=pers_b).status_code == 200
    # at View only level it does nothing
    again = _expense(client, pers_a, category=cat).json()["key"]
    grant(db_conn, name_b, "pay_expense_void", view_only=True)
    assert client.post(f"/payments/expenses/entries/{again}/void", json={"reason": "wrong one"}, headers=pers_b).status_code == 403


# ------------------------------------------------------------------ My checklist
def _checklist(client, admin, assignee, title, freq="Daily", **kw):
    today = modcommon.today_ist()
    body = {"title": title, "assignee": assignee, "frequency": freq, "startDate": today.isoformat(), **kw}
    r = client.post("/checklist/checklists", json=body, headers=admin)
    assert r.status_code == 201, r.text


def _not_the_weekly_off_day(monkeypatch):
    """The weekly off day (default Monday) has no Daily items, so on that day these tests would find nothing due: move it to a day that is
    neither today nor tomorrow (Sunday = 0 ... Saturday = 6, as the setting counts)."""
    from app import checklist
    today_dow = (modcommon.today_ist().weekday() + 1) % 7
    monkeypatch.setattr(checklist, "_off_day", lambda: (today_dow + 3) % 7)


def test_my_checklist_person_filter_and_frequency_on_every_item(client, db_conn, days, seed, monkeypatch):
    _not_the_weekly_off_day(monkeypatch)
    admin = login(client, db_conn, "admin")
    emp1, n1 = user(client, db_conn, "cashier")
    emp2, n2 = user(client, db_conn, "cashier")
    grant(db_conn, n1, "chk_mine")
    grant(db_conn, n2, "chk_mine")
    today = modcommon.today_ist()
    _checklist(client, admin, n1, "Open the shop")                                          # Daily
    _checklist(client, admin, n2, "Count stock", "Weekly", dueDow=(today.weekday() + 1) % 7)
    _checklist(client, admin, n2, "Pay rent", "Monthly", dueDay=today.day)

    mine = client.get("/checklist/mine", headers=emp1).json()
    assert mine["scope"] == "me" and {i["frequency"] for i in mine["dueToday"]} == {"Daily"}
    assert all(i["assigneeId"] == n1 for i in mine["dueToday"])
    # a plain employee cannot look at anyone else's list, and the refusal says why
    for who in ("all", n2):
        r = client.get(f"/checklist/mine?assignee={who}", headers=emp1)
        assert r.status_code == 403 and "only see your own" in r.json()["detail"]
    assert client.get(f"/checklist/mine?assignee={n1}", headers=emp1).json()["scope"] == "me"      # asking for yourself is fine

    everyone = client.get("/checklist/mine?assignee=all", headers=admin).json()
    assert everyone["scope"] == "all" and {i["assigneeId"] for i in everyone["dueToday"]} >= {n1, n2}
    assert {i["frequency"] for i in everyone["dueToday"]} >= {"Daily", "Weekly", "Monthly"}
    one = client.get(f"/checklist/mine?assignee={n2}", headers=admin).json()
    assert one["scope"] == "person" and {i["assigneeId"] for i in one["dueToday"]} == {n2}
    assert client.get("/checklist/mine?assignee=nobody-here", headers=admin).status_code == 404

    me = client.get("/checklist/me", headers=admin).json()
    assert me["canSeeEveryone"] is True and me["canEditOthers"] is True and {p["id"] for p in me["people"]} >= {n1, n2}
    assert client.get("/checklist/me", headers=emp1).json()["canSeeEveryone"] is False


def test_checklist_edit_page_marks_and_reopens_for_others_view_only_does_not(client, db_conn, days, seed, monkeypatch):
    _not_the_weekly_off_day(monkeypatch)
    admin = login(client, db_conn, "admin")
    emp, ename = user(client, db_conn, "cashier")
    helper, hname = user(client, db_conn, "cashier")
    grant(db_conn, ename, "chk_mine")
    grant(db_conn, hname, "chk_mine")
    _checklist(client, admin, ename, "Lock the godown")
    item = client.get("/checklist/mine", headers=emp).json()["dueToday"][0]
    url = f"/checklist/occurrences/{item['key']}/mark"
    assert client.post(url, json={"status": "Done"}, headers=helper).status_code == 403            # not theirs, no add-on yet
    grant(db_conn, hname, "chk_edit", view_only=True)
    assert client.post(url, json={"status": "Done"}, headers=helper).status_code == 403            # View only = no
    grant(db_conn, hname, "chk_edit")
    assert client.get("/checklist/me", headers=helper).json()["canEditOthers"] is True
    seen = client.get(f"/checklist/mine?assignee={ename}", headers=helper).json()
    assert [i["title"] for i in seen["dueToday"]] == ["Lock the godown"]
    assert client.post(url, json={"status": "Done"}, headers=helper).status_code == 200
    assert client.post(url, json={"status": "Pending"}, headers=helper).status_code == 200         # re-open
    # the add-on never lets the editor write through someone else's wording: they still cannot create checklists
    assert client.post("/checklist/checklists", json={"title": "x", "assignee": ename, "frequency": "Once",
                                                      "startDate": (modcommon.today_ist() + timedelta(days=1)).isoformat()}, headers=helper).status_code == 403
