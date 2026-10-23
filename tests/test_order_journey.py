# ruff: noqa: F811, E501
"""Where an order shows on each dashboard, step by step (Shop -> Godown -> Dispatch -> Receiving), with dummy orders:
 new order            -> Shop "New Orders" (the tracker) + Godown "Pending Orders", nowhere else
 delivery status set  -> Godown "Under Processing" + the status's "Goes to" dispatch screen (Shop box keeps tracking it):
                         Godown to Dispatch / Sent to the Company -> Godown Dispatch, Dispatch for Shop / Cancelled -> Shop Dispatch & Receiving,
                         Processing / Delivery for NextDay -> no dispatch screen
 dispatch filled      -> Godown Dispatch: Awaiting receiving there and on Godown Receiving; Shop Dispatch: still "Ready" until receiving is filled
 receiving filled     -> fully closed: "Received" (Godown Receiving / Shop Dispatch & Receiving), Shop box still shows it until 11 PM
 11 PM clean-up       -> closed orders disappear from every dashboard (the data stays)."""
from datetime import timedelta

import pytest

from app import o2d_screens
from tests.conftest import bill_no
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)


@pytest.fixture()
def people(client, db_conn, seed):
    with db_conn.cursor() as cur:
        for name in ("Godown to Dispatch", "Sent to the Company", "Processing", "Delivery for NextDay", "Goods Return"):
            cur.execute("INSERT INTO dim_delivery_status (status_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        cur.execute("UPDATE dim_delivery_status SET dispatch_route = 'none' WHERE status_name IN ('Processing', 'Delivery for NextDay')")
        cur.execute("UPDATE dim_delivery_status SET amount_effect = 'subtract' WHERE status_name = 'Goods Return'")
    db_conn.commit()
    with db_conn.cursor() as cur:   # these tests pin the 11 PM clean-up, which is now a setting that is off by default
        cur.execute("INSERT INTO app_config (key, value) VALUES ('o2d_cleanup_enabled', '1') ON CONFLICT (key) DO UPDATE SET value = '1'")
    db_conn.commit()
    from app import config
    config._value_cache.clear()
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    yield {r: login(client, db_conn, r) for r in ("shop", "godown", "godown_dispatch", "shop_dispatch", "receiving", "admin")}, today
    with db_conn.cursor() as cur:   # back to the default (off) for the rest of the session
        cur.execute("DELETE FROM app_config WHERE key = 'o2d_cleanup_enabled'")
    db_conn.commit()
    config._value_cache.clear()


def _dc():
    return bill_no()


def _where(client, h, dc):
    """Every list this order is in, as {'screen:list', ...}."""
    found = set()

    def scan(screen, url, lists):
        data = client.get(url, headers=h[screen]).json()
        for name in lists:
            if any(o["dcNo"] == dc for o in data.get(name, [])):
                found.add(f"{screen}:{name}")
    scan("shop", "/o2d/shop", ["recentOrders"])
    scan("godown", "/o2d/godown", ["pending", "completed"])
    scan("godown_dispatch", "/o2d/dispatch", ["pending", "completed"])
    scan("shop_dispatch", "/o2d/dispatch", ["pending", "completed"])
    scan("receiving", "/o2d/receiving", ["pending", "completed"])
    return found


def _dispatch(client, h, role, sl, **extra):
    body = {"materialDeliveryDateTime": o2d_screens.today_ist().isoformat() + "T10:00", "deliveredByWhom": "Amit", **extra}
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=h[role], json=body).json()
    assert r["success"] is True, r
    return r


def _status(client, h, sl, status):
    r = client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"deliveryStatus": status}).json()
    assert r["success"] is True, r


def _clean_up(db_conn, sl):
    """What 11 PM does to a closed order: it was last touched before the most recent 11 PM."""
    with db_conn.cursor() as cur:
        cur.execute("UPDATE fact_orders SET last_updated_at = now() - interval '2 days' WHERE sl_no = %s", (sl,))
    db_conn.commit()


def test_godown_to_dispatch_journey(client, people, db_conn):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert _where(client, h, dc) == {"shop:recentOrders", "godown:pending"}                       # new order: New Orders + Pending only

    r = client.put(f"/o2d/orders/{sl}/godown", headers=h["godown"], json={"readyByWhom": "Ravi"}).json()   # info filled, no status yet
    assert r["success"] and _where(client, h, dc) == {"shop:recentOrders", "godown:pending"}

    _status(client, h, sl, "Godown to Dispatch")
    assert _where(client, h, dc) == {"shop:recentOrders", "godown_dispatch:pending"}   # left the godown: only Godown Dispatch ready (not Under Processing)

    _dispatch(client, h, "godown_dispatch", sl)
    assert _where(client, h, dc) == {"shop:recentOrders", "godown_dispatch:completed", "receiving:pending"}   # Awaiting receiving on both

    assert client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 100}).json()["success"]
    assert _where(client, h, dc) == {"shop:recentOrders", "receiving:completed"}                   # closed: Received, Shop box still shows today's

    _clean_up(db_conn, sl)
    assert _where(client, h, dc) == set()                                                          # 11 PM: gone from every dashboard


def test_sent_to_the_company_behaves_like_godown_to_dispatch(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Sent to the Company")
    assert _where(client, h, dc) == {"shop:recentOrders", "godown_dispatch:pending"}


@pytest.mark.parametrize("status", ["Processing", "Delivery for NextDay"])
def test_statuses_that_stay_with_the_godown_reach_no_dispatch_screen(client, people, status):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, status)
    assert _where(client, h, dc) == {"shop:recentOrders", "godown:completed"}                       # Logic 1: Under Processing + New Orders only


def test_dispatch_for_shop_journey_uses_the_new_label_and_closes_on_the_shop_screen(client, people, db_conn):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    assert "Dispatch for Shop" in client.get("/o2d/godown", headers=h["godown"]).json()["dropdowns"]["deliveryStatus"]
    _status(client, h, sl, "Dispatch for Shop")
    assert _where(client, h, dc) == {"shop:recentOrders", "shop_dispatch:pending"}

    _dispatch(client, h, "shop_dispatch", sl)                                                      # dispatched, receiving not filled yet
    assert _where(client, h, dc) == {"shop:recentOrders", "shop_dispatch:pending"}                 # stays in Ready, tagged "Receiving pending"
    row = next(o for o in client.get("/o2d/dispatch", headers=h["shop_dispatch"]).json()["pending"] if o["dcNo"] == dc)
    assert row["stuckReason"] == "Receiving pending"

    assert client.put(f"/o2d/orders/{sl}/dispatch", headers=h["shop_dispatch"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 50}).json()["success"]
    assert _where(client, h, dc) == {"shop:recentOrders", "shop_dispatch:completed"}               # Received box; never on Godown Receiving

    _clean_up(db_conn, sl)
    assert _where(client, h, dc) == set()


def test_cancelled_goes_straight_to_shop_dispatch_ready_and_never_to_godown_receiving(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Cancelled")
    assert _where(client, h, dc) == {"shop:recentOrders", "shop_dispatch:pending"}
    _dispatch(client, h, "shop_dispatch", sl)
    assert _where(client, h, dc) == {"shop:recentOrders", "shop_dispatch:pending"}                 # not on Godown Receiving


def test_received_box_shows_to_every_user_of_the_screen_not_only_the_one_who_closed_it(client, people, db_conn):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl)
    assert client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid"}).json()["success"]
    second = login(client, db_conn, "receiving")                                                   # a different receiving login
    seen = client.get("/o2d/receiving", headers=second).json()["completed"]
    assert dc in {o["dcNo"] for o in seen}


def test_admin_sees_the_same_lists_per_dispatch_screen(client, people):
    h, today = people
    new, godown_side, shop_side, stays = _dc(), _dc(), _dc(), _dc()
    for dc, status in ((godown_side, "Godown to Dispatch"), (shop_side, "Dispatch for Shop"), (stays, "Processing")):
        _status(client, h, add(client, h["shop"], today, dc)["slNo"], status)
    add(client, h["shop"], today, new)                                                             # still waiting for the godown

    def ready(view_as):
        data = client.get("/o2d/dispatch", params={"view_as": view_as}, headers=h["admin"]).json()
        return {o["dcNo"] for o in data["pending"]}
    assert godown_side in ready("godown_dispatch") and shop_side not in ready("godown_dispatch")
    assert shop_side in ready("shop_dispatch") and godown_side not in ready("shop_dispatch")
    assert not ({new, stays} & (ready("godown_dispatch") | ready("shop_dispatch")))                # unprocessed / "stays" orders: on no dispatch screen


def test_old_open_orders_stay_on_the_shop_box_but_old_closed_ones_do_not(client, people, db_conn):
    h, today = people
    old_open, old_closed = _dc(), _dc()
    for dc in (old_open, old_closed):
        add(client, h["shop"], today - timedelta(days=0), dc)
    sl_closed = next(o["slNo"] for o in client.get("/o2d/shop", headers=h["shop"]).json()["recentOrders"] if o["dcNo"] == old_closed)
    _status(client, h, sl_closed, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl_closed)
    client.put(f"/o2d/orders/{sl_closed}/receiving", headers=h["receiving"], json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid"})
    with db_conn.cursor() as cur:
        cur.execute("UPDATE fact_orders SET last_updated_at = now() - interval '3 days' WHERE dc_inv_no IN (%s, %s)", (old_open, old_closed))
    db_conn.commit()
    shop_seen = {o["dcNo"] for o in client.get("/o2d/shop", headers=h["shop"]).json()["recentOrders"]}
    assert old_open in shop_seen and old_closed not in shop_seen


def test_godown_dispatch_never_lists_shop_side_orders_whoever_opens_it(client, people, db_conn):
    """The side belongs to the screen, not the person: any role given the Godown Dispatch page (and admin) sees godown-route orders only."""
    h, today = people
    godown_side, shop_side = _dc(), _dc()
    for dc, status in ((godown_side, "Godown to Dispatch"), (shop_side, "Dispatch for Shop")):
        _status(client, h, add(client, h["shop"], today, dc)["slNo"], status)
    viewer = login(client, db_conn, "purchase_shop")
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO page_access (page_key, role) VALUES ('o2d_godown_dispatch', 'purchase_shop') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO role_view_access (role, can_view) VALUES ('purchase_shop', true) ON CONFLICT (role) DO UPDATE SET can_view = true")
    db_conn.commit()
    for url in ("/o2d/dispatch", "/o2d/dispatch?view_as=godown_dispatch"):
        seen = {o["dcNo"] for o in client.get(url, headers=viewer).json()["pending"]}
        assert godown_side in seen and shop_side not in seen, url
    seen = {o["dcNo"] for o in client.get("/o2d/dispatch?view_as=godown_dispatch", headers=h["admin"]).json()["pending"]}
    assert godown_side in seen and shop_side not in seen
    # asking for a screen they were not given just gives them the one they hold
    seen = {o["dcNo"] for o in client.get("/o2d/dispatch?view_as=shop_dispatch", headers=viewer).json()["pending"]}
    assert godown_side in seen and shop_side not in seen
    with db_conn.cursor() as cur:   # leave the shared test database as found
        cur.execute("DELETE FROM page_access WHERE page_key = 'o2d_godown_dispatch' AND role = 'purchase_shop'")
        cur.execute("DELETE FROM role_view_access WHERE role = 'purchase_shop'")
    db_conn.commit()


def test_without_the_11_pm_clean_up_closed_orders_stay_listed(client, people, db_conn):
    """The clean-up is a setting that is off by default: a fully closed order stays on its dashboards (until it is archived)."""
    from app import config
    h, today = people
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM app_config WHERE key = 'o2d_cleanup_enabled'")
    db_conn.commit()
    config._value_cache.clear()
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    _dispatch(client, h, "godown_dispatch", sl)
    assert client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"],
                      json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 100}).json()["success"]
    _clean_up(db_conn, sl)                                           # closed two days ago
    assert _where(client, h, dc) == {"shop:recentOrders", "receiving:completed"}
