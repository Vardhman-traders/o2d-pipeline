"""VT Decor module: the rules the old Apps Script had, on the shared database (one targeted flow, not the whole suite)."""
import uuid

from app import attachments
from tests.test_modules import FakeStore, user
from tests.test_o2d_screens import PW, client, ensure_date, login, seed  # noqa: F401  (fixtures)


def rpc(http, headers, action, /, **payload):
    r = http.post("/decor/rpc", json={"action": action, "payload": payload}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def staff(client, db_conn, page="decor_staff", view_only=False):
    """A person who holds a decor page by a per-person exception (their role is irrelevant)."""
    headers, name = user(client, db_conn, "godown")
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, view_only) "
                    "SELECT user_key, %s, true, %s FROM dim_user WHERE username = %s", (page, view_only, name))
    db_conn.commit()
    return headers, name.lower()


def test_decor_flow(client, db_conn, seed, monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    store = FakeStore()
    attachments.set_storage(store)
    try:
        owner = login(client, db_conn, "admin")
        asha, asha_name = staff(client, db_conn)
        ravi, _ = staff(client, db_conn)
        stranger, _ = user(client, db_conn, "godown")
        assert client.post("/decor/rpc", json={"action": "getClients"}, headers=stranger).status_code == 403

        cfg = rpc(client, owner, "getConfig")
        wall = next(t for t in cfg["productTypes"] if t["name"] == "Wallpaper")
        assert [s["pct"] for s in wall["stages"]][:4] == [40, 30, 20, 10] and cfg["role"] == "owner"
        assert "Cash" in cfg["paymentModes"] and any(s["id"] == asha_name for s in cfg["staff"])

        # a staff member adds a client; only they and the owner see it
        made = rpc(client, asha, "addClient", name="Mrs Gupta", mobile="9999900000", amount=100000, productType="Wallpaper")
        code = made["clientId"]
        assert made["ok"] and code.startswith("C")
        assert [c["clientId"] for c in rpc(client, asha, "getClients")["active"]] == [code]
        assert rpc(client, ravi, "getClients")["active"] == []
        assert rpc(client, ravi, "getClientDetail", clientId=code)["msg"] == "Access denied"
        assert any(c["clientId"] == code for c in rpc(client, owner, "getClients")["active"])
        assert rpc(client, asha, "addClient", name="", mobile="1")["ok"] is False

        # payments land on stages; the wallpaper stage target is a share of the contract
        stage1 = wall["stages"][0]["name"]
        assert rpc(client, asha, "addPayment", clientId=code, stage=stage1, amount=40000, mode="UPI")["ok"]
        assert rpc(client, asha, "addPayment", clientId=code, stage=wall["stages"][1]["name"], amount=10000, mode="Cash")["ok"]
        d = rpc(client, asha, "getClientDetail", clientId=code)
        assert d["client"]["totalPaid"] == 50000 and d["client"]["balance"] == 50000
        st = {s["stage"]: s for s in d["stages"]}
        assert st[stage1]["status"] == "Received" and st[stage1]["target"] == 40000
        assert st[wall["stages"][1]["name"]]["status"] == "Partial"
        assert st[wall["stages"][2]["name"]]["status"] == "Pending"

        # items raise the contract; revising, deleting and editing are the owner's
        assert rpc(client, asha, "addItem", clientId=code, product="Border", amount=5000)["ok"]
        assert rpc(client, owner, "getClientDetail", clientId=code)["client"]["amount"] == 105000
        assert rpc(client, asha, "reviseAmount", clientId=code, newAmount=90000)["msg"] == "Not authorized"
        assert rpc(client, owner, "reviseAmount", clientId=code, newAmount=90000, reason="Scope cut")["ok"]
        pay_id = d["payments"][0]["rowIndex"]
        assert rpc(client, asha, "deletePayment", clientId=code, rowIndex=pay_id)["msg"] == "Not authorized"
        assert rpc(client, owner, "deletePayment", clientId=code, rowIndex=pay_id)["ok"]
        assert rpc(client, owner, "deletePayment", clientId=code, rowIndex=pay_id)["ok"] is False

        # an expense with a challan: the photo is kept; without storage it is skipped, not lost
        jpeg = "/9j/4AAQSkZJRg=="
        e = rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Labour", amount=3000, base64=jpeg, mimeType="image/jpeg", filename="c.jpg")
        assert e["ok"] and "photoSkipped" not in e and len(store.objects) == 1
        pdf = rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Transport", amount=500, base64="JVBERi0=", mimeType="application/pdf", filename="c.pdf")
        assert pdf["ok"] and len(store.objects) == 2
        bad = rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Misc", amount=10, base64="AAAA", mimeType="text/html", filename="x.html")
        assert bad["ok"] and bad["photoSkipped"] is True
        photos = rpc(client, asha, "getPhotos", clientId=code)["photos"]
        assert len(photos) == 2 and photos[0]["url"].startswith("https://acct.r2.cloudflarestorage.com/bkt/decor/")
        assert any(".pdf" in p["url"] for p in photos)
        allp = rpc(client, owner, "getAllPhotos")
        assert allp["total"] >= 2 and all(p["clientId"] for p in allp["photos"])
        assert rpc(client, asha, "deletePhoto", clientId=code, fileId=photos[0]["fileId"])["msg"] == "Not authorized"
        assert rpc(client, owner, "deletePhoto", clientId=code, fileId=photos[0]["fileId"])["ok"] and len(store.objects) == 1

        # GP = received - expenses (not contract - expenses)
        gp = next(s for s in rpc(client, owner, "getGPData")["sites"] if s["clientId"] == code)
        assert gp["paid"] == 10000 and gp["expenses"] == 3510 and gp["gp"] == 6490 and gp["gpPct"] == "64.9"
        led = rpc(client, owner, "getAllBusinessLedger")
        row = next(s for s in led["sites"] if s["clientId"] == code)
        assert row["payStatus"] == "Partial" and row["balance"] == 80000 and rpc(client, asha, "getAllBusinessLedger")["msg"] == "Not authorized"
        assert rpc(client, asha, "getAgingReport")["buckets"]["b0"][0]["clientId"] == code

        # status moves: completed clients leave the active list, hidden ones leave the ledger
        assert rpc(client, owner, "markCompleted", clientId=code)["ok"]
        assert [c["clientId"] for c in rpc(client, asha, "getClients")["completed"]] == [code]
        assert rpc(client, owner, "hideClient", clientId=code)["ok"]
        assert all(s["clientId"] != code for s in rpc(client, owner, "getAllBusinessLedger")["sites"])

        # fixed expenses and stage percentages are the owner's, and Setup-driven
        assert rpc(client, owner, "saveFixedExp", month=10, year=2026, rent=15000, salaries=40000, notes="Oct")["ok"]
        fx = rpc(client, owner, "getFixedExp", month=10, year=2026)["data"]
        assert fx["rent"] == 15000 and fx["salaries"] == 40000 and fx["notes"] == "Oct"
        assert rpc(client, asha, "saveFixedExp", month=10, year=2026)["msg"] == "Not authorized"
        assert rpc(client, owner, "saveStagePcts", p1=50, p2=30, p3=10, p4=10)["ok"]
        assert rpc(client, owner, "getStagePcts")["p1"] == 50
        assert rpc(client, owner, "saveStagePcts", p1=90, p2=30, p3=10, p4=10)["ok"] is False
        rpc(client, owner, "saveStagePcts", p1=40, p2=30, p3=20, p4=10)

        # quotations: numbered VT-0001..., the same number is never issued twice, staff see only their own
        n1 = rpc(client, asha, "getNextPerformaNo")["next"]
        q = dict(performa_no=n1, client="ABC", mobile="1", subtotal=1000, cgst_pct=9, sgst_pct=9, grand=1180, items=[{"d": "x"}])
        s1 = rpc(client, asha, "saveQuotation", **q)
        s2 = rpc(client, ravi, "saveQuotation", **q)
        assert s1["performaNo"] == n1 and s2["performaNo"] != n1
        assert [x["performaNo"] for x in rpc(client, asha, "getQuotations")["list"]] == [n1]
        assert {n1, s2["performaNo"]} <= {x["performaNo"] for x in rpc(client, owner, "getQuotations")["list"]}

        # deleting a client takes its payments, expenses and files with it (and leaves a note in the audit log)
        assert rpc(client, owner, "deleteClient", clientId=code)["ok"]
        assert rpc(client, owner, "getClientDetail", clientId=code)["ok"] is False and len(store.objects) == 0
    finally:
        attachments.set_storage(None)


def test_view_only_staff_cannot_save(client, db_conn, seed):
    viewer, _ = staff(client, db_conn, view_only=True)
    assert rpc(client, viewer, "getClients")["ok"]
    assert "view-only" in rpc(client, viewer, "addClient", name="N", mobile="1")["msg"]


def test_decor_lists_are_set_up_in_setup(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    masters = {m["id"] for m in client.get("/admin/masters", headers=admin).json()}
    assert {"decor_type", "decor_stage", "decor_expense_type"} <= masters
    types = client.get("/admin/masters/decor_type", headers=admin).json()["rows"]
    tex = next(t for t in types if t["name"] == "Texture")
    name = "Stage Z " + uuid.uuid4().hex[:4]
    r = client.post("/admin/masters/decor_stage", json={"name": name, "product_type_key": tex["key"], "target_pct": 0}, headers=admin)
    assert r.status_code == 201, r.text
    cfg = rpc(client, admin, "getConfig")
    stages = [s["name"] for s in next(t for t in cfg["productTypes"] if t["name"] == "Texture")["stages"]]
    assert stages[-1] == name                                  # a new stage goes to the end of its type
    assert client.put("/admin/module-settings", json={"key": "decor_quote_terms", "value": "Pay on time\nNo returns"}, headers=admin).status_code == 200
    assert rpc(client, admin, "getConfig")["quoteTerms"] == "Pay on time\nNo returns"


def test_nobody_sees_anyone_elses_decor_data(client, db_conn, seed):
    """Only the person who made an entry - or an administrator - can see or touch it. Owner-page holders are not exempt."""
    admin = login(client, db_conn, "admin")
    asha, _ = staff(client, db_conn)
    boss, _ = staff(client, db_conn, page="decor_owner")      # holds the Owner page but is not an administrator
    ravi, _ = staff(client, db_conn)

    code = rpc(client, asha, "addClient", name="Asha Client", mobile="9000000001", amount=50000, productType="Wallpaper")["clientId"]
    pay = rpc(client, asha, "addPayment", clientId=code, amount=1000, mode="Cash", stage="Stage 1 - Visit")
    assert pay["ok"]
    assert rpc(client, asha, "addExpense", clientId=code, type="Labour", amount=200)["ok"]
    assert rpc(client, asha, "addItem", clientId=code, product="Border", amount=10)["ok"]
    d = rpc(client, asha, "getClientDetail", clientId=code)
    pay_id, exp_id, item_id = d["payments"][0]["rowIndex"], d["expenses"][0]["rowIndex"], d["items"][0]["rowIndex"]
    qn = rpc(client, asha, "saveQuotation", client="Q", subtotal=1, grand=1, items=[])["performaNo"]

    for who in (ravi, boss):
        assert rpc(client, who, "getClients") == {"ok": True, "active": [], "completed": [], "hidden": []}
        for action in ("getClientDetail", "getPartyLedger", "getPaymentsList", "getExpensesList", "getItems", "getPhotos"):
            assert rpc(client, who, action, clientId=code)["ok"] is False, action
        assert rpc(client, who, "getAgingReport")["buckets"] == {"b0": [], "b30": [], "b60": [], "b90": []}
        assert rpc(client, who, "getMonthlyCollection")["monthly"] == {}
        assert rpc(client, who, "getGPData")["sites"] == []
        assert rpc(client, who, "getAllPhotos")["photos"] == []
        assert rpc(client, who, "getQuotations")["list"] == []
        assert qn not in str(rpc(client, who, "getQuotations"))
        assert len(rpc(client, who, "getConfig")["staff"]) == 1          # no other person's name
        # nothing of theirs can reach it by writing either
        assert rpc(client, who, "addPayment", clientId=code, amount=5)["ok"] is False
        assert rpc(client, who, "addExpense", clientId=code, type="x", amount=5)["ok"] is False
        assert rpc(client, who, "addItem", clientId=code, product="x", amount=5)["ok"] is False
        assert rpc(client, who, "savePhotoLink", clientId=code, photoUrl="http://x")["ok"] is False
    for action in ("getAllBusinessLedger", "getFixedExp", "saveFixedExp", "saveStagePcts"):
        assert rpc(client, boss, action, month=1, year=2026)["msg"] == "Not authorized", action

    # the Owner page gives edit/delete buttons - on their own clients only; ids guessed from outside do nothing
    assert rpc(client, boss, "editPayment", clientId=code, rowIndex=pay_id, amount=9)["ok"] is False
    assert rpc(client, boss, "deletePayment", clientId=code, rowIndex=pay_id)["ok"] is False
    assert rpc(client, boss, "editExpense", clientId=code, rowIndex=exp_id, amount=9)["ok"] is False
    assert rpc(client, boss, "deleteExpense", clientId=code, rowIndex=exp_id)["ok"] is False
    assert rpc(client, boss, "deleteItem", clientId=code, rowIndex=item_id)["ok"] is False
    for action in ("reviseAmount", "markCompleted", "markActive", "hideClient", "deleteClient"):
        assert rpc(client, boss, action, clientId=code, newAmount=1)["ok"] is False, action
    mine = rpc(client, boss, "addClient", name="Boss Client", mobile="9000000002", amount=100, productType="Texture")["clientId"]
    assert [c["clientId"] for c in rpc(client, boss, "getClients")["active"]] == [mine]
    assert rpc(client, boss, "markCompleted", clientId=mine)["ok"]
    assert [c["clientId"] for c in rpc(client, asha, "getClients")["active"]] == [code]        # and Asha cannot see Boss's

    # still intact, and the administrator sees both people's work
    after = rpc(client, asha, "getClientDetail", clientId=code)
    assert len(after["payments"]) == 1 and after["payments"][0]["amount"] == 1000 and len(after["items"]) == 1
    seen = {c["clientId"] for c in rpc(client, admin, "getClients")["active"] + rpc(client, admin, "getClients")["completed"]}
    assert {code, mine} <= seen
    assert qn in str(rpc(client, admin, "getQuotations")) and len(rpc(client, admin, "getConfig")["staff"]) > 1
    assert rpc(client, admin, "getAllBusinessLedger")["ok"]


def test_assigned_client_is_visible_to_the_assignee_only(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    asha, asha_name = staff(client, db_conn)
    ravi, _ = staff(client, db_conn)
    code = rpc(client, admin, "addClient", name="Assigned", mobile="9000000003", amount=500, productType="Wallpaper")["clientId"]
    assert rpc(client, asha, "getClientDetail", clientId=code)["ok"] is False
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dim_decor_client SET assigned_to = %s WHERE client_code = %s", (asha_name, code))
    db_conn.commit()
    assert rpc(client, asha, "addPayment", clientId=code, amount=50)["ok"]
    assert [c["clientId"] for c in rpc(client, asha, "getClients")["active"]] == [code]
    assert rpc(client, ravi, "getClientDetail", clientId=code)["ok"] is False
    assert rpc(client, ravi, "addPayment", clientId=code, amount=50)["ok"] is False


def test_assign_clients_and_edit_delete_quotations_and_decor_money_through_the_payments_merge(client, db_conn, seed):
    from app import modcommon
    ensure_date(db_conn, modcommon.today_ist())
    owner = login(client, db_conn, "admin")
    asha, asha_name = staff(client, db_conn)
    ravi, _ = staff(client, db_conn)
    nodecor, nodecor_name = user(client, db_conn, "godown")

    # a client the owner has (like an imported one): the staff member sees nothing until the administrator assigns it
    code = rpc(client, owner, "addClient", name="Imported Sharma", mobile="9999911111", amount=50000, productType="Texture")["clientId"]
    assert rpc(client, asha, "getClients")["active"] == []
    assert rpc(client, asha, "assignClients", clientIds=[code], staffId=asha_name.lower())["msg"] == "Not authorized"      # administrators only
    assert "no VT Decor access" in rpc(client, owner, "assignClients", clientIds=[code], staffId=nodecor_name.lower())["msg"]
    assert "not found" in rpc(client, owner, "assignClients", clientIds=[code], staffId="nobody-here")["msg"]
    done = rpc(client, owner, "assignClients", clientIds=[code, "C999"], staffId=asha_name.lower())
    assert done["ok"] and done["assigned"] == 1 and done["missing"] == ["C999"]
    assert [c["clientId"] for c in rpc(client, asha, "getClients")["active"]] == [code]
    assert rpc(client, ravi, "getClients")["active"] == []
    waiting = lambda kind: client.get("/payments/merge", headers=owner).json()["counts"][kind]
    before_in, before_out = waiting("decor_in"), waiting("decor_out")      # the test database is shared: count what this test adds
    assert rpc(client, asha, "addPayment", clientId=code, stage="", amount=1000, mode="UPI")["ok"]

    # VT Decor money reaches Payments through the merge step (like Sales / Sales Return) and is then tagged "VT Decor"
    q = client.get("/payments/merge", headers=owner).json()
    assert q["counts"]["decor_in"] == before_in + 1
    row = next(r for r in q["rows"] if r["kind"] == "decor_in" and r["dcNo"] == code)
    assert row["dcNo"] == code and "Imported Sharma" in row["address"] and row["orderAmount"] == 1000 and row["split"] == "bank" and row["bank"] == 1000
    body = {"date": row["date"], "company": "IRIS", "party": "Imported Sharma", "cashAmount": 0, "bankAmount": 1000, "remarks": "decor stage"}
    assert client.post(f"/payments/merge/{row['key']}/merge", json={**body, "bankAmount": 400}, headers=owner).json()["success"] is False   # must add up
    assert client.post(f"/payments/merge/{row['key']}/merge", json={**body, "cashAmount": 1000, "bankAmount": 0}, headers=owner).json()["success"] is False  # UPI = bank
    done = client.post(f"/payments/merge/{row['key']}/merge", json=body, headers=owner).json()
    assert done["success"], done
    assert len(done["txnIds"]) == 1
    entries = [r for r in client.get("/payments/transactions", headers=owner).json()["rows"] if r["TxnID"] == done["txnIds"][0]]
    assert entries and entries[0]["Source"] == "VT Decor" and entries[0]["Type"] == "Payment Received" and entries[0]["Amount"] == 1000 and entries[0]["Status"] == "Pending"
    # changing the VT Decor payment afterwards brings the row back as "already merged", and the entry carries a warning
    with db_conn.cursor() as cur:
        cur.execute("SELECT p.payment_key FROM fact_decor_payment p JOIN dim_decor_client c ON c.client_key = p.client_key WHERE c.client_code = %s", (code,))
        pk = cur.fetchone()
        pk = pk["payment_key"] if isinstance(pk, dict) else pk[0]
    assert rpc(client, owner, "editPayment", rowIndex=pk, amount=1500, mode="UPI", stage="", date=row["date"])["ok"]
    back = next(r for r in client.get("/payments/merge", headers=owner).json()["rows"] if r["kind"] == "decor_in" and r["dcNo"] == code)
    assert back["alreadyMerged"] and back["editedAfterMerge"] and back["orderAmount"] == 1500
    assert [r["Warning"] for r in client.get("/payments/transactions", headers=owner).json()["rows"] if r["TxnID"] == done["txnIds"][0]][0]
    # an expense is staged too (cash or bank is the merger's choice) and a deleted entry that nobody merged just disappears
    assert rpc(client, asha, "addExpense", clientId=code, type="Labour", amount=250, date=row["date"])["ok"]
    out = next(r for r in client.get("/payments/merge", headers=owner).json()["rows"] if r["kind"] == "decor_out" and r["dcNo"] == code)
    assert out["split"] == "both" and out["orderAmount"] == 250 and out["cash"] == 250
    assert client.post(f"/payments/merge/{out['key']}/merge", json={"date": out["date"], "company": "IRIS", "party": "", "cashAmount": 250, "bankAmount": 0, "remarks": ""}, headers=owner).json()["success"]
    assert rpc(client, asha, "addExpense", clientId=code, type="Tea", amount=10, date=row["date"])["ok"]
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM fact_decor_expense WHERE expense_type = 'Tea'")
    db_conn.commit()
    assert waiting("decor_out") == before_out

    # quotations: the maker and the administrator can open, edit and delete; somebody else cannot
    quote = {"client": "Mrs Rao", "mobile": "98", "addr": "A-1", "date": "2026-10-01", "subtotal": 100, "cgst_pct": 9, "sgst_pct": 9, "grand": 118,
             "items": [{"desc": "Wallpaper roll", "area": "Hall", "qty": 2, "mrp": 50, "disc": 0, "discAmt": 0, "amt": 100}]}
    no = rpc(client, asha, "saveQuotation", **quote)["performaNo"]
    full = rpc(client, asha, "getQuotation", performaNo=no)
    assert full["ok"] and full["client"] == "Mrs Rao" and full["items"][0]["desc"] == "Wallpaper roll" and full["cgstPct"] == 9
    assert rpc(client, ravi, "getQuotation", performaNo=no)["msg"] == "Access denied"
    assert rpc(client, asha, "updateQuotation", performa_no=no, **{**quote, "client": "Mrs Rao Jr", "grand": 236, "subtotal": 200})["ok"]
    assert rpc(client, owner, "getQuotation", performaNo=no)["client"] == "Mrs Rao Jr"
    assert rpc(client, owner, "updateQuotation", performa_no=no, **{**quote, "client": "Owner edit"})["ok"]
    assert rpc(client, asha, "updateQuotation", performa_no=no, **{**quote, "client": ""})["ok"] is False
    assert rpc(client, ravi, "deleteQuotation", performaNo=no)["msg"] == "Access denied"
    assert rpc(client, asha, "deleteQuotation", performaNo=no)["ok"]
    assert rpc(client, owner, "getQuotation", performaNo=no)["ok"] is False
