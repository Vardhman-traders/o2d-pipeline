# ruff: noqa: F811, E501
"""Where an order shows on each dashboard, step by step (Shop -> Godown -> Dispatch -> Receiving):
 new order            -> Shop "New orders" + Godown "Pending orders", nowhere else
 delivery status set  -> Godown "Under processing" + the right "Ready for dispatch"
                         (Godown to Dispatch / Sent to the Company -> Godown Dispatch; Dispatch for Shop / Cancelled -> Shop Dispatch & Receiving)
 dispatch filled      -> "Awaiting receiving" on the same dispatch screen (Godown Dispatch ones also on Godown Receiving)
 receiving filled     -> gone from every Awaiting receiving; "Received" on Godown Receiving."""
import uuid

import pytest

from app import o2d_screens
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)


@pytest.fixture()
def people(client, db_conn, seed):
    with db_conn.cursor() as cur:
        for name in ("Godown to Dispatch", "Sent to the Company"):
            cur.execute("INSERT INTO dim_delivery_status (status_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
    db_conn.commit()
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    return {r: login(client, db_conn, r) for r in ("shop", "godown", "godown_dispatch", "shop_dispatch", "receiving", "admin")}, today


def _dc():
    return "5" + str(uuid.uuid4().int)[:8]


def _where(client, h, dc):
    """Every list this order is in, as {'screen: list', ...}."""
    found = set()

    def scan(screen, url, lists):
        data = client.get(url, headers=h[screen if screen != "godown_dispatch_admin" else "admin"]).json()
        for name in lists:
            if any(o["dcNo"] == dc for o in data.get(name, [])):
                found.add(f"{screen}:{name}")
    scan("shop", "/o2d/shop", ["recentOrders"])
    scan("godown", "/o2d/godown", ["pending", "completed"])
    scan("godown_dispatch", "/o2d/dispatch", ["pending", "completed"])
    scan("shop_dispatch", "/o2d/dispatch", ["pending", "completed"])
    scan("receiving", "/o2d/receiving", ["pending", "completed"])
    return found


def _sl(client, h, dc):
    r = client.get("/o2d/search", headers=h["admin"], params={"dc_no": dc}).json()
    return r["orders"][0]["slNo"] if r.get("orders") else None


def _dispatch(client, h, role, sl, **extra):
    body = {"materialDeliveryDateTime": o2d_screens.today_ist().isoformat() + "T10:00", "deliveredByWhom": "Amit", **extra}
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=h[role], json=body).json()
    assert r["success"] is True, r
    return r


def test_godown_to_dispatch_journey(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert _where(client, h, dc) == {"shop:recentOrders", "godown:pending"}                       # 1. only New orders + Pending orders

    r = client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"readyByWhom": "Ravi"}).json()   # info filled, no status yet
    assert r["success"] and _where(client, h, dc) == {"shop:recentOrders", "godown:pending"}

    assert client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": "Godown to Dispatch"}).json()["success"]
    assert _where(client, h, dc) == {"godown:completed", "godown_dispatch:pending"}                # 2a. Under processing + Godown Dispatch ready

    _dispatch(client, h, "godown_dispatch", sl)
    assert _where(client, h, dc) == {"godown_dispatch:completed", "receiving:pending"}             # 3b. Awaiting receiving on both; out of Under processing

    assert client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 100}).json()["success"]
    assert _where(client, h, dc) == {"receiving:completed"}                                        # 3c. out of every Awaiting receiving -> Received


def test_sent_to_the_company_behaves_like_godown_to_dispatch(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": "Sent to the Company"}).json()["success"]
    assert _where(client, h, dc) == {"godown:completed", "godown_dispatch:pending"}


def test_dispatch_for_shop_journey_uses_the_new_label_and_stays_off_godown_receiving(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert "Dispatch for Shop" in client.get("/o2d/godown", headers=h["godown"]).json()["dropdowns"]["deliveryStatus"]
    assert client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": "Dispatch for Shop"}).json()["success"]
    assert _where(client, h, dc) == {"godown:completed", "shop_dispatch:pending"}                  # 2b

    _dispatch(client, h, "shop_dispatch", sl)
    assert _where(client, h, dc) == {"shop_dispatch:completed"}                                    # 3a. Awaiting receiving on the same screen only

    assert client.put(f"/o2d/orders/{sl}/dispatch", headers=h["shop_dispatch"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 50}).json()["success"]
    assert _where(client, h, dc) == set()                                                          # 3c. received: out of the table


def test_cancelled_goes_straight_to_shop_dispatch_ready_and_never_to_godown_receiving(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": "Cancelled"}).json()["success"]
    assert _where(client, h, dc) == {"godown:completed", "shop_dispatch:pending"}                  # 2c
    _dispatch(client, h, "shop_dispatch", sl)
    assert _where(client, h, dc) == {"shop_dispatch:completed"}                                    # not on Godown Receiving


def test_admin_sees_the_same_lists_per_dispatch_screen(client, people):
    h, today = people
    new, godown_side, shop_side = _dc(), _dc(), _dc()
    for dc, status in ((godown_side, "Godown to Dispatch"), (shop_side, "Dispatch for Shop")):
        sl = add(client, h["shop"], today, dc)["slNo"]
        assert client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": status}).json()["success"]
    add(client, h["shop"], today, new)                                                             # still waiting for the godown

    def ready(view_as):
        data = client.get("/o2d/dispatch", params={"view_as": view_as}, headers=h["admin"]).json()
        return {o["dcNo"] for o in data["pending"]}
    assert godown_side in ready("godown_dispatch") and shop_side not in ready("godown_dispatch")
    assert shop_side in ready("shop_dispatch") and godown_side not in ready("shop_dispatch")
    assert new not in ready("godown_dispatch") | ready("shop_dispatch")                            # an unprocessed order is on no dispatch screen
