# ruff: noqa: F811, E501
"""The client's feedback round, checked with dummy orders: the amount rule (entered / 100), Goods Return subtracted everywhere,
"Dispatch for Shop" only on the Godown dashboard, the delivery-status settings in Setup, the Under-processing card, cartage by the
date of receiving, and the daily delivery reports."""
import io
import re
import uuid
from datetime import timedelta

import pytest

from app import o2d_cartage, o2d_screens
from tests.test_order_journey import _clean_up, _dc, _dispatch, _status, people  # noqa: F401  (fixture + helpers)
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)


def _receive(client, h, sl, day, amount, payment="Paid", via="receiving"):
    r = client.put(f"/o2d/orders/{sl}/receiving", headers=h[via], json={"dateOfReceiving": day.isoformat(), "paymentStatus": payment, "amountReceived": amount}).json()
    assert r["success"] is True, r


def _pdf_text(raw: bytes) -> str:
    fitz = pytest.importorskip("fitz")
    return "".join(p.get_text() for p in fitz.open(stream=raw, filetype="pdf"))


def test_entered_amount_is_shown_divided_by_100_everywhere_but_the_form_keeps_what_was_entered(client, people, db_conn):
    h, today = people
    day = today - timedelta(days=21)
    ensure_date(db_conn, day)
    sl = add(client, h["shop"], day, _dc())["slNo"]
    _status(client, h, sl, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl)
    _receive(client, h, sl, today, 10000)
    row = next(o for o in client.get("/o2d/receiving", headers=h["receiving"]).json()["completed"] if o["slNo"] == sl)
    assert row["amountReceived"] == 100.0 and row["amountReceivedRaw"] == 10000.0                  # shown 100, form starts from 10000
    with db_conn.cursor() as cur:
        cur.execute("SELECT amount_received FROM fact_orders WHERE sl_no = %s", (sl,))
        assert float(cur.fetchone()[0]) == 10000.0                                                # the stored value is untouched
        cur.execute("SELECT amount_received, amount_received_entered FROM v_orders WHERE sl_no = %s", (sl,))
        shown, entered = cur.fetchone()
        assert float(shown) == 100.0 and float(entered) == 10000.0                                # the admin views follow the same rule


def test_goods_return_is_subtracted_in_dashboards_exports_and_pdf_reports(client, people, db_conn):
    h, today = people
    day = today - timedelta(days=22)               # a day of its own: the test database is shared by the whole run
    ensure_date(db_conn, day)
    sale, ret = _dc(), _dc()
    for dc, status, amount in ((sale, "Sent to the Company", 10000), (ret, "Goods Return", 3000)):
        sl = add(client, h["shop"], day, dc)["slNo"]
        _status(client, h, sl, status)
        _dispatch(client, h, "godown_dispatch", sl)               # Goods Return is a normal godown-side status: it goes to Godown Dispatch
        _receive(client, h, sl, day, amount)                      # received that same day: the received & payments PDF filters on the date of receiving
    seen = {o["dcNo"]: o["amountReceived"] for o in client.get("/o2d/receiving", headers=h["receiving"]).json()["completed"]}
    assert seen[sale] == 100.0 and seen[ret] == -30.0                                                # the return shows as minus

    from app import dashboard
    assert float(dashboard.headline(day, day)["amount_received"]) == 70.0                            # Overview totals: 100 - 30

    from openpyxl import load_workbook
    r = client.get("/admin/export.xlsx", params={"date_from": day.isoformat(), "date_to": day.isoformat()}, headers=h["admin"])
    summary = {a: b for a, b, *_ in load_workbook(io.BytesIO(r.content))["Summary"].iter_rows(values_only=True)}
    assert float(summary["Amount received (Rs)"]) == 70.0                                            # Excel export summary

    pdf = client.get("/o2d/reports/godown-received-other.pdf", params={"date_from": day.isoformat(), "date_to": day.isoformat()}, headers=h["admin"])
    text = _pdf_text(pdf.content)
    assert "100.00" in text and "-30.00" in text and "70.00" in text                                 # rows and the total of the PDF report


def test_dispatch_for_shop_is_offered_on_the_godown_dashboard_only_and_the_admin_can_change_that(client, people):
    h, _ = people
    names = lambda role: client.get("/o2d/godown" if role == "godown" else "/o2d/dispatch", headers=h[role]).json()["dropdowns"]["deliveryStatus"]  # noqa: E731
    assert "Dispatch for Shop" in names("godown")
    assert "Dispatch for Shop" not in names("godown_dispatch") and "Dispatch for Shop" not in names("shop_dispatch")
    assert "Cancelled" in names("godown") and "Cancelled" in names("shop_dispatch") and "Cancelled" in names("godown_dispatch")   # unchanged for now

    rows = client.get("/admin/lookups/delivery-statuses", headers=h["admin"]).json()
    shop = next(r for r in rows if r["name"] == "Shop")
    assert shop["screens"] == ["godown"] and shop["dispatch_route"] == "shop"
    ok = client.put(f"/admin/lookups/delivery-statuses/{shop['key']}", json={"name": "Shop", "screens": ["godown", "shop_dispatch"]}, headers=h["admin"])
    assert ok.status_code == 200 and "Dispatch for Shop" in names("shop_dispatch")                  # a built-in's "Offered on" can be edited
    client.put(f"/admin/lookups/delivery-statuses/{shop['key']}", json={"name": "Shop", "screens": ["godown"]}, headers=h["admin"])
    assert "Dispatch for Shop" not in names("shop_dispatch")


def test_delivery_status_settings_goes_to_and_amount_in_reports(client, people):
    h, today = people
    fields = client.get("/admin/lookup-fields", headers=h["admin"]).json()["delivery-statuses"]
    assert [f["name"] for f in fields] == ["dispatch_route", "amount_effect"]
    name = f"Held {uuid.uuid4().hex[:4]}"
    made = client.post("/admin/lookups/delivery-statuses", json={"name": name, "fields": {"dispatch_route": "none", "amount_effect": ""}}, headers=h["admin"])
    assert made.status_code == 201
    row = next(r for r in client.get("/admin/lookups/delivery-statuses", headers=h["admin"]).json() if r["name"] == name)
    assert row["dispatch_route"] == "none" and row["amount_effect"] is None
    bad = client.put(f"/admin/lookups/delivery-statuses/{row['key']}", json={"name": name, "fields": {"dispatch_route": "moon"}}, headers=h["admin"])
    assert bad.status_code == 422
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, name)
    ready = {o["dcNo"] for o in client.get("/o2d/dispatch", headers=h["godown_dispatch"]).json()["pending"]}
    assert dc not in ready                                                                           # "stays with the godown"
    client.put(f"/admin/lookups/delivery-statuses/{row['key']}", json={"name": name, "fields": {"dispatch_route": "godown"}}, headers=h["admin"])
    ready = {o["dcNo"] for o in client.get("/o2d/dispatch", headers=h["godown_dispatch"]).json()["pending"]}
    assert dc in ready                                                                               # the admin re-routes it, the order follows


def test_under_processing_card_counts_and_filters_like_a_stage(client, people):
    h, today = people
    in_process, new_one = _dc(), _dc()
    _status(client, h, add(client, h["shop"], today, in_process)["slNo"], "Processing")
    add(client, h["shop"], today, new_one)
    plain = client.get("/o2d/admin/kanban", headers=h["admin"]).json()
    assert plain["underProcessing"] >= 1 and "Under processing (godown)" not in [s["name"] for s in plain["stages"]]
    picked = client.get("/o2d/admin/kanban", params={"stage": "Under processing (godown)"}, headers=h["admin"]).json()
    column = next(s for s in picked["stages"] if s["name"] == "Under processing (godown)")
    cards = {c["dcNo"] for c in column["orders"]}
    assert in_process in cards and new_one not in cards and column["count"] == picked["underProcessing"]


def test_cartage_follows_the_date_of_receiving_not_the_order_or_delivery_date(client, people, db_conn):
    h, today = people
    created, received = today - timedelta(days=25), today - timedelta(days=24)
    for d in (created, received):
        ensure_date(db_conn, d)
    done, half = _dc(), _dc()
    sl = add(client, h["shop"], created, done)["slNo"]
    _status(client, h, sl, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl, cartage=75)
    _receive(client, h, sl, received, 500)
    sl2 = add(client, h["shop"], created, half)["slNo"]
    _status(client, h, sl2, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl2, cartage=40)
    with db_conn.cursor() as cur:       # payment status entered but no date of receiving: not a final entry
        cur.execute("UPDATE fact_orders SET payment_status_key = (SELECT payment_status_key FROM dim_payment_status LIMIT 1) WHERE sl_no = %s", (sl2,))
    db_conn.commit()
    on_receiving_day = {r["dc_inv_no"] for r in o2d_cartage.fetch_cartage(received, received)}
    on_creation_day = {r["dc_inv_no"] for r in o2d_cartage.fetch_cartage(created, created)}
    assert done in on_receiving_day and done not in on_creation_day                                  # dated by receiving
    assert half not in on_receiving_day and half not in {r["dc_inv_no"] for r in o2d_cartage.fetch_cartage(None, None)}


def test_daily_delivery_reports_are_built_stored_listed_and_downloadable(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl, cartage=60)
    assert client.post("/o2d/daily-reports/run", headers=h["receiving"]).status_code in (403, 422)   # only admin may build on demand
    built = client.post("/o2d/daily-reports/run", headers=h["admin"]).json()
    assert built["ok"] and built["persons"] >= 1
    listing = client.get("/o2d/daily-reports", headers=h["admin"]).json()
    mine = next(r for r in listing["reports"] if r["person"] == "Amit")
    assert mine["orders"] >= 1
    pdf = client.get(f"/o2d/daily-reports/{mine['key']}.pdf", headers=h["admin"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and dc in _pdf_text(pdf.content)


def test_scheduler_runs_the_day_once_after_7_30_pm_only(client, people):
    from datetime import datetime

    from app import daily_reports
    h, today = people
    sl = add(client, h["shop"], today, _dc())["slNo"]
    _status(client, h, sl, "Sent to the Company")
    _dispatch(client, h, "godown_dispatch", sl)
    base = datetime.now(daily_reports.IST)
    # a day far in the future has no run yet: nothing happens before 7:30 PM, it happens once after
    later = base.replace(year=base.year + 3, hour=19, minute=29)
    assert daily_reports.run_if_due(later) is False
    assert daily_reports.run_if_due(later.replace(minute=31)) is True
    assert daily_reports.run_if_due(later.replace(minute=45)) is False                  # the 7:30 PM batch is done; the 9:00 PM one is not due yet
    assert daily_reports.run_if_due(later.replace(hour=21, minute=5)) is True           # the second batch of the day
    assert daily_reports.run_if_due(later.replace(hour=22, minute=0)) is False          # each batch runs once a day
