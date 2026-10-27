# ruff: noqa: F811, E501
"""Dashboard clean-up batch: the Overview board's order, All orders opening on the open orders, the complete admin edit form, the Setup > WhatsApp
list, the quick duplicate finder, and the screens' response cache (which the rest of the suite switches off)."""
import re
import time
import uuid
from datetime import date

from app import db, o2d_screens, reconcile, whatsapp
from tests.test_o2d_cashier import _dc, _received, _status
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)


# ------------------------------------------------------------------ the Overview board
def test_board_cards_run_from_the_smallest_number_to_the_largest(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    shop = login(client, db_conn, "shop")
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    dcs = [_dc() for _ in range(3)]
    for dc in reversed(dcs):                                     # punched largest first
        add(client, shop, today, dc)
    board = client.get("/o2d/admin/kanban?per_stage=500", headers=admin).json()
    column = next(s for s in board["stages"] if s["name"] == "Awaiting godown")["orders"]
    ours = [o["dcNo"] for o in column if o["dcNo"] in dcs]
    assert ours == sorted(dcs, key=int) and len(ours) == 3


# ------------------------------------------------------------------ All orders
def test_all_orders_opens_without_closed_entries_but_keeps_them_one_click_away(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash = _status(client, admin, True)
    closed_sl, _ = _received(client, db_conn, cash)
    open_sl = add(client, login(client, db_conn, "shop"), o2d_screens.today_ist(), _dc())["slNo"]

    def sls(**q):
        r = client.get("/admin/orders/search", params={"limit": 200, **q}, headers=admin).json()
        return {x["sl_no"] for x in r["rows"]}

    assert open_sl in sls(view="open") and closed_sl not in sls(view="open")      # the page's start view
    assert closed_sl in sls(view="closed") and closed_sl in sls(view="active")    # the Closed / Active cards still show it
    assert closed_sl in sls(sl_no=str(closed_sl), include_archived="true")        # and so does looking the order up by its number
    summary = client.get("/admin/orders/summary", headers=admin).json()
    assert summary["open"] >= 1 and summary["closed"] >= 1 and summary["active"] >= summary["open"] + 1


def test_the_admin_edit_form_reaches_every_column_of_the_pipeline(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    cash = _status(client, admin, True)
    sl, _ = _received(client, db_conn, cash, amount=7000)
    ensure_date(db_conn, date(2026, 10, 20))
    r = client.put(f"/o2d/orders/{sl}/admin", headers=admin, json={
        "deliveredByDetail": "Porter Ramesh", "dateOfReceiving": "2026-10-20", "materialDeliveryDateTime": "2026-10-19T14:30",
        "amountReceived": 7500, "cartage": 80}).json()
    assert r["success"] is True, r
    row = client.get("/admin/orders/search", params={"sl_no": str(sl), "include_archived": "true"}, headers=admin).json()["rows"][0]
    assert row["delivered_by_detail"] == "Porter Ramesh" and str(row["date_of_receiving"])[:10] == "2026-10-20"
    assert str(row["material_delivery_datetime"])[:16].replace(" ", "T") == "2026-10-19T14:30"
    assert float(row["amount_received_entered"]) == 7500 and float(row["cartage"]) == 80 and "amount_locked" in row
    # an empty optional number in the form leaves the amount alone instead of writing 0
    assert client.put(f"/o2d/orders/{sl}/admin", headers=admin, json={"amountReceived": "", "cartage": "", "detailedRemarks": "checked"}).json()["success"] is True
    with db_conn.cursor() as cur:
        cur.execute("SELECT amount_received, cartage FROM fact_orders WHERE sl_no = %s", (sl,))
        assert [float(x) for x in cur.fetchone()] == [7500.0, 80.0]


# ------------------------------------------------------------------ Setup
def test_setup_whatsapp_hides_the_delegation_and_checklist_alerts_but_they_keep_working(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    alerts = {a["key"]: a for a in client.get("/admin/whatsapp", headers=admin).json()["alerts"]}
    hidden = {k for k, a in alerts.items() if a["hidden"]}
    assert hidden == {"deleg_assigned", "deleg_done", "chk_assigned", "chk_done"}
    assert all(whatsapp.ALERTS[k].get("app_title") for k in hidden)                # the web bell they feed is untouched


def _party(i, name):
    return {"key": i, "name": name, "role": "payment", "phone": None, "sort_order": 0}


def test_duplicate_finder_is_exact_where_it_matters_and_fast_on_a_long_list(client):
    rows = [_party(1, "Sharma Traders"), _party(2, "sharma  traders."), _party(3, "Sharma Trader"), _party(4, "Gupta Stores"), _party(5, "Zed Sharma Traders")]
    pairs = {tuple(sorted(s["keys"])): s["score"] for s in reconcile.suggest(rows)}
    assert pairs[(1, 2)] == 1.0 and (1, 3) in pairs                                    # the same name two ways / a near spelling
    assert not any(4 in p or 5 in p for p in pairs)                                    # different first letter / different name: not a pair
    assert not reconcile.suggest(rows, 0.99)[1:]                                       # a stricter list asks for near-identical names
    many = [_party(i, f"{chr(97 + i % 26)}{uuid.uuid4().hex[:10]} trading company") for i in range(3000)]
    start = time.perf_counter()
    reconcile.suggest(many, 0.92, 40)
    assert time.perf_counter() - start < 5                                             # it used to compare every name with every other


# ------------------------------------------------------------------ the screens' response cache
def test_screen_responses_are_kept_until_something_is_written(client, db_conn, seed, monkeypatch):
    monkeypatch.setattr(db, "READ_CACHE_TTL", 15.0)
    monkeypatch.setattr(o2d_screens, "SCREEN_CACHE_TTL", 20.0)
    o2d_screens._SCREEN_CACHE.clear()
    db._read_cache.clear()
    try:
        shop, other = login(client, db_conn, "shop"), login(client, db_conn, "shop")
        today = o2d_screens.today_ist()
        ensure_date(db_conn, today)
        first = client.get("/o2d/shop", headers=shop)
        assert first.status_code == 200 and first.headers.get("content-encoding") == "gzip"
        assert client.get("/o2d/shop", headers=shop).json() == first.json()            # the same answer, from the copy
        assert len(o2d_screens._SCREEN_CACHE) == 1
        client.get("/o2d/shop", headers=other)
        assert len(o2d_screens._SCREEN_CACHE) == 2                                      # kept per person, never shared
        dc = _dc()
        add(client, shop, today, dc)                                                    # any committed write drops every copy
        assert dc in [o["dcNo"] for o in client.get("/o2d/shop", headers=shop).json()["recentOrders"]]
        assert dc in [o["dcNo"] for o in client.get("/o2d/shop", headers=other).json()["recentOrders"]]
        # without a gzip-capable client the plain JSON is sent (and kept separately)
        plain = client.get("/o2d/shop", headers={**shop, "Accept-Encoding": "identity"})
        assert plain.headers.get("content-encoding") is None and re.search(r'"recentOrders"', plain.text)
    finally:
        o2d_screens._SCREEN_CACHE.clear()
        db._read_cache.clear()
