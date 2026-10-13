# ruff: noqa: F811  (the shared fixtures are imported by name, the usual pytest pattern in this repo)
"""O2D Cashier dashboard (app/o2d_cashier.py): which payment statuses send an order to the cashier is a Setup
setting, the cashier confirms the money, and a confirmation never outlives a change to the payment it confirmed."""
import uuid

from app import o2d_screens
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)


def _status(client, admin, flagged):
    """A new payment status through Setup > Dropdown values, ticked (or not) for the Cashier dashboard."""
    name = ("CashT " if flagged else "OtherT ") + uuid.uuid4().hex[:6]
    r = client.post("/admin/lookups/payment-statuses", headers=admin,
                    json={"name": name, "fields": {"cashier_review": "yes" if flagged else ""}})
    assert r.status_code == 201, r.text
    return name


def _dc() -> str:
    """A fresh bill number (the Challan series wants digits only)."""
    return str(100000 + uuid.uuid4().int % 800000)


def _received(client, db_conn, payment, amount=5000):
    """An order delivered and received on Shop Dispatch & Receiving with `payment` selected."""
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    sl = add(client, login(client, db_conn, "shop"), today, _dc())["slNo"]
    godown = login(client, db_conn, "godown")
    assert client.put(f"/o2d/orders/{sl}/godown", headers=godown, json={"deliveryStatus": "Shop"}).json()["success"]
    sd = login(client, db_conn, "shop_dispatch")
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json={
        "materialDeliveryDateTime": f"{today.isoformat()}T11:00", "deliveredByWhom": "Amit",
        "dateOfReceiving": today.isoformat(), "paymentStatus": payment, "amountReceived": amount}).json()
    assert r["success"] is True, r
    return sl, sd


def _cashier(client, db_conn):
    with db_conn.cursor() as cur:   # the migration's default, restated so a test that edited the matrix cannot break us
        cur.execute("INSERT INTO page_access (page_key, role) VALUES ('o2d_cashier', 'cashier') ON CONFLICT DO NOTHING")
    db_conn.commit()
    return login(client, db_conn, "cashier")


def _confirm(client, who, sl, **body):
    return client.put(f"/o2d/cashier/{sl}/confirm", headers=who, json=body)


def _awaiting(client, who):
    return {o["slNo"] for o in client.get("/o2d/cashier", headers=who).json()["awaiting"]}


def test_cashier_flow_and_a_changed_payment_needs_confirming_again(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash, other = _status(client, admin, True), _status(client, admin, False)
    cashier = _cashier(client, db_conn)
    sl, sd = _received(client, db_conn, cash)
    plain_sl, _ = _received(client, db_conn, other)

    d = client.get("/o2d/cashier", headers=cashier).json()
    mine = {o["slNo"]: o for o in d["awaiting"]}
    assert sl in mine and plain_sl not in mine                     # only the ticked payment status lands here
    assert mine[sl]["paymentStatus"] == cash and mine[sl]["amountReceived"] == 50.0   # entered / 100, like all screens
    assert mine[sl]["receivedOn"] == "Shop Dispatch & Receiving" and d["canEdit"] is True
    assert d["summary"]["awaiting"]["orders"] >= 1

    # an order that is not a cashier payment cannot be confirmed
    assert _confirm(client, cashier, plain_sl).json()["success"] is False

    assert _confirm(client, cashier, sl, note="counted").json()["success"] is True
    d = client.get("/o2d/cashier", headers=cashier).json()
    assert sl not in {o["slNo"] for o in d["awaiting"]}
    done = next(o for o in d["confirmed"] if o["slNo"] == sl)
    assert done["note"] == "counted" and done["confirmedBy"].startswith("t_cashier_")
    assert _confirm(client, cashier, sl).json()["success"] is False   # already confirmed

    # the amount is corrected afterwards: the old confirmation no longer stands
    fix = {"paymentStatus": cash, "amountReceived": 7000}
    assert client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json=fix).json()["success"]
    d = client.get("/o2d/cashier", headers=cashier).json()
    assert sl in {o["slNo"] for o in d["awaiting"]} and sl not in {o["slNo"] for o in d["confirmed"]}

    # confirmed by mistake: take it back
    assert _confirm(client, cashier, sl).json()["success"] is True
    assert _confirm(client, cashier, sl, confirmed=False).json()["success"] is True
    assert sl in _awaiting(client, cashier)

    # the confirmation is part of the order's timeline
    with db_conn.cursor() as cur:
        cur.execute("SELECT title FROM order_event WHERE event_type = 'cashier' AND order_key = "
                    "(SELECT order_key FROM fact_orders WHERE sl_no = %s) ORDER BY event_key", (sl,))
        titles = [r[0] for r in cur.fetchall()]
    assert any("confirmed" in t and cash in t for t in titles)
    assert titles[-1] == "Cashier confirmation taken back"


def test_cancelled_orders_are_listed_as_negative_sales(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash = _status(client, admin, True)
    cashier = _cashier(client, db_conn)
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    sl = add(client, login(client, db_conn, "shop"), today, _dc())["slNo"]
    godown = login(client, db_conn, "godown")
    body = {"deliveryStatus": "Cancelled"}
    assert client.put(f"/o2d/orders/{sl}/godown", headers=godown, json=body).json()["success"]
    sd = login(client, db_conn, "shop_dispatch")
    recv = {"dateOfReceiving": today.isoformat(), "paymentStatus": cash, "amountReceived": 5000}
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json=recv).json()
    assert r["success"] is True, r
    mine = {o["slNo"]: o for o in client.get("/o2d/cashier", headers=cashier).json()["awaiting"]}
    assert sl in mine and mine[sl]["cancelled"] is True and mine[sl]["amountReceived"] == -50.0
    assert _confirm(client, cashier, sl).json()["success"] is True


def test_cashier_page_access_follows_setup_and_view_only_cannot_confirm(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    sl, _ = _received(client, db_conn, _status(client, admin, True))
    shop = login(client, db_conn, "shop")
    assert client.get("/o2d/cashier", headers=shop).status_code == 403            # not on the shop's pages
    assert _confirm(client, shop, sl).status_code == 403

    cashier = _cashier(client, db_conn)
    me = client.get("/auth/me", headers=cashier).json()
    person = f"/admin/access/users/{me['user_key']}"
    client.put(person, headers=admin, json={"page_key": "o2d_cashier", "allowed": True, "view_only": True})
    assert client.get("/o2d/cashier", headers=cashier).json()["canEdit"] is False
    assert _confirm(client, cashier, sl).status_code == 403
    client.put(person, headers=admin, json={"page_key": "o2d_cashier", "allowed": None})
    assert _confirm(client, cashier, sl).json()["success"] is True


def test_cashier_is_the_last_screen_and_its_setup_controls_are_listed(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    views = [v["view"] for v in client.get("/auth/me", headers=admin).json()["o2d_views"]]
    assert views[-1] == "cashier"
    # Setup > Access: switch the page on / off per role or per person
    pages = {p["key"] for p in client.get("/admin/access/matrix", headers=admin).json()["pages"]}
    assert "o2d_cashier" in pages
    # Setup > Dropdown values > Payment status: which values go to the cashier
    fields = client.get("/admin/lookup-fields", headers=admin).json()["payment-statuses"]
    assert fields[0]["name"] == "cashier_review"
    rows = client.get("/admin/lookups/payment-statuses", headers=admin).json()
    assert all("cashier_review" in r for r in rows)
