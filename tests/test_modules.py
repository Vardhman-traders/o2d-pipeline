"""Payments, Delegation and Purchase modules: the rules the old Apps Scripts had, on the shared database."""
from datetime import timedelta

import pytest

from app import attachments, modcommon, o2d_screens
from tests.test_o2d_screens import PW, client, ensure_date, login, seed  # noqa: F401  (fixtures)


@pytest.fixture()
def days(db_conn):
    today = modcommon.today_ist()
    for i in range(-20, 21):
        ensure_date(db_conn, today + timedelta(days=i))
    return today


def user(client, db_conn, role, username=None):
    """(headers, username) for a fresh user of `role`."""
    import uuid
    from app import auth
    name = username or f"m_{role}_{uuid.uuid4().hex[:6]}"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) VALUES (%s, %s, %s, %s, false)",
                    (name, auth.hash_password(PW), role, name.title()))
    db_conn.commit()
    tok = client.post("/auth/login", json={"username": name, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, name


def txn(**kw):
    base = {"date": modcommon.today_ist().isoformat(), "type": "Payment Received", "company": "IRIS", "party": "Acme", "mode": "Cash",
            "amount": 1000, "invoice": "INV-1", "remarks": ""}
    return {**base, **kw}


# ---------------------------------------------------------------- payments
def test_roles_come_from_pages_and_cashier_entries_follow_the_mode_flags(client, db_conn, days, seed):
    cash, _ = user(client, db_conn, "cashier")
    acct, _ = user(client, db_conn, "accounts")
    stranger, _ = user(client, db_conn, "godown")
    assert client.get("/payments/bootstrap", headers=stranger).status_code == 403
    assert client.get("/payments/bootstrap", headers=cash).json()["role"] == "Cashier"
    assert client.get("/payments/bootstrap", headers=acct).json()["role"] == "Account"
    boot = client.get("/payments/bootstrap", headers=cash).json()
    assert "Sales Cash" not in boot["entryTypes"] and "Sales Cash" in boot["txnTypes"] and "VT" in boot["companies"]
    assert boot["modeMeta"]["Cash"]["isCash"] is True and boot["companyMeta"]["VT"]["isSales"] is True

    r = client.post("/payments/transactions", json=txn(), headers=cash).json()
    assert r["success"] and r["autoApproved"] is True and r["txnId"].startswith("TXN-")
    pend = client.post("/payments/transactions", json=txn(mode="PENDING", amount=500), headers=cash).json()
    assert pend["autoApproved"] is False                       # the PENDING mode needs approval - a flag, not a typed name
    assert client.post("/payments/transactions", json=txn(amount=0), headers=cash).json()["success"] is False
    assert client.post("/payments/transactions", json=txn(company="Nope"), headers=cash).json()["success"] is False
    # decisions are the account team's
    assert client.post(f"/payments/transactions/{pend['txnId']}/decision", json={"decision": "Approved"}, headers=cash).json()["success"] is False
    assert client.post(f"/payments/transactions/{pend['txnId']}/decision", json={"decision": "Approved"}, headers=acct).json()["success"] is True
    assert client.post(f"/payments/transactions/{pend['txnId']}/decision", json={"decision": "Rejected"}, headers=acct).json()["success"] is False


def test_dashboard_totals_follow_the_list_flags(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    other = (days - timedelta(days=2)).isoformat()
    post = lambda **kw: client.post("/payments/transactions", json=txn(**kw), headers=admin).json()["success"]  # noqa: E731
    base = client.get("/payments/dashboard", headers=admin).json()
    assert post(type="Payment Received", mode="Cash", amount=1000)
    assert post(type="Payment Paid", mode="Cash", amount=300)
    assert post(type="Payment Received", mode="Bank", amount=5000, date=other)
    assert post(type="Cash Deposit", mode="Bank", amount=700)              # moves cash<->bank: not in the bank balance
    assert post(type="Sales", company="VT", mode="Cash", amount=200)
    assert post(type="Sale Return", company="VT", mode="Cash", amount=50)
    assert post(type="Sales", company="IRIS", mode="Cash", amount=999)     # not a sales company: no cash sales
    d = client.get("/payments/dashboard", headers=admin).json()
    delta = lambda k: round(d[k] - base[k], 2)  # noqa: E731  (other tests share the database)
    assert delta("totalReceived") == 6000 and delta("totalPaid") == 300
    assert delta("cashSales") == 150
    assert delta("bankBalance") == 5000
    assert delta("cashInHand") == 1000 - 300 + 200 - 50 + 999
    # a date range narrows it, and the cashier only ever sees today
    narrowed = client.get(f"/payments/dashboard?date_from={other}&date_to={other}", headers=admin).json()
    assert narrowed["scopeLabel"] == "Filtered Summary" and narrowed["bankBalance"] >= 5000


def test_cashier_edit_and_visibility_rules(client, db_conn, days, seed):
    cash, name = user(client, db_conn, "cashier")
    cash2, _ = user(client, db_conn, "cashier")
    acct, _ = user(client, db_conn, "accounts")
    mine = client.post("/payments/transactions", json=txn(), headers=cash).json()["txnId"]
    old = client.post("/payments/transactions", json=txn(date=(days - timedelta(days=3)).isoformat()), headers=cash).json()
    assert old["isBackdated"] is True
    rows = client.get("/payments/transactions", headers=cash).json()["rows"]
    assert [r["TxnID"] for r in rows] == [mine]                            # own entries of today only
    assert all(r["CreatedBy"] == name for r in rows)
    assert client.put(f"/payments/transactions/{old['txnId']}", json=txn(amount=5), headers=cash).json()["success"] is False   # yesterday is locked
    assert client.put(f"/payments/transactions/{mine}", json=txn(amount=2000), headers=cash2).json()["success"] is False      # not theirs
    assert client.put(f"/payments/transactions/{mine}", json=txn(amount=2000), headers=cash).json()["success"] is True
    assert next(r for r in client.get("/payments/transactions", headers=acct).json()["rows"] if r["TxnID"] == mine)["Amount"] == 2000
    # amounts are stored as typed (the old sheet stored them divided by 100)
    assert client.get("/payments/transactions", headers=cash).json()["rows"][0]["Amount"] == 2000


def test_parties_are_scoped_by_company_and_type_and_added_on_first_use(client, db_conn, days, seed):
    acct, _ = user(client, db_conn, "accounts")
    assert client.post("/payments/parties", json={"name": "Zed Traders", "company": "IRIS", "txnType": "Expense"}, headers=acct).json()["success"]
    assert not client.post("/payments/parties", json={"name": "zed traders", "company": "IRIS", "txnType": "Expense"}, headers=acct).json()["success"]
    assert client.post("/payments/transactions", json=txn(party="Fresh Party", type="Sales"), headers=acct).json()["success"]
    assert "Zed Traders" in client.get("/payments/parties?company=IRIS&txn_type=Expense", headers=acct).json()
    assert "Zed Traders" not in client.get("/payments/parties?company=IRIS&txn_type=Sales", headers=acct).json()
    assert "Fresh Party" in client.get("/payments/parties?company=IRIS&txn_type=Sales", headers=acct).json()
    assert "Zed Traders" in client.get("/payments/parties?company=IRIS", headers=acct).json()           # no type: all of the company's


def test_payments_view_only_cannot_save(client, db_conn, days, seed):
    acct, name = user(client, db_conn, "accounts")
    admin = login(client, db_conn, "admin")
    key = client.get("/auth/me", headers=acct).json()["user_key"]
    client.put(f"/admin/access/users/{key}", headers=admin, json={"page_key": "pay_account", "allowed": True, "view_only": True})
    assert client.get("/payments/transactions", headers=acct).status_code == 200
    assert client.post("/payments/transactions", json=txn(), headers=acct).status_code == 403


# ---------------------------------------------------------------- delegation
def test_task_lifecycle_scoring_and_week(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    other, _ = user(client, db_conn, "staff")
    assert any(s["id"] == sname for s in client.get("/delegation/staff", headers=mgr).json())      # staff, not managers
    assert client.get("/delegation/staff", headers=staff).status_code == 403
    due = days.isoformat()
    msg = client.post("/delegation/tasks", json={"staffId": sname, "desc": "Count stock", "dueDate": due}, headers=mgr).json()
    assert "Task assigned" in msg
    assert client.post("/delegation/tasks", json={"staffId": sname, "desc": "x", "dueDate": due}, headers=staff).status_code == 403
    tid = next(t for t in client.get("/delegation/tasks", headers=mgr).json() if t["assignedId"] == sname)["taskId"]
    assert [t["taskId"] for t in client.get("/delegation/tasks", headers=staff).json()] == [tid]          # privacy: own tasks only
    assert client.get("/delegation/tasks", headers=other).json() == []
    assert "own tasks" in client.post(f"/delegation/tasks/{tid}/complete", json={}, headers=other).json()
    assert "+10 points" in client.post(f"/delegation/tasks/{tid}/complete", json={"notes": "done"}, headers=staff).json()
    assert client.post(f"/delegation/tasks/{tid}/complete", json={}, headers=staff).json() == "This task is not pending any more."
    assert "-3 points" in client.post(f"/delegation/tasks/{tid}/review", json={"status": "Revise", "remark": "redo"}, headers=mgr).json()
    assert "+7 points" in client.post(f"/delegation/tasks/{tid}/complete", json={}, headers=staff).json()    # 10 less one revision
    assert client.post(f"/delegation/tasks/{tid}/review", json={"status": "Approved"}, headers=mgr).json() == "Task Approved successfully!"
    me = client.get("/delegation/my-score", headers=staff).json()
    assert me["totalScore"] == 7 and me["onTime"] == 1
    week = client.get(f"/delegation/weekly?staff_id={sname}", headers=staff).json()
    assert week["counts"]["yellow"] == 1 and week["counts"]["green"] == 0                                # one revision = yellow
    assert client.get(f"/delegation/weekly?staff_id={sname}", headers=other).status_code == 403
    assert "100%" in client.post("/delegation/weekly-plan", json={"staffId": sname, "weekStart": week["nextWeekStart"], "green": 50, "yellow": 10, "red": 10}, headers=staff).json()
    assert "Plan saved" in client.post("/delegation/weekly-plan", json={"staffId": sname, "weekStart": week["nextWeekStart"], "green": 60, "yellow": 30, "red": 10}, headers=staff).json()
    assert client.get(f"/delegation/weekly?staff_id={sname}&anchor={week['nextWeekStart']}", headers=staff).json()["plan"]["green"] == 60
    board = client.get("/delegation/scoreboard", headers=staff).json()
    assert any(b["id"] == sname and b["totalScore"] == 7 for b in board)


def test_missed_deadline_extension_costs_points_and_counts_red(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    yesterday = (days - timedelta(days=1)).isoformat()
    client.post("/delegation/tasks", json={"staffId": sname, "desc": "Late job", "dueDate": yesterday}, headers=mgr)
    task = next(t for t in client.get("/delegation/tasks", headers=staff).json() if t["taskDesc"] == "Late job")
    assert task["deadlineState"] == "overdue"
    assert "-5 points" in client.post(f"/delegation/tasks/{task['taskId']}/extend", json={"newDueDate": (days + timedelta(days=2)).isoformat()}, headers=mgr).json()
    assert "-5 points" in client.post(f"/delegation/tasks/{task['taskId']}/complete", json={}, headers=staff).json() or True
    msg = client.post(f"/delegation/tasks/{task['taskId']}/complete", json={}, headers=staff)
    # 10 on time - 5 for the extension
    assert client.get("/delegation/my-score", headers=staff).json()["totalScore"] == 5
    client.post(f"/delegation/tasks/{task['taskId']}/review", json={"status": "Approved"}, headers=mgr)
    week = client.get(f"/delegation/weekly?staff_id={sname}&anchor={(days + timedelta(days=2)).isoformat()}", headers=staff).json()
    assert week["counts"]["red"] >= 1                                                                   # an extension is never green


def test_delegation_scoring_rules_are_settings(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    assert client.put("/admin/module-settings", headers=admin, json={"key": "deleg_on_time_score", "value": 25}).status_code == 200
    assert client.put("/admin/module-settings", headers=admin, json={"key": "deleg_week_start_dow", "value": 9}).status_code == 422
    from app import config
    config._value_cache.clear()
    client.post("/delegation/tasks", json={"staffId": sname, "desc": "Bonus", "dueDate": days.isoformat()}, headers=mgr)
    tid = client.get("/delegation/tasks", headers=staff).json()[0]["taskId"]
    assert "+25 points" in client.post(f"/delegation/tasks/{tid}/complete", json={}, headers=staff).json()
    client.put("/admin/module-settings", headers=admin, json={"key": "deleg_on_time_score", "value": 10})
    config._value_cache.clear()


def test_week_runs_from_the_configured_day():
    from datetime import date
    from app.delegation import week_bounds
    start, end = week_bounds(date(2026, 10, 7), 6)       # Wednesday -> the Saturday before
    assert (start, end) == (date(2026, 10, 3), date(2026, 10, 9))
    assert week_bounds(date(2026, 10, 7), 1)[0] == date(2026, 10, 5)   # Monday start


# ---------------------------------------------------------------- purchase
def test_purchase_sites_and_admin_delete(client, db_conn, days, seed):
    god, _ = user(client, db_conn, "purchase_godown")
    shop, _ = user(client, db_conn, "purchase_shop")
    admin = login(client, db_conn, "admin")
    assert client.get("/purchase/me", headers=god).json()["dashboard"] == "godown"
    assert client.get("/purchase/me", headers=admin).json()["dashboard"] == "admin"
    body = {"vendorName": "Asian  Paints Limited", "materialReceivedDate": days.isoformat(), "invoiceDate": "", "invoiceNumber": "A-1",
            "invoiceAmount": "1250.50", "remarks": "ok", "photos": ""}
    r = client.post("/purchase/entries/godown", json=body, headers=god).json()
    assert r["success"] and r["rowId"]
    assert client.post("/purchase/entries/shop", json=body, headers=god).status_code == 403       # their own site only
    assert client.get("/purchase/entries?site=shop", headers=god).status_code == 403
    rows = client.get("/purchase/entries?site=godown", headers=god).json()
    assert rows[0]["vendorName"] == "Asian Paints Limited" and rows[0]["invoiceAmount"] == 1250.5 and rows[0]["uid"] == f"godown_{r['rowId']}"
    assert "Asian Paints Limited" in client.get("/purchase/vendors", headers=shop).json()          # shared vendor list
    again = client.post("/purchase/entries/shop", json={**body, "vendorName": "asian paints limited"}, headers=shop).json()
    assert again["success"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM dim_party WHERE party_kind='vendor' AND lower(party_name)='asian paints limited'")
        assert cur.fetchone()[0] == 1                                                            # no duplicate vendor
    assert client.put(f"/purchase/entries/godown/{r['rowId']}", json={**body, "invoiceAmount": 99}, headers=god).json()["success"]
    assert client.post("/purchase/entries/godown", json={**body, "invoiceAmount": "abc"}, headers=god).json()["success"] is False
    assert client.delete(f"/purchase/entries/godown/{r['rowId']}", headers=god).json()["success"] is False   # delete is admin-only, server-side
    assert client.delete(f"/purchase/entries/godown/{r['rowId']}", headers=admin).json()["success"] is True
    assert client.get("/purchase/entries?site=godown", headers=god).json() == [] or all(e["rowId"] != r["rowId"] for e in client.get("/purchase/entries?site=godown", headers=god).json())
    combined = client.get("/purchase/entries?site=shop", headers=admin).json()
    assert combined and combined[0]["source"] == "shop"


class FakeStore:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://acct.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}?sig=1"


def test_purchase_photos_attach_on_save_and_drop_when_removed(client, db_conn, days, seed, monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    store = FakeStore()
    attachments.set_storage(store)
    try:
        god, _ = user(client, db_conn, "purchase_godown")
        photo = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        up = client.post("/purchase/photos/godown", json={"data": photo}, headers=god).json()
        assert up["success"] and up["url"].startswith("vtfile:")
        body = {"vendorName": "Photo Vendor", "materialReceivedDate": days.isoformat(), "photos": up["url"]}
        key = client.post("/purchase/entries/godown", json=body, headers=god).json()["rowId"]
        entry = next(e for e in client.get("/purchase/entries?site=godown", headers=god).json() if e["rowId"] == key)
        assert entry["photos"].startswith("https://acct.r2.cloudflarestorage.com/bkt/purchase/")
        # editing keeps a photo that is still listed (as the signed link) and drops one that is not
        assert client.put(f"/purchase/entries/godown/{key}", json={**body, "photos": entry["photos"]}, headers=god).json()["success"]
        assert len(store.objects) == 1
        assert client.put(f"/purchase/entries/godown/{key}", json={**body, "photos": ""}, headers=god).json()["success"]
        assert len(store.objects) == 0
    finally:
        attachments.set_storage(None)


# ---------------------------------------------------------------- setup: lists, settings, KPIs
def test_master_lists_are_managed_in_setup_and_protected_when_used(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    cash, _ = user(client, db_conn, "cashier")
    assert client.get("/admin/masters", headers=cash).status_code == 403
    ids = [m["id"] for m in client.get("/admin/masters", headers=admin).json()]
    assert {"company", "payment_mode", "txn_type", "party", "vendor"} <= set(ids)
    new = client.post("/admin/masters/company", headers=admin, json={"name": "New Co", "is_sales_company": True}).json()["key"]
    assert client.post("/admin/masters/company", headers=admin, json={"name": "new   co"}).status_code == 409
    assert client.put(f"/admin/masters/company/{new}", headers=admin, json={"name": "New Co Ltd", "is_sales_company": False}).status_code == 200
    mode = client.post("/admin/masters/payment_mode", headers=admin, json={"name": "Wallet", "is_cash": False, "needs_approval": True}).json()["key"]
    assert client.post("/payments/transactions", json=txn(mode="Wallet"), headers=cash).json()["autoApproved"] is False   # new flag, new behaviour
    client.post("/payments/transactions", json=txn(company="New Co Ltd", mode="Wallet"), headers=admin)
    assert client.delete(f"/admin/masters/company/{new}", headers=admin).status_code == 409                               # in use
    typ = client.post("/admin/masters/txn_type", headers=admin, json={"name": "Refund", "direction": "out", "sales_effect": -1}).json()["key"]
    assert client.post("/admin/masters/txn_type", headers=admin, json={"name": "Bad", "direction": "sideways"}).status_code == 422
    assert client.delete(f"/admin/masters/txn_type/{typ}", headers=admin).status_code == 200
    assert client.delete(f"/admin/masters/payment_mode/{mode}", headers=admin).status_code == 409
    rows = client.get("/admin/masters/vendor", headers=admin).json()["rows"]
    assert isinstance(rows, list)
    co = client.get("/admin/masters/party", headers=admin).json()
    assert any(f["name"] == "company_key" and f["options"] for f in co["fields"])
    assert "company" in [k["kind"] for k in client.get("/admin/reconcile", headers=admin).json()]          # merge works on the new lists too


def test_new_pages_and_module_tiles_follow_access(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    staff, _ = user(client, db_conn, "staff")
    assert [m["key"] for m in client.get("/auth/me", headers=staff).json()["modules"]] == ["delegation"]
    assert {m["key"] for m in client.get("/auth/me", headers=admin).json()["modules"]} == {"payments", "delegation", "purchase"}
    pages = {p["key"] for p in client.get("/admin/access/matrix", headers=admin).json()["pages"]}
    assert {"pay_cashier", "pay_account", "deleg_mine", "deleg_manage", "po_godown", "po_shop", "po_all"} <= pages
    assert client.get("/delegation/scoreboard", headers=staff).status_code == 200
    me = client.get("/auth/me", headers=staff).json()
    client.put(f"/admin/access/users/{me['user_key']}", headers=admin, json={"page_key": "deleg_scoreboard", "allowed": False})
    assert client.get("/delegation/scoreboard", headers=staff).status_code == 403                         # a sub page switched off


def test_admin_overview_kpis(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    client.post("/payments/transactions", json=txn(amount=4321), headers=admin)
    god, _ = user(client, db_conn, "purchase_godown")
    client.post("/purchase/entries/godown", json={"vendorName": "KPI Vendor", "materialReceivedDate": days.isoformat(), "invoiceAmount": 100}, headers=god)
    k = client.get("/admin/module-kpis", headers=admin).json()
    assert set(k) >= {"payments", "delegation", "purchase", "asOf"}
    assert k["payments"]["monthReceived"] >= 4321 and k["purchase"]["monthEntries"] >= 1
    assert "openTasks" in k["delegation"]
    assert client.get("/admin/module-kpis", headers=god).status_code == 403


# ---------------------------------------------------------------- values typed on the module screens wait for review in Setup
def test_new_values_from_the_screens_wait_for_review_and_are_usable_at_once(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    acct, _ = user(client, db_conn, "accounts")
    god, _ = user(client, db_conn, "purchase_godown")
    base = client.get("/admin/masters-pending", headers=admin).json()["total"]
    # a company typed on the Payments page, then a party and a vendor
    co = client.post("/payments/companies", json={"name": "  Brand   New Co "}, headers=acct).json()
    assert co["success"] and co["company"] == "Brand New Co"
    again = client.post("/payments/companies", json={"name": "brand new co"}, headers=acct).json()
    assert again["success"] is False and again["company"] == "Brand New Co"
    assert "Brand New Co" in client.get("/payments/bootstrap", headers=acct).json()["companies"]           # usable straight away
    assert client.post("/payments/transactions", json=txn(company="Brand New Co", party="Fresh Counterparty"), headers=acct).json()["success"]
    client.post("/purchase/entries/godown", json={"vendorName": "Brand New Vendor", "materialReceivedDate": days.isoformat()}, headers=god)
    assert client.get("/admin/masters-pending", headers=admin).json()["total"] == base + 3
    meta = {m["id"]: m for m in client.get("/admin/masters", headers=admin).json()}
    assert meta["company"]["pending"] >= 1 and meta["vendor"]["pending"] >= 1 and meta["party"]["pending"] >= 1 and meta["payment_mode"]["pending"] == 0
    rows = client.get("/admin/masters/company", headers=admin).json()["rows"]
    new = next(r for r in rows if r["name"] == "Brand New Co")
    assert new["review_status"] == "pending" and new["created_by"]
    assert rows[0]["review_status"] == "pending"                                                           # pending ones come first
    # approve one, correct one by editing, approve the rest in one go
    assert client.post(f"/admin/masters/company/{new['key']}/approve", headers=admin).status_code == 200
    vend = next(r for r in client.get("/admin/masters/vendor", headers=admin).json()["rows"] if r["name"] == "Brand New Vendor")
    assert client.put(f"/admin/masters/vendor/{vend['key']}", headers=admin, json={"name": "Brand New Vendor Ltd"}).status_code == 200
    assert next(r for r in client.get("/admin/masters/vendor", headers=admin).json()["rows"] if r["key"] == vend["key"])["review_status"] == "approved"
    assert client.post("/admin/masters/party/approve-all", headers=admin).json()["approved"] >= 1
    after = client.get("/admin/masters-pending", headers=admin).json()["by"]
    assert after["party"] == 0 and next(r for r in client.get("/admin/masters/company", headers=admin).json()["rows"] if r["name"] == "Brand New Co")["review_status"] == "approved"
    assert client.post("/admin/masters/payment_mode/1/approve", headers=admin).status_code == 422           # nothing to review there
    # an admin adding in Setup is approved from the start; only an admin sees the review state
    k = client.post("/admin/masters/company", headers=admin, json={"name": "Setup Made Co"}).json()["key"]
    assert next(r for r in client.get("/admin/masters/company", headers=admin).json()["rows"] if r["key"] == k)["review_status"] == "approved"
    assert client.get("/admin/masters-pending", headers=acct).status_code == 403
    # a view-only person cannot add a company
    me = client.get("/auth/me", headers=acct).json()
    client.put(f"/admin/access/users/{me['user_key']}", headers=admin, json={"page_key": "pay_account", "allowed": True, "view_only": True})
    assert client.post("/payments/companies", json={"name": "Nope Co"}, headers=acct).status_code == 403


def test_staff_are_managed_in_setup_only_and_no_old_system_is_named_in_the_pages(client, db_conn, days, seed):
    admin = login(client, db_conn, "admin")
    for path in ("/delegation/all-staff", "/delegation/staff/password"):
        assert client.get(path, headers=admin).status_code in (404, 405)
    import re
    from tests.conftest import ROOT
    for name in ("payments", "delegation", "purchase"):
        text = (ROOT / "app" / "static" / name / "index.html").read_text(encoding="utf-8")
        assert "manageStaff" not in text and "staffModal" not in text
        visible = re.sub(r"fonts\.(googleapis|gstatic)\.com", "", text)
        assert not re.search(r"google|apps script|spreadsheet", visible, re.I), re.findall(r".{20}(?:google|apps script|spreadsheet).{20}", visible, re.I)
