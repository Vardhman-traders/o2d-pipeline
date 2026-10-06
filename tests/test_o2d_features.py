"""O2D follow-ups: delivered-by detail + per-screen lists, admin "view as" dispatch lists, click-a-missing-number
entry, photo attachments (R2, with a fake store), and the PDF reports."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import attachments, config, o2d_reports, o2d_screens
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)


# ---------------------------------------------------------------- pure logic
@pytest.mark.parametrize("name,expected", [
    ("Porter", True), ("by company", True), ("Transport", True), ("Porter / By Company / Transport", True),
    ("Amit", False), ("", False), (None, False)])
def test_delivered_by_detail_trigger(name, expected):
    assert config.needs_delivered_by_detail(name, ["Porter", "By Company", "Transport"]) is expected


def test_pdf_has_title_created_by_and_approved_by():
    secs = [{"heading": h, "columns": c, "totals": t, "rows": [
        {"order_received_date": date(2026, 10, 1), "dc_inv_no": "12", "shipping_location": "Rohini", "delivered_by_full": "Porter - Ramesh",
         "material_delivery_datetime": datetime(2026, 10, 1, 8, tzinfo=timezone.utc), "payment_status": "Cash",
         "cartage": Decimal("50"), "date_of_receiving": date(2026, 10, 2), "amount_received": Decimal("1200.5")}]}
        for h, _, c, t in o2d_reports.REPORTS["receiving-received"]["sections"]]
    pdf = o2d_reports.build_pdf("Receiving report", secs, "Sahil", datetime.now(timezone.utc))
    assert pdf.startswith(b"%PDF")
    for kind in o2d_reports.REPORTS:  # empty reports still render
        empty = [{"heading": h, "columns": c, "totals": t, "rows": []} for h, _, c, t in o2d_reports.REPORTS[kind]["sections"]]
        assert o2d_reports.build_pdf("x", empty, "S", datetime.now(timezone.utc)).startswith(b"%PDF")


def _order_at_dispatch(client, db_conn, dc, status):
    """A new order the godown has marked with `status` ('Shop' reaches shop dispatch, 'Delivered' godown dispatch)."""
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    sl = add(client, login(client, db_conn, "shop"), today, dc)["slNo"]
    r = client.put(f"/o2d/orders/{sl}/godown", headers=login(client, db_conn, "godown"), json={"deliveryStatus": status}).json()
    assert r["success"] is True
    return sl


# ---------------------------------------------------------------- delivered by
def _delivery_person(db_conn, name, scope):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_person (full_name, person_role, dispatch_scope) VALUES (%s, 'delivery', %s) "
                    "ON CONFLICT (lower(btrim(full_name)), person_role) DO UPDATE SET dispatch_scope = EXCLUDED.dispatch_scope",
                    (name, scope))
    db_conn.commit()


def test_delivery_lists_differ_per_dispatch_screen_and_admin_view_as_gets_them(client, db_conn, seed):
    _delivery_person(db_conn, "Porter", "shop")
    _delivery_person(db_conn, "Godown Van", "godown")
    shop_d, godown_d, admin = (login(client, db_conn, r) for r in ("shop_dispatch", "godown_dispatch", "admin"))
    s = client.get("/o2d/dispatch", headers=shop_d).json()["dropdowns"]["deliveredByWhom"]
    g = client.get("/o2d/dispatch", headers=godown_d).json()["dropdowns"]["deliveredByWhom"]
    assert "Porter" in s and "Godown Van" not in s and "Amit" in s
    assert "Godown Van" in g and "Porter" not in g and "Amit" in g
    # admin used to get empty lists here (its own role has none); "View as" picks which screen's lists it sees
    a = client.get("/o2d/dispatch?view_as=shop_dispatch", headers=admin).json()
    assert "Porter" in a["dropdowns"]["deliveredByWhom"] and a["dropdowns"]["deliveryStatus"] and a["dropdowns"]["paymentStatus"]
    # nobody else can use view_as to see the other screen's list
    assert "Godown Van" not in client.get("/o2d/dispatch?view_as=godown_dispatch", headers=shop_d).json()["dropdowns"]["deliveredByWhom"]


def test_porter_needs_details_and_other_people_clear_them(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    _delivery_person(db_conn, "Porter", "both")
    sd = login(client, db_conn, "shop_dispatch")
    sl = _order_at_dispatch(client, db_conn, "90101", "Shop")
    base = {"materialDeliveryDateTime": f"{today.isoformat()}T10:00", "deliveredByWhom": "Porter"}
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json=base).json()
    assert r["success"] is False and "details" in r["message"]
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json={**base, "deliveredByDetail": "  Ramesh   MH12 "}).json()
    assert r["success"] is True
    order = next(o for o in client.get("/o2d/dispatch", headers=sd).json()["completed"] if o["dcNo"] == "90101")
    assert order["deliveredByWhom"] == "Porter" and order["deliveredByDetail"] == "Ramesh MH12"
    client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json={**base, "deliveredByWhom": "Amit", "deliveredByDetail": "ignored"})
    order = next(o for o in client.get("/o2d/dispatch", headers=sd).json()["completed"] if o["dcNo"] == "90101")
    assert order["deliveredByWhom"] == "Amit" and order["deliveredByDetail"] == ""


# ---------------------------------------------------------------- click a missing number
def test_missing_number_entry_is_a_page_the_admin_switches_on(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    gd = login(client, db_conn, "godown_dispatch")
    assert client.get("/o2d/doc-gaps", headers=gd).json()["canPunch"] is False
    form = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "typeOfSubmission": "Challan", "dcNo": "90105"}
    assert client.post("/o2d/missing-entry", json=form, headers=gd).status_code == 403
    assert client.get("/o2d/missing-entry/options", headers=gd).status_code == 403
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO page_access (page_key, role) VALUES ('o2d_missing_entry', 'godown_dispatch')")
    db_conn.commit()
    assert client.get("/o2d/doc-gaps", headers=gd).json()["canPunch"] is True
    assert "Call" in client.get("/o2d/missing-entry/options", headers=gd).json()["orderVia"]
    r = client.post("/o2d/missing-entry", json=form, headers=gd).json()
    assert r["success"] is True and r["slNo"]
    again = client.post("/o2d/missing-entry", json=form, headers=gd).json()
    assert again["success"] is False  # duplicate numbers are still refused


# ---------------------------------------------------------------- photos
class FakeR2:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"


@pytest.fixture()
def r2(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "x")
    fake = FakeR2()
    attachments.set_storage(fake)
    yield fake
    attachments.set_storage(None)


def test_photos_are_off_until_storage_is_configured(client, db_conn, seed, monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    sd = login(client, db_conn, "shop_dispatch")
    assert client.get("/o2d/dispatch", headers=sd).json()["photosEnabled"] is False
    r = client.post("/o2d/orders/1/photos?kind=dispatch", content=b"x", headers={**sd, "Content-Type": "image/jpeg"})
    assert r.status_code == 503


def test_photo_upload_and_listing_follow_order_visibility(client, db_conn, seed, r2):
    sl = _order_at_dispatch(client, db_conn, "90102", "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    jpeg = {"Content-Type": "image/jpeg"}
    assert client.get("/o2d/dispatch", headers=gd).json()["photosEnabled"] is True
    ok = client.post(f"/o2d/orders/{sl}/photos?kind=dispatch", content=b"\xff\xd8photo", headers={**gd, **jpeg})
    assert ok.status_code == 200 and ok.json()["success"] is True and len(r2.objects) == 1
    # godown dispatch has no business attaching a *receiving* photo
    assert client.post(f"/o2d/orders/{sl}/photos?kind=receiving", content=b"x", headers={**gd, **jpeg}).status_code == 403
    assert client.post(f"/o2d/orders/{sl}/photos?kind=dispatch", content=b"x", headers={**gd, "Content-Type": "text/html"}).status_code == 415
    listed = client.get(f"/o2d/orders/{sl}/photos", headers=gd).json()
    assert listed["enabled"] and [p["kind"] for p in listed["photos"]] == ["dispatch"] and listed["photos"][0]["url"].startswith("https://signed.example/")
    # a role that cannot see this order (receiving only sees delivered ones) gets a 404, not a link
    rec = login(client, db_conn, "receiving")
    assert client.get(f"/o2d/orders/{sl}/photos", headers=rec).status_code == 404


def test_photo_limit_per_order_and_kind(client, db_conn, seed, r2):
    sl = _order_at_dispatch(client, db_conn, "90103", "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    codes = [client.post(f"/o2d/orders/{sl}/photos?kind=dispatch", content=b"x", headers={**gd, "Content-Type": "image/png"}).status_code
             for _ in range(attachments.MAX_PER_KIND + 1)]
    assert codes == [200] * attachments.MAX_PER_KIND + [409]


# ---------------------------------------------------------------- reports
def test_reports_need_the_page_and_list_only_open_orders(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    shop, gd, admin = (login(client, db_conn, r) for r in ("shop", "godown_dispatch", "admin"))
    add(client, shop, today, "90104")
    assert client.get("/o2d/reports/godown-pending.pdf", headers=gd).status_code == 403
    r = client.get("/o2d/reports/godown-pending.pdf", headers=admin)
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]
    for kind in [k for k in o2d_reports.REPORTS if k != "godown-pending"]:
        extra = "&delivered_by=Amit" if kind == "cartage-detail" else ""   # the history report is for one delivery person
        assert client.get(f"/o2d/reports/{kind}.pdf?date_from={today.isoformat()}{extra}", headers=admin).status_code == 200
    assert client.get("/o2d/reports/nope.pdf", headers=admin).status_code == 404
    assert client.get(f"/o2d/reports/receiving-pending.pdf?date_from={today.isoformat()}&date_to=2000-01-01", headers=admin).status_code == 422


# ---------------------------------------------------------------- view only
def test_view_only_page_access_opens_the_screen_but_blocks_every_save(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    admin = login(client, db_conn, "admin")
    shop = login(client, db_conn, "shop")
    me = client.get("/auth/me", headers=shop).json()
    sl = add(client, shop, today, "90201")["slNo"]
    assert me["view_only_pages"] == []
    # admin sets this person's Shop screen to "View only"
    r = client.put(f"/admin/access/users/{me['user_key']}", headers=admin, json={"page_key": "o2d_shop", "allowed": True, "view_only": True})
    assert r.status_code == 200
    again = client.get("/auth/me", headers=shop).json()
    assert "o2d_shop" in again["pages"] and again["view_only_pages"] == ["o2d_shop"]
    assert client.get("/o2d/shop", headers=shop).status_code == 200            # can still open and read
    body = {"orderRcvdDate": today.isoformat(), "orderVia": "Call", "dcNo": "90202", "typeOfSubmission": "Challan"}
    assert client.post("/o2d/orders", json=body, headers=shop).status_code == 403
    assert client.put(f"/o2d/orders/{sl}/shop", json=body, headers=shop).status_code == 403
    assert client.get("/o2d/doc-gaps", headers=shop).json()["canPunch"] is False
    # the admin's per-person view shows it, and going back to "Always allow" restores saving
    rows = client.get(f"/admin/access/users/{me['user_key']}", headers=admin).json()["pages"]
    assert next(p for p in rows if p["key"] == "o2d_shop")["view_only"] is True
    client.put(f"/admin/access/users/{me['user_key']}", headers=admin, json={"page_key": "o2d_shop", "allowed": True})
    assert client.post("/o2d/orders", json=body, headers=shop).json()["success"] is True


# ---------------------------------------------------------------- cartage
def _delivered(client, db_conn, dc, person, cartage, received=True):
    """An order delivered today by `person` with `cartage` rupees (set through the godown + godown dispatch screens)."""
    today = o2d_screens.today_ist()
    sl = _order_at_dispatch(client, db_conn, dc, "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=gd, json={
        "materialDeliveryDateTime": f"{today.isoformat()}T11:00", "deliveredByWhom": person, "cartage": cartage}).json()
    assert r["success"] is True
    if received:   # the Archive portal's rule: cartage is accounted once receiving and payment are done
        rec = login(client, db_conn, "receiving")
        r = client.put(f"/o2d/orders/{sl}/receiving", headers=rec,
                       json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 500}).json()
        assert r["success"] is True
    return sl


def test_screens_come_from_one_list_and_cartage_follows_the_view_as_order(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    views = [v["view"] for v in client.get("/auth/me", headers=admin).json()["o2d_views"]]
    assert views.index("cartage") == views.index("godown_dispatch") + 1          # right after the dispatch screens
    shop = login(client, db_conn, "shop")
    assert [v["view"] for v in client.get("/auth/me", headers=shop).json()["o2d_views"]] == ["shop"]


def test_cartage_page_is_admin_configured_and_reports_money_per_person(client, db_conn, seed):
    _delivery_person(db_conn, "Carl", "both")
    _delivered(client, db_conn, "90301", "Carl", 100)
    _delivered(client, db_conn, "90302", "Carl", 150)
    _delivered(client, db_conn, "90303", "Carl", 0)
    _delivered(client, db_conn, "90304", "Carl", 999, received=False)   # not received yet: not part of cartage history
    cart = login(client, db_conn, "cartage")
    admin = login(client, db_conn, "admin")
    assert client.get("/o2d/cartage", headers=cart).status_code == 403          # nothing is built in for a role
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO page_access (page_key, role) VALUES ('o2d_cartage', 'cartage')")
    db_conn.commit()
    d = client.get("/o2d/cartage", headers=cart).json()
    assert d["canEdit"] is True and d["summary"]["total"] >= 250 and d["summary"]["deliveries"] >= 3
    carl = next(e for e in d["byEmployee"] if e["employee"] == "Carl")
    assert carl["deliveries"] == 3 and carl["withCartage"] == 2 and carl["total"] == 250 and carl["average"] == 125
    sl = next(o["slNo"] for o in d["orders"] if o["deliveredByName"] == "Carl")
    assert client.put(f"/o2d/cartage/{sl}", headers=cart, json={"cartage": 80}).json()["success"] is True
    # one person set to view-only on it: can read, cannot change
    me = client.get("/auth/me", headers=cart).json()
    client.put(f"/admin/access/users/{me['user_key']}", headers=admin, json={"page_key": "o2d_cartage", "allowed": True, "view_only": True})
    assert client.get("/o2d/cartage", headers=cart).json()["canEdit"] is False
    assert client.put(f"/o2d/cartage/{sl}", headers=cart, json={"cartage": 1}).status_code == 403
    assert client.get("/o2d/reports/cartage-detail.pdf", headers=cart).status_code == 422   # needs one delivery person
    for url in ("cartage-detail.pdf?delivered_by=Carl", "cartage-by-employee.pdf"):
        r = client.get(f"/o2d/reports/{url}", headers=cart)
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert client.get("/o2d/reports/godown-pending.pdf", headers=cart).status_code == 403   # other reports need the Reports page
