"""VT Decor module: the rules the old Apps Script had, on the shared database (one targeted flow, not the whole suite)."""
import uuid

from app import attachments
from tests.test_modules import FakeStore, user
from tests.test_o2d_screens import PW, client, login, seed  # noqa: F401  (fixtures)


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
