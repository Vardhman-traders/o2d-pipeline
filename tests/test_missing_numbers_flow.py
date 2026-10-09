# ruff: noqa: F811
"""The missing-number panel on the Shop dashboard, end to end: orders 1 and 8 entered -> 2..7 listed; a listed number can be
clicked and entered; the list then shrinks; Invoice numbers work per financial year."""
from datetime import timedelta

from app import o2d_screens
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)


def _gaps(client, h):
    r = client.get("/o2d/doc-gaps", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_one_then_eight_lists_two_to_seven_and_a_listed_number_can_be_entered(client, db_conn, seed):
    today = o2d_screens.today_ist() - timedelta(days=2)   # a day of its own: the test database is shared by the whole run
    ensure_date(db_conn, today)
    shop = login(client, db_conn, "shop")
    add(client, shop, today, "1")
    add(client, shop, today, "8")
    g = _gaps(client, shop)
    assert g["canPunch"] is True
    day = next(d for d in g["challan"] if d["date"] == today.isoformat())
    assert [(r["from"], r["to"]) for r in day["ranges"]] == [(2, 7)] and day["highest"] == 8 and g["total"] >= 6

    # click 5 -> the pop-up posts the order with that number
    opts = client.get("/o2d/missing-entry/options", headers=shop).json()
    assert opts["ok"] and "Challan" in opts["typeOfSubmission"]
    r = client.post("/o2d/missing-entry", headers=shop, json={"orderRcvdDate": today.isoformat(), "orderVia": "Call",
                                                              "dcNo": "5", "typeOfSubmission": "Challan", "shippingLocation": "Rohini"})
    assert r.json()["success"] is True, r.text
    day = next(d for d in _gaps(client, shop)["challan"] if d["date"] == today.isoformat())
    assert [(x["from"], x["to"]) for x in day["ranges"]] == [(2, 4), (6, 7)]
    # the same number cannot be punched twice
    dup = client.post("/o2d/missing-entry", headers=shop, json={"orderRcvdDate": today.isoformat(), "orderVia": "Call",
                                                                "dcNo": "5", "typeOfSubmission": "Challan"}).json()
    assert dup["success"] is False and "already used" in dup["message"]


def test_the_panel_also_shows_for_other_roles_and_invoice_gaps_run_per_financial_year(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    shop, godown = login(client, db_conn, "shop"), login(client, db_conn, "godown")
    add(client, shop, today, "101", typ="Invoice")
    add(client, shop, today, "104", typ="Invoice")
    inv = _gaps(client, godown)["invoice"][0]
    assert inv["highest"] == 104 and inv["ranges"][0]["from"] == 1 and inv["ranges"][-1]["to"] == 103
    assert client.get("/o2d/missing-entry/options", headers=godown).status_code == 403   # not allowed to punch, only to see


def test_yesterdays_challan_gaps_are_kept_apart_from_todays(client, db_conn, seed):
    today = o2d_screens.today_ist() - timedelta(days=5)
    yesterday = today - timedelta(days=1)
    for d in (today, yesterday):
        ensure_date(db_conn, d)
    shop = login(client, db_conn, "shop")
    add(client, shop, yesterday, "3")
    add(client, shop, today, "2")
    by_day = {d["date"]: d for d in _gaps(client, shop)["challan"]}
    assert [(r["from"], r["to"]) for r in by_day[yesterday.isoformat()]["ranges"]] == [(1, 2)]
    assert [(r["from"], r["to"]) for r in by_day[today.isoformat()]["ranges"]] == [(1, 1)]
