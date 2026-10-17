# ruff: noqa: F811, E501
"""Order changes log, bulk "Mark done", kanban search, sales-return series and the Under Processing rule (migration 039 batch)."""
from datetime import date, timedelta

from app import doc_numbers as dn
from tests.test_o2d_screens import add, client, ensure_date, seed  # noqa: F401  (fixtures + helpers)
from tests.test_order_journey import _dc, _status, people  # noqa: F401


def _quiet_day(db_conn, back: int):
    """A day of its own, so small bill numbers used by one test never sit beside the big random ones the other tests use."""
    from app import o2d_screens
    d = o2d_screens.today_ist() - timedelta(days=back)
    ensure_date(db_conn, d)
    return d



def test_sales_return_series_use_the_challan_and_invoice_reset_rules():
    d = date(2026, 10, 2)
    assert dn.series_key("Sales Return Challan", d) == "2026-10-02"      # daily, like Challan
    assert dn.series_key("Sales Return Invoice", d) == "2026-27"         # financial year, like Invoice
    assert dn._window("Sales Return Challan", d) == (d, d)
    assert dn._window("Sales Return Invoice", d) == (date(2026, 4, 1), date(2027, 3, 31))
    assert dn.SERIES["sales_return_challan"] == "Sales Return Challan" and dn.slug("Sales Return Invoice") == "sales_return_invoice"
    assert dn.number_in("Sales Return Challan", "sales_return_challan", "12") == 12


def test_sales_return_series_can_be_tagged_and_numbers_stay_apart(client, people, db_conn):
    h, _ = people
    today = _quiet_day(db_conn, 30)
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


def test_bulk_dispatch_takes_each_orders_own_values_all_or_nothing(client, people, db_conn):
    h, today = people
    sls = []
    for _ in range(2):
        sl = add(client, h["shop"], today, _dc())["slNo"]
        _status(client, h, sl, "Godown to Dispatch")
        sls.append(sl)
    f1 = {"materialDeliveryDateTime": today.isoformat() + "T10:00", "deliveredByWhom": "Amit", "cartage": 40, "viewAs": "godown_dispatch"}
    f2 = {**f1, "materialDeliveryDateTime": today.isoformat() + "T11:30", "cartage": 75}
    refused = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"],
                         json={"items": [{"slNo": sls[0], "form": f1}, {"slNo": 99999999, "form": f2}]}).json()
    assert refused["success"] is False and "Sl 99999999" in refused["message"] and "nothing was saved" in refused["message"]
    first = next(o for o in client.get("/o2d/dispatch", headers=h["godown_dispatch"]).json()["pending"] if o["slNo"] == sls[0])
    assert not first["materialDeliveryDateTime"]                         # rolled back
    missing = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"],
                         json={"items": [{"slNo": sls[0], "form": f1}, {"slNo": sls[1], "form": {"viewAs": "godown_dispatch"}}]}).json()
    assert missing["success"] is False and f"Sl {sls[1]}" in missing["message"] and "missing required" in missing["message"]

    ok = client.put("/o2d/bulk/dispatch", headers=h["godown_dispatch"],
                    json={"items": [{"slNo": sls[0], "form": f1}, {"slNo": sls[1], "form": f2}]}).json()
    assert ok["success"] is True and ok["count"] == 2
    rows = {o["slNo"]: o for o in client.get("/o2d/dispatch", headers=h["godown_dispatch"]).json()["completed"]}
    assert rows[sls[0]]["cartage"] == 40 and rows[sls[1]]["cartage"] == 75      # per-order values, not one shared set
    assert rows[sls[0]]["materialDeliveryDateTime"].endswith("10:00") and rows[sls[1]]["materialDeliveryDateTime"].endswith("11:30")
    with db_conn.cursor() as cur:
        cur.execute("SELECT o.sl_no, count(*) AS n FROM order_event e JOIN fact_orders o ON o.order_key = e.order_key "
                    "WHERE o.sl_no = ANY(%s) AND e.event_type = 'dispatch' GROUP BY o.sl_no", (sls,))
        assert {r["sl_no"] if isinstance(r, dict) else r[0]: (r["n"] if isinstance(r, dict) else r[1]) for r in cur.fetchall()} == {sls[0]: 1, sls[1]: 1}
    assert client.put("/o2d/bulk/dispatch", headers=h["shop"], json={"items": [{"slNo": sls[0], "form": f1}]}).status_code == 403   # no dispatch page


def test_bulk_receiving_closes_the_selected_orders_each_with_its_own_amount(client, people):
    h, today = people
    sls = []
    for _ in range(2):
        sl = add(client, h["shop"], today, _dc())["slNo"]
        _status(client, h, sl, "Godown to Dispatch")
        client.put(f"/o2d/orders/{sl}/dispatch", headers=h["godown_dispatch"], json={"materialDeliveryDateTime": today.isoformat() + "T10:00", "deliveredByWhom": "Amit"})
        sls.append(sl)
    forms = [{"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": amt} for amt in (1000, 2500)]
    r = client.put("/o2d/bulk/receiving", headers=h["receiving"], json={"items": [{"slNo": sl, "form": f} for sl, f in zip(sls, forms)]}).json()
    assert r["success"] is True and r["count"] == 2
    done = {o["slNo"]: o["amountReceivedRaw"] for o in client.get("/o2d/receiving", headers=h["receiving"]).json()["completed"]}
    assert done[sls[0]] == 1000 and done[sls[1]] == 2500


def test_order_changes_log_is_admin_only_and_filters_by_dashboard(client, people):
    h, today = people
    dc = _dc()
    sl = add(client, h["shop"], today, dc)["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    assert client.get("/admin/order-events", headers=h["shop"]).status_code == 403
    allv = client.get(f"/admin/order-events?q={dc}", headers=h["admin"]).json()
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


def test_bank_and_other_reports_build(client, people):
    h, _ = people
    for kind in ("received-bank", "received-other-all", "godown-received-cash", "shop-received-cash"):
        r = client.get(f"/o2d/reports/{kind}.pdf", headers=h["admin"])
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), kind


def test_goods_return_type_maps_to_the_sales_return_series_by_the_pick(client, people, db_conn):
    h, _ = people
    today = _quiet_day(db_conn, 31)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_submission_type (type_name, bill_series) VALUES ('Goods Ret', 'goods_return') ON CONFLICT DO NOTHING")
    db_conn.commit()

    def save(dc, pick=None):
        body = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": dc, "typeOfSubmission": "Goods Ret"}
        if pick:
            body["billSeries"] = pick
        return client.post("/o2d/orders", headers=h["shop"], json=body).json()
    assert not save("61")["success"]                                       # the form must ask Challan or Invoice
    assert save("61", "challan")["success"]
    assert "Sales Return Challan 61 is already used" in save("61", "challan")["message"]
    assert save("61", "invoice")["success"]                                # Sales Return Invoice 61 is another series
    assert add(client, h["shop"], today, "61", typ="Challan")["success"]    # and a plain Challan 61 is another again
    chk = client.get("/o2d/doc-check", params={"type_name": "Goods Ret", "date": today.isoformat(), "number": "61", "bill": "invoice"}, headers=h["shop"]).json()
    assert chk["series"] == "Sales Return Invoice" and chk["duplicate"] is True
    assert "Goods Ret" in client.get("/o2d/doc-gaps", headers=h["shop"]).json()["seriesTypes"]["Sales Return Challan"]


def test_an_invoice_number_in_the_challan_field_is_refused_and_listed(client, people, db_conn):
    h, _ = people
    today = _quiet_day(db_conn, 32)
    for n in ("1", "2", "3"):
        assert add(client, h["shop"], today, n, typ="Challan")["success"]
    body = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": "984321", "typeOfSubmission": "Challan"}
    refused = client.post("/o2d/orders", headers=h["shop"], json=body).json()
    assert not refused["success"] and "looks like an Invoice number" in refused["message"]
    sl = client.post("/o2d/orders", headers=h["admin"], json=body).json()["slNo"]            # an admin may still enter an unusual number
    items = client.get("/admin/number-check", headers=h["admin"]).json()["items"]
    mine = [i for i in items if i["slNo"] == sl]
    assert mine and mine[0]["series"] == "Challan" and "Invoice" in mine[0]["reason"]
    assert client.get("/admin/number-check", headers=h["shop"]).status_code == 403
    fixed = client.put(f"/o2d/orders/{sl}/admin", headers=h["admin"], json={"dcNo": "4"}).json()          # the fix is an audited edit
    assert fixed["success"] is True
    assert not [i for i in client.get("/admin/number-check", headers=h["admin"]).json()["items"] if i["slNo"] == sl]
    events = client.get(f"/admin/order-events?q={sl}", headers=h["admin"]).json()["events"]
    assert any(e["slNo"] == sl and any(c["field"] == "DC / Inv no" and c["to"] == "4" for c in e["changes"]) for e in events)
