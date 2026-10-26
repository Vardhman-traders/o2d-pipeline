# ruff: noqa: F811  (the shared fixtures are imported by name, the usual pytest pattern in this repo)
"""Advance taken at the shop counter: entered with the order (amount + payment mode), goes straight to the cashier, and is locked
for Shop Dispatch & Receiving and Godown Receiving. The cashier may correct it until it is merged into Payments; after that only an admin."""
from app import o2d_screens
from tests.test_o2d_cashier import _cashier, _confirm, _dc, _status
from tests.test_o2d_screens import client, ensure_date, login, seed  # noqa: F401  (fixtures)


def _shop_order(client, db_conn, shop, **extra):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    body = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": _dc(), "typeOfSubmission": "Challan", **extra}
    return client.post("/o2d/orders", json=body, headers=shop).json()


def _order_row(db_conn, sl):
    with db_conn.cursor() as cur:
        cur.execute("SELECT amount_received, payment_status_key, amount_locked_at, amount_locked_stage FROM fact_orders WHERE sl_no = %s", (sl,))
        return cur.fetchone()


def _to_shop_dispatch(client, db_conn, sl):
    assert client.put(f"/o2d/orders/{sl}/godown", headers=login(client, db_conn, "godown"), json={"deliveryStatus": "Shop"}).json()["success"]
    return login(client, db_conn, "shop_dispatch")


def test_shop_advance_reaches_the_cashier_at_once_and_is_locked_downstream(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash = _status(client, admin, True)
    cashier = _cashier(client, db_conn)
    shop = login(client, db_conn, "shop")
    assert cash in client.get("/o2d/shop", headers=shop).json()["dropdowns"]["advanceMode"]

    sl = _shop_order(client, db_conn, shop, advanceAmount=5000, advanceMode=cash)["slNo"]
    amount, mode, locked_at, stage = _order_row(db_conn, sl)
    assert float(amount) == 5000 and mode is not None and locked_at is not None and stage == "shop"

    # the cashier sees it straight away, before anything happened at the godown or the dispatch
    row = next(o for o in client.get("/o2d/cashier", headers=cashier).json()["awaiting"] if o["slNo"] == sl)
    assert row["paymentStatus"] == cash and row["amountReceived"] == 50.0 and row["receivedOn"] == "Shop (advance)"
    assert row["advance"] is True and row["canCorrect"] is True and row["amountRaw"] == 5000

    # shop dispatch & receiving: the locked values can be repeated but not changed; the rest of the receiving still saves
    sd = _to_shop_dispatch(client, db_conn, sl)
    today = o2d_screens.today_ist().isoformat()
    base = {"materialDeliveryDateTime": f"{today}T11:00", "deliveredByWhom": "Amit", "dateOfReceiving": today}
    changed = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json={**base, "paymentStatus": cash, "amountReceived": 9000}).json()
    assert changed["success"] is False and "locked" in changed["message"]
    assert float(_order_row(db_conn, sl)[0]) == 5000
    same = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json={**base, "paymentStatus": cash, "amountReceived": 5000}).json()
    assert same["success"] is True, same
    assert float(_order_row(db_conn, sl)[0]) == 5000 and _order_row(db_conn, sl)[2] is not None
    screen = client.get("/o2d/dispatch", headers=sd).json()
    shown = next(o for o in screen["pending"] + screen["completed"] if o["slNo"] == sl)
    assert shown["amountLocked"] is True and shown["dateOfReceiving"] == today


def test_godown_receiving_cannot_change_a_locked_amount_but_an_admin_can(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash, other = _status(client, admin, True), _status(client, admin, True)
    shop = login(client, db_conn, "shop")
    sl = _shop_order(client, db_conn, shop, advanceAmount=2000, advanceMode=cash)["slNo"]
    today = o2d_screens.today_ist().isoformat()
    godown_dispatch = login(client, db_conn, "godown_dispatch")
    assert client.put(f"/o2d/orders/{sl}/godown", headers=login(client, db_conn, "godown"), json={"deliveryStatus": "Delivered"}).json()["success"]
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=godown_dispatch, json={"materialDeliveryDateTime": f"{today}T11:00", "deliveredByWhom": "Amit"})
    assert r.json()["success"] is True, r.text

    receiving = login(client, db_conn, "receiving")
    refused = client.put(f"/o2d/orders/{sl}/receiving", headers=receiving, json={"dateOfReceiving": today, "paymentStatus": other, "amountReceived": 2000}).json()
    assert refused["success"] is False and "locked" in refused["message"]
    # the bulk "Mark done" follows the same rule, and refuses the whole batch
    bulk = client.put("/o2d/bulk/receiving", headers=receiving, json={"items": [{"slNo": sl, "form": {
        "dateOfReceiving": today, "paymentStatus": other, "amountReceived": 1}}]}).json()
    assert bulk["success"] is False and "locked" in bulk["message"]

    ok = client.put(f"/o2d/orders/{sl}/receiving", headers=receiving, json={"dateOfReceiving": today, "paymentStatus": cash, "amountReceived": 2000}).json()
    assert ok["success"] is True, ok

    # an admin may still correct it (All orders > Edit); the lock stays
    fixed = client.put(f"/o2d/orders/{sl}/admin", headers=admin, json={"paymentStatus": cash, "amountReceived": 2500}).json()
    assert fixed["success"] is True, fixed
    amount, _, locked_at, _ = _order_row(db_conn, sl)
    assert float(amount) == 2500 and locked_at is not None


def test_advance_needs_both_fields_and_a_cashier_mode(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash, plain = _status(client, admin, True), _status(client, admin, False)
    shop = login(client, db_conn, "shop")
    for extra in ({"advanceAmount": 500}, {"advanceMode": cash}, {"advanceAmount": 0, "advanceMode": cash},
                  {"advanceAmount": "abc", "advanceMode": cash}, {"advanceAmount": 500, "advanceMode": plain},
                  {"advanceAmount": 500, "advanceMode": "No Such Mode"}):
        r = _shop_order(client, db_conn, shop, **extra)
        assert r["success"] is False, extra
    # no advance: the order is saved exactly as before, unlocked
    sl = _shop_order(client, db_conn, shop)["slNo"]
    assert _order_row(db_conn, sl)[0] is None and _order_row(db_conn, sl)[2] is None
    # the shop role still cannot write the amount through the generic order API
    assert client.patch(f"/orders/{sl}", headers=shop, json={"amount_received": 100}).status_code == 403


def test_cashier_corrects_a_pending_advance_but_not_a_merged_one(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash, bank = _status(client, admin, True), _status(client, admin, True)
    cashier = _cashier(client, db_conn)
    shop = login(client, db_conn, "shop")
    sl = _shop_order(client, db_conn, shop, advanceAmount=50000, advanceMode=cash)["slNo"]   # typed 50000 instead of 5000

    # not an advance -> refused (an ordinary receiving amount is corrected by an admin)
    plain_sl = _shop_order(client, db_conn, shop)["slNo"]
    assert client.put(f"/o2d/cashier/{plain_sl}/correct", headers=cashier, json={"amount": 1, "paymentStatus": cash}).json()["success"] is False

    assert client.put(f"/o2d/cashier/{sl}/correct", headers=cashier, json={"amount": 50000, "paymentStatus": cash}).json()["success"] is False   # unchanged
    assert client.put(f"/o2d/cashier/{sl}/correct", headers=cashier, json={"amount": 0, "paymentStatus": cash}).status_code == 422
    fixed = client.put(f"/o2d/cashier/{sl}/correct", headers=cashier, json={"amount": 5000, "paymentStatus": bank, "note": "typo"}).json()
    assert fixed["success"] is True, fixed
    amount, _, locked_at, _ = _order_row(db_conn, sl)
    assert float(amount) == 5000 and locked_at is not None
    row = next(o for o in client.get("/o2d/cashier", headers=cashier).json()["awaiting"] if o["slNo"] == sl)
    assert row["paymentStatus"] == bank and row["amountReceived"] == 50.0

    # confirmed and waiting in Payments: a correction sends it back to be confirmed again (the database clears the stale confirmation)
    assert _confirm(client, cashier, sl).json()["success"] is True
    assert client.put(f"/o2d/cashier/{sl}/correct", headers=cashier, json={"amount": 6000, "paymentStatus": bank}).json()["success"] is True
    assert sl in {o["slNo"] for o in client.get("/o2d/cashier", headers=cashier).json()["awaiting"]}

    # merged into Payments: only an admin can change it now
    assert _confirm(client, cashier, sl).json()["success"] is True
    with db_conn.cursor() as cur:
        cur.execute("UPDATE payment_push SET status = 'merged', merged_at = now() WHERE order_key = (SELECT order_key FROM fact_orders WHERE sl_no = %s)", (sl,))
    db_conn.commit()
    late = client.put(f"/o2d/cashier/{sl}/correct", headers=cashier, json={"amount": 7000, "paymentStatus": bank}).json()
    assert late["success"] is False and "admin" in late["message"]

    # the shop cannot use the cashier's correction either
    assert client.put(f"/o2d/cashier/{sl}/correct", headers=shop, json={"amount": 1, "paymentStatus": cash}).status_code == 403
    with db_conn.cursor() as cur:
        cur.execute("SELECT title FROM order_event WHERE event_type = 'cashier' AND title LIKE 'Cashier corrected the advance%%' "
                    "AND order_key = (SELECT order_key FROM fact_orders WHERE sl_no = %s)", (sl,))
        assert len(cur.fetchall()) == 2
