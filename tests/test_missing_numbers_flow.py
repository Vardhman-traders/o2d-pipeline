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


def test_an_either_type_is_tagged_to_one_series_chosen_on_the_form(client, db_conn, seed):
    """A "Both" submission type means "either": the order form asks Challan or Invoice and the order counts in that series only."""
    today = o2d_screens.today_ist() - timedelta(days=8)
    ensure_date(db_conn, today)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_submission_type (type_name, bill_series) VALUES ('Challan+Invoice', 'both') ON CONFLICT DO NOTHING")
    db_conn.commit()
    shop = login(client, db_conn, "shop")
    body = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "typeOfSubmission": "Challan+Invoice"}
    # the form must say which series; the old "12/345" way is gone
    no_pick = client.post("/o2d/orders", json={**body, "dcNo": "12"}, headers=shop).json()
    assert no_pick["success"] is False and "Challan or an Invoice" in no_pick["message"]
    assert client.post("/o2d/orders", json={**body, "dcNo": "12/345", "billSeries": "challan"}, headers=shop).json()["success"] is False
    assert client.post("/o2d/orders", json={**body, "dcNo": "1", "billSeries": "challan"}, headers=shop).json()["success"] is True
    assert client.post("/o2d/orders", json={**body, "dcNo": "4", "billSeries": "challan"}, headers=shop).json()["success"] is True
    assert client.post("/o2d/orders", json={**body, "dcNo": "7000", "billSeries": "invoice"}, headers=shop).json()["success"] is True
    assert client.post("/o2d/orders", json={**body, "dcNo": "7002", "billSeries": "invoice"}, headers=shop).json()["success"] is True
    # the challan ones count only as challans (1 and 4 -> 2, 3 missing), the invoice ones only as invoices (7001 missing)
    g = client.get("/o2d/doc-gaps", headers=shop).json()
    day = next(d for d in g["challan"] if d["date"] == today.isoformat())
    assert [(r["from"], r["to"]) for r in day["ranges"]] == [(2, 3)] and day["highest"] == 4
    assert any(r["from"] <= 7001 <= r["to"] for fy in g["invoice"] for r in fy["ranges"])
    # a number cannot repeat inside its own series: a plain Challan 4 that day, or another "either" order tagged Challan 4
    dup1 = client.post("/o2d/missing-entry", headers=shop, json={"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": "4",
                                                                 "typeOfSubmission": "Challan"}).json()
    assert dup1["success"] is False and "already used" in dup1["message"]
    dup2 = client.post("/o2d/orders", json={**body, "dcNo": "4", "billSeries": "challan"}, headers=shop).json()
    assert dup2["success"] is False and "already used" in dup2["message"]
    # ... but the same digits in the OTHER series are a different number (an admin enters it: a small invoice number among 7000s is
    # otherwise refused as "looks like a challan number")
    admin = login(client, db_conn, "admin")
    assert client.post("/o2d/orders", json={**body, "dcNo": "4", "billSeries": "invoice"}, headers=admin).json()["success"] is True
    # the form hint follows the pick
    q = {"type_name": "Challan+Invoice", "date": today.isoformat(), "number": "5"}
    assert client.get("/o2d/doc-check", params=q, headers=shop).json()["needsChoice"] is True
    chk = client.get("/o2d/doc-check", params={**q, "bill": "challan"}, headers=shop).json()
    assert chk["series"] == "Challan" and chk["duplicate"] is False
    assert client.get("/o2d/doc-check", params={**q, "number": "4", "bill": "challan"}, headers=shop).json()["duplicate"] is True
    # the form is told which types ask
    assert "Challan+Invoice" in client.get("/o2d/shop", headers=shop).json()["dropdowns"]["eitherTypes"]


def test_the_built_in_cancelled_type_can_be_either_series(client, db_conn, seed):
    with db_conn.cursor() as cur:   # a Cancelled type created later gets the same setting (migration 030 set the existing one)
        cur.execute("INSERT INTO dim_submission_type (type_name) VALUES ('Cancelled') ON CONFLICT DO NOTHING")
        cur.execute("SELECT bill_series FROM dim_submission_type WHERE lower(btrim(type_name)) = 'cancelled'")
        assert [r[0] for r in cur.fetchall()] == ["both"]
