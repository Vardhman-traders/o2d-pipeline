"""O2D screens that replace Apps Script (app/o2d_screens.py): same behaviour Code.gs had, now in Python."""
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import o2d_screens

PW = "Pass-12345"


@pytest.fixture()
def client(migrated_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
    monkeypatch.delenv("APPS_SCRIPT_CLIENT_KEY", raising=False)
    monkeypatch.delenv("WHATSAPP_API_USERNAME", raising=False)
    monkeypatch.delenv("WHATSAPP_API_PASSWORD", raising=False)
    from app import config
    from app.main import app
    config._view_cache = None
    config._perm_cache = None
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seed(db_conn):
    """Dropdown values, people and dates the screens need."""
    with db_conn.cursor() as cur:
        for name in ("Call", "Walk-in"):
            cur.execute("INSERT INTO dim_order_channel (channel_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        for name in ("Challan", "Invoice"):
            cur.execute("INSERT INTO dim_submission_type (type_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        for name in ("Shop", "Delivered", "Cancelled"):
            cur.execute("INSERT INTO dim_delivery_status (status_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        for name in ("Paid", "Pending"):
            cur.execute("INSERT INTO dim_payment_status (status_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        for name, role in (("Ravi", "ready_by"), ("Sonu", "colour_making"), ("Amit", "delivery")):
            cur.execute("INSERT INTO dim_person (full_name, person_role) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (name, role))
    db_conn.commit()


def ensure_date(db_conn, d: date):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (int(d.strftime("%Y%m%d")), d, d.day, d.month, d.year, d.strftime("%A"), d.weekday() == 0))
    db_conn.commit()


def login(client, db_conn, role, must_change=False):
    from app import auth
    username = f"t_{role}_{uuid.uuid4().hex[:6]}"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s, %s, %s, %s, %s)", (username, auth.hash_password(PW), role, username, must_change))
    db_conn.commit()
    tok = client.post("/auth/login", json={"username": username, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def add(client, h, day, dc, via="Call", typ="Challan", **extra):
    body = {"orderRcvdDate": day.isoformat(), "orderVia": via, "dcNo": dc, "typeOfSubmission": typ, **extra}
    r = client.post("/o2d/orders", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- pure logic
@pytest.mark.parametrize("today,expected_prev", [
    (date(2026, 9, 30), "2026-09-29"),   # Wednesday -> Tuesday
    (date(2026, 9, 29), "2026-09-27"),   # Tuesday -> skips Monday -> Sunday
    (date(2026, 9, 28), "2026-09-27"),   # Monday -> Sunday
])
def test_dashboard_window_skips_monday(today, expected_prev):
    # off_days passed explicitly: the default (DB-backed, admin-configurable) path is covered
    # by the admin API test instead, so this stays a pure unit test with no DB dependency.
    assert o2d_screens.dashboard_window(today, off_days={0}) == [expected_prev, today.isoformat()]


def test_gap_helpers():
    assert o2d_screens._gaps([1, 2, 4, 7]) == {"min": 1, "max": 7, "missing": [3, 5, 6]}
    assert o2d_screens._gaps([]) == {"min": None, "max": None, "missing": []}
    # invoices: only gaps of 2..10 between neighbours count; a jump of 15 is a new book, not "missing"
    assert o2d_screens._invoice_gaps([5001, 5002, 5005, 5020])["missing"] == [5003, 5004]


def test_whatsapp_message_and_skip_without_credentials(monkeypatch):
    order = {"orderRcvdDate": "2026-09-30", "dcNo": "123", "readyByWhom": "Ravi", "deliveryStatus": "Delivered",
             "shippingLocation": "Rohini", "detailedRemarks": "", "deliveredByWhom": "Amit"}
    msg = o2d_screens.build_dispatch_message(order)
    assert "Order Date: 30/09/2026" in msg and "DC No: 123" in msg and "Remarks: -" in msg
    monkeypatch.delenv("WHATSAPP_API_USERNAME", raising=False)
    assert o2d_screens.send_dispatch_alert(order)["skipped"] is True
    monkeypatch.setenv("WHATSAPP_API_USERNAME", "u")
    monkeypatch.setenv("WHATSAPP_API_PASSWORD", "p")
    monkeypatch.delenv("WHATSAPP_GROUP_ID", raising=False)
    assert "group" in o2d_screens.send_dispatch_alert(order, post=None)["reason"]   # no built-in group: nothing is sent
    monkeypatch.setenv("WHATSAPP_GROUP_ID", "12345@g.us")
    seen = {}
    out = o2d_screens.send_dispatch_alert(order, post=lambda url, body, headers: seen.update(h=headers) or (200, "ok"))
    assert out["code"] == 200 and seen["h"]["Authorization"] == "Basic dTpw"


# ---------------------------------------------------------------- the order lifecycle through the new routes
def test_order_lifecycle_across_role_screens(client, db_conn, seed, monkeypatch):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    alerts = []
    monkeypatch.setattr(o2d_screens, "send_dispatch_alert", lambda o: alerts.append(o))
    shop = login(client, db_conn, "shop")
    dc = "LC-" + uuid.uuid4().hex[:6]

    res = add(client, shop, today, dc, shippingLocation="Rohini")
    assert res["success"] is True and res["slNo"]
    sl = res["slNo"]

    shop_screen = client.get("/o2d/shop", headers=shop).json()
    assert "Call" in shop_screen["dropdowns"]["orderVia"]
    assert dc in [o["dcNo"] for o in shop_screen["recentOrders"]]

    godown = login(client, db_conn, "godown")
    g = client.get("/o2d/godown", headers=godown).json()
    assert dc in [o["dcNo"] for o in g["pending"]]
    r = client.put(f"/o2d/orders/{sl}/godown", headers=godown,
                   json={"readyByWhom": "Ravi", "colourMakingBy": "Sonu", "deliveryStatus": "Delivered"}).json()
    assert r["success"] is True
    g = client.get("/o2d/godown", headers=godown).json()
    assert dc in [o["dcNo"] for o in g["completed"]]

    gd = login(client, db_conn, "godown_dispatch")
    d = client.get("/o2d/dispatch", headers=gd).json()
    assert dc in [o["dcNo"] for o in d["pending"]]
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=gd,
                   json={"materialDeliveryDateTime": f"{today.isoformat()}T11:30", "deliveredByWhom": "Amit"}).json()
    assert r["success"] is True
    assert len(alerts) == 1 and alerts[0]["dcNo"] == dc, "first dispatch fill sends exactly one WhatsApp alert"
    client.put(f"/o2d/orders/{sl}/dispatch", headers=gd, json={"cartage": 50})
    assert len(alerts) == 1, "editing again must not re-send the alert"
    d = client.get("/o2d/dispatch", headers=gd).json()
    stuck = [o for o in d["completed"] if o["dcNo"] == dc]
    assert stuck and stuck[0]["stuckReason"] == "Receiving pending"

    rec = login(client, db_conn, "receiving")
    p = client.get("/o2d/receiving", headers=rec).json()
    assert dc in [o["dcNo"] for o in p["pending"]]
    r = client.put(f"/o2d/orders/{sl}/receiving", headers=rec,
                   json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 1500}).json()
    assert r["success"] is True
    p = client.get("/o2d/receiving", headers=rec).json()
    done = [o for o in p["completed"] if o["dcNo"] == dc]
    assert done and done[0]["amountReceived"] == 1500 and done[0]["paymentStatus"] == "Paid"


def test_alert_failure_never_fails_the_save(client, db_conn, seed, monkeypatch):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    monkeypatch.setattr(o2d_screens, "send_dispatch_alert", lambda o: (_ for _ in ()).throw(RuntimeError("down")))
    shop, godown, gd = (login(client, db_conn, r) for r in ("shop", "godown", "godown_dispatch"))
    sl = add(client, shop, today, "AL-" + uuid.uuid4().hex[:6])["slNo"]
    client.put(f"/o2d/orders/{sl}/godown", headers=godown, json={"deliveryStatus": "Delivered"})
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=gd,
                   json={"materialDeliveryDateTime": f"{today.isoformat()}T10:00", "deliveredByWhom": "Amit"}).json()
    assert r["success"] is True


def test_validation_messages(client, db_conn, seed):
    shop = login(client, db_conn, "shop")
    r = client.post("/o2d/orders", json={"orderVia": "Call"}, headers=shop).json()
    assert r["success"] is False and "Order Received Date" in r["message"] and "DC/Inv No." in r["message"]
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    r = client.post("/o2d/orders", headers=shop, json={
        "orderRcvdDate": today.isoformat(), "orderVia": "Nonexistent", "dcNo": "X1",
        "typeOfSubmission": "Challan"}).json()
    assert r["success"] is False and "Unknown dropdown" in r["message"]


def test_role_field_permissions_still_enforced(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    shop = login(client, db_conn, "shop")
    sl = add(client, shop, today, "PM-" + uuid.uuid4().hex[:6])["slNo"]
    receiving = login(client, db_conn, "receiving")
    # receiving cannot see a fresh (not yet delivered) order, so the write is refused, not applied
    r = client.put(f"/o2d/orders/{sl}/receiving", headers=receiving, json={"paymentStatus": "Paid"}).json()
    assert r["success"] is False


# ---------------------------------------------------------------- admin
def test_admin_only_routes(client, db_conn, seed):
    shop = login(client, db_conn, "shop")
    assert client.get("/o2d/admin/orders", headers=shop).status_code == 403
    assert client.get("/o2d/admin/form-options", headers=shop).status_code == 403
    assert client.get("/o2d/missing-numbers?date=2026-01-15", headers=shop).status_code == 403


def test_admin_sees_and_edits_any_order(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    shop = login(client, db_conn, "shop")
    admin = login(client, db_conn, "admin")
    dc = "AD-" + uuid.uuid4().hex[:6]
    sl = add(client, shop, today, dc, shippingLocation="Old")["slNo"]
    listing = client.get("/o2d/admin/orders?limit=200", headers=admin).json()["orders"]
    assert dc in [o["dcNo"] for o in listing]
    opts = client.get("/o2d/admin/form-options", headers=admin).json()["dropdowns"]
    assert "Amit" in opts["deliveredByWhom"] and "Paid" in opts["paymentStatus"]
    r = client.put(f"/o2d/orders/{sl}/admin", headers=admin,
                   json={"shippingLocation": "", "paymentStatus": "Pending", "cartage": 75}).json()
    assert r["success"] is True
    row = next(o for o in client.get("/o2d/admin/orders?limit=200", headers=admin).json()["orders"] if o["dcNo"] == dc)
    assert row["shippingLocation"] == "" and row["paymentStatus"] == "Pending" and row["cartage"] == 75


def test_missing_numbers_challans_and_invoices(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    d1, d2 = date(2026, 1, 15), date(2026, 1, 16)
    for d in (d1, d2):
        ensure_date(db_conn, d)
    for dc in ("101", "102", "104"):
        add(client, admin, d1, dc)
    for dc in ("5001", "5002", "5005", "5030"):
        add(client, admin, d2, dc, typ="Invoice")
    a = client.get("/o2d/missing-numbers?date=2026-01-15", headers=admin).json()
    assert a["challanByDate"][0]["challan"]["missing"] == [103]
    b = client.get("/o2d/missing-numbers?date=2026-01-16", headers=admin).json()
    assert b["invoiceByDate"][0]["invoice"]["missing"] == [5003, 5004]


# ---------------------------------------------------------------- item 1: admin sees orders; cashier has a toggle
def test_admin_sees_orders_and_cashier_view_is_a_toggle(client, db_conn, seed):
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    shop, admin, cashier = (login(client, db_conn, r) for r in ("shop", "admin", "cashier"))
    dc = "CT-" + uuid.uuid4().hex[:6]
    add(client, shop, today, dc)

    def seen_by(h):
        return dc in [o["dcNo"] for o in client.get(f"/o2d/search?dc_no={dc}", headers=h).json()["results"]]

    assert seen_by(admin) is True
    assert seen_by(cashier) is False, "off by default"

    def toggle(on):
        r = client.put("/admin/view-access", headers=admin, json={"role": "cashier", "can_view": on})
        assert r.status_code == 200

    toggle(True)
    assert seen_by(cashier) is True, "toggle on: cashier can view"
    edit = client.put("/o2d/orders/1/receiving", headers=cashier, json={"paymentStatus": "Paid"}).json()
    assert edit["success"] is False, "the toggle grants viewing only, never editing"
    toggle(False)
    assert seen_by(cashier) is False, "toggle off: back to nothing"
