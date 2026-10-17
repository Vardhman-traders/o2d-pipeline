# ruff: noqa: F811, E501
"""Order changes log, bulk "Mark done", kanban search, sales-return series and the Under Processing rule (migration 039 batch)."""
from datetime import date

from app import doc_numbers as dn
from tests.test_o2d_screens import add, client, seed  # noqa: F401  (fixtures + helpers)
from tests.test_o2d_features import r2  # noqa: F401  (fake photo storage)
from tests.test_order_journey import _dc, _status, people  # noqa: F401


def test_sales_return_series_use_the_challan_and_invoice_reset_rules():
    d = date(2026, 10, 2)
    assert dn.series_key("Sales Return Challan", d) == "2026-10-02"      # daily, like Challan
    assert dn.series_key("Sales Return Invoice", d) == "2026-27"         # financial year, like Invoice
    assert dn._window("Sales Return Challan", d) == (d, d)
    assert dn._window("Sales Return Invoice", d) == (date(2026, 4, 1), date(2027, 3, 31))
    assert dn.SERIES["sales_return_challan"] == "Sales Return Challan" and dn.slug("Sales Return Invoice") == "sales_return_invoice"
    assert dn.number_in("Sales Return Challan", "sales_return_challan", "12") == 12


def test_sales_return_series_can_be_tagged_and_numbers_stay_apart(client, people, db_conn):
    h, today = people
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_submission_type (type_name, bill_series) VALUES ('SR Challan', 'sales_return_challan') ON CONFLICT DO NOTHING")
    db_conn.commit()
    ok = add(client, h["shop"], today, "88417", typ="SR Challan")
    assert ok["success"]
    again = client.post("/o2d/orders", headers=h["shop"], json={"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": "88417", "typeOfSubmission": "SR Challan"}).json()
    assert not again["success"] and "Sales Return Challan 88417 is already used" in again["message"]
    gaps = client.get("/o2d/doc-gaps", headers=h["shop"]).json()
    assert [g["date"] for g in gaps["salesReturnChallan"]] == [today.isoformat()] and gaps["salesReturnChallan"][0]["highest"] == 88417
    assert "Sales Return Challan" in gaps["seriesTypes"]
    other = add(client, h["shop"], today, "88417", typ="Challan")            # a plain Challan 7 is a different series
    assert other["success"]


def test_under_processing_keeps_only_statuses_that_stay_with_the_godown(client, people):
    h, today = people
    stays, leaves = _dc(), _dc()
    for dc, status in ((stays, "Processing"), (leaves, "Godown to Dispatch")):
        _status(client, h, add(client, h["shop"], today, dc)["slNo"], status)
    under = {o["dcNo"] for o in client.get("/o2d/godown", headers=h["godown"]).json()["completed"]}
    assert stays in under and leaves not in under


def test_bulk_dispatch_is_all_or_nothing_and_each_order_keeps_its_own_event(client, people, db_conn):
    h, today = people
    sls = []
    for _ in range(2):
        sl = add(client, h["shop"], today, _dc())["slNo"]
        _status(client, h, sl, "Godown to Dispatch")
        sls.append(sl)
    form = {"materialDeliveryDateTime": today.isoformat() + "T10:00", "deliveredByWhom": "Amit", "viewAs": "godown_dispatch"}
    refused = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"], json={"slNos": [sls[0], 99999999], "form": form}).json()
    assert refused["success"] is False and "nothing was saved" in refused["message"]
    first = next(o for o in client.get("/o2d/dispatch", headers=h["godown_dispatch"]).json()["pending"] if o["slNo"] == sls[0])
    assert not first["materialDeliveryDateTime"]                         # rolled back
    missing = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"], json={"slNos": sls, "form": {"viewAs": "godown_dispatch"}}).json()
    assert missing["success"] is False and "Missing required" in missing["message"]

    ok = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"], json={"slNos": sls, "form": form}).json()
    assert ok["success"] is True and ok["count"] == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT o.sl_no, count(*) AS n FROM order_event e JOIN fact_orders o ON o.order_key = e.order_key "
                    "WHERE o.sl_no = ANY(%s) AND e.event_type = 'dispatch' GROUP BY o.sl_no", (sls,))
        assert {r[0] if not isinstance(r, dict) else r["sl_no"]: (r[1] if not isinstance(r, dict) else r["n"]) for r in cur.fetchall()} == {sls[0]: 1, sls[1]: 1}
    assert client.put("/o2d/bulk/dispatch", headers=h["shop"], json={"slNos": sls, "form": form}).status_code == 403   # no dispatch page


def test_bulk_receiving_closes_the_selected_orders(client, people):
    h, today = people
    sls = []
    for _ in range(2):
        sl = add(client, h["shop"], today, _dc())["slNo"]
        _status(client, h, sl, "Godown to Dispatch")
        client.put(f"/o2d/orders/{sl}/dispatch", headers=h["godown_dispatch"], json={"materialDeliveryDateTime": today.isoformat() + "T10:00", "deliveredByWhom": "Amit"})
        sls.append(sl)
    r = client.put("/o2d/bulk/receiving", headers=h["receiving"], json={"slNos": sls, "form": {"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid"}}).json()
    assert r["success"] is True and r["count"] == 2
    done = {o["slNo"] for o in client.get("/o2d/receiving", headers=h["receiving"]).json()["completed"]}
    assert set(sls) <= done


def test_order_changes_log_is_admin_only_and_filters_by_dashboard(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    assert client.get("/admin/order-events", headers=h["shop"]).status_code == 403
    allv = client.get(f"/admin/order-events?q={sl}", headers=h["admin"]).json()
    assert [e["type"] for e in allv["events"]] == ["created", "godown"] and allv["events"][1]["slNo"] == sl
    assert {"id": "godown", "label": "Godown dashboard"} in allv["screens"]
    only = client.get(f"/admin/order-events?q={dc}&screen=godown", headers=h["admin"]).json()["events"]
    assert [e["type"] for e in only] == ["godown"] and only[0]["changes"]
    assert client.get("/admin/order-events?screen=nope", headers=h["admin"]).status_code == 422


def test_kanban_search_finds_by_s_number_or_invoice_number(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    add(client, h["shop"], today, _dc())
    for term in (str(sl), dc):
        stages = client.get(f"/o2d/admin/kanban?find={term}", headers=h["admin"]).json()["stages"]
        found = [o["slNo"] for s in stages for o in s["orders"]]
        assert sl in found and (term != dc or found == [sl])
    assert all("photoCount" in o for s in client.get("/o2d/admin/kanban", headers=h["admin"]).json()["stages"] for o in s["orders"])


def test_admin_can_see_what_a_user_sees_on_cashier_and_cartage(client, people, db_conn):
    h, today = people
    assert client.get("/o2d/cashier/visible-to", headers=h["shop"]).status_code == 403
    for kind in ("cashier", "cartage"):
        who = client.get(f"/o2d/{kind}/visible-to", headers=h["admin"]).json()
        assert who["ok"] and isinstance(who["users"], list)
    with db_conn.cursor() as cur:
        cur.execute("SELECT user_key FROM dim_user WHERE role = 'shop' LIMIT 1")
        row = cur.fetchone()
    key = row["user_key"] if isinstance(row, dict) else row[0]
    out = client.get(f"/o2d/cashier/visible-to?user_key={key}", headers=h["admin"]).json()
    assert out["ok"] and out["canOpen"] is False and out["awaiting"] == []      # a shop login does not hold the Cashier page


def test_bulk_photo_is_saved_once_per_order(client, people, r2, db_conn):
    h, today = people
    sls = [add(client, h["shop"], today, _dc())["slNo"] for _ in range(2)]
    for sl in sls:
        _status(client, h, sl, "Godown to Dispatch")
    hdr = {**h["godown_dispatch"], "Content-Type": "image/jpeg"}
    r = client.post(f"/o2d/bulk/photos?kind=dispatch&sl_nos={sls[0]},{sls[1]}", content=b"img", headers=hdr).json()
    assert r["success"] is True and r["saved"] == 2
    assert len(r2.objects) == 2                                   # one stored object per order
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM order_attachments a JOIN fact_orders o ON o.order_key = a.order_key WHERE o.sl_no = ANY(%s)", (sls,))
        row = cur.fetchone()
    assert (row["n"] if isinstance(row, dict) else row[0]) == 2
    bad = client.post(f"/o2d/bulk/photos?kind=dispatch&sl_nos={sls[0]},99999999", content=b"img", headers=hdr).json()
    assert bad["success"] is False and bad["saved"] == 1 and bad["failed"][0]["slNo"] == 99999999
    assert client.post(f"/o2d/bulk/photos?kind=dispatch&sl_nos={sls[0]}", content=b"img", headers={**h["shop"], "Content-Type": "image/jpeg"}).status_code == 403


def test_bank_and_other_reports_build(client, people):
    h, _ = people
    for kind in ("received-bank", "received-other-all", "godown-received-cash", "shop-received-cash"):
        r = client.get(f"/o2d/reports/{kind}.pdf", headers=h["admin"])
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), kind
