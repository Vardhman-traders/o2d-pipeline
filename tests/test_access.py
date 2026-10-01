"""Page access (role matrix + per-person exceptions), access requests, and the order timeline."""
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

PW = "Pass-12345"


@pytest.fixture()
def client(migrated_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
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
    today = date.today()
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (int(today.strftime("%Y%m%d")), today, today.day, today.month, today.year,
                     today.strftime("%A"), today.weekday() == 0))
        for table, col, names in (("dim_order_channel", "channel_name", ("Call",)),
                                  ("dim_submission_type", "type_name", ("Challan",)),
                                  ("dim_delivery_status", "status_name", ("Shop", "Delivered", "Cancelled")),
                                  ("dim_payment_status", "status_name", ("Paid",))):
            for n in names:
                cur.execute(f"INSERT INTO {table} ({col}) VALUES (%s) ON CONFLICT DO NOTHING", (n,))
        cur.execute("INSERT INTO dim_person (full_name, person_role) VALUES ('Ravi','ready_by') ON CONFLICT DO NOTHING")
    db_conn.commit()


def login(client, db_conn, role):
    from app import auth
    username = f"ac_{role}_{uuid.uuid4().hex[:6]}"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s,%s,%s,%s,false) RETURNING user_key", (username, auth.hash_password(PW), role, username))
        key = cur.fetchone()[0]
    db_conn.commit()
    tok = client.post("/auth/login", json={"username": username, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, key


def pages(client, h):
    return client.get("/auth/me", headers=h).json()["pages"]


def test_role_defaults_keep_the_old_behaviour(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    shop, _ = login(client, db_conn, "shop")
    cashier, _ = login(client, db_conn, "cashier")
    assert pages(client, shop) == ["o2d_shop"]
    assert pages(client, cashier) == []
    assert "dashboard_overview" in pages(client, admin) and "o2d_receiving" in pages(client, admin)
    assert client.get("/o2d/shop", headers=shop).status_code == 200
    assert client.get("/o2d/receiving", headers=shop).status_code == 403          # not their screen
    assert client.get("/o2d/receiving", headers=admin).status_code == 200
    assert client.get("/admin/dashboard", headers=shop).status_code == 403
    r = client.get("/admin/orders/filter-options", headers=cashier)
    assert r.status_code == 403 and "Request access" in r.json()["detail"]
    assert client.get("/admin/access/matrix", headers=shop).status_code == 403    # Setup stays admin-only


def test_role_matrix_and_person_exceptions(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    c1, c1_key = login(client, db_conn, "cashier")
    c2, _ = login(client, db_conn, "cashier")
    m = client.get("/admin/access/matrix", headers=admin).json()
    assert "cashier" in m["roles"] and "admin" not in m["roles"] and len(m["pages"]) == 8

    assert client.put("/admin/access/matrix", headers=admin,
                      json={"page_key": "dashboard_orders", "role": "cashier", "allowed": True}).status_code == 200
    assert client.get("/admin/orders/filter-options", headers=c1).status_code == 200
    assert client.get("/admin/orders/filter-options", headers=c2).status_code == 200
    assert client.get("/admin/dashboard", headers=c1).status_code == 403          # only the page they were given

    # one person blocked, the colleague with the same role keeps it
    assert client.put(f"/admin/access/users/{c1_key}", headers=admin,
                      json={"page_key": "dashboard_orders", "allowed": False}).status_code == 200
    assert client.get("/admin/orders/filter-options", headers=c1).status_code == 403
    assert client.get("/admin/orders/filter-options", headers=c2).status_code == 200
    # and one person granted something the role does not have
    assert client.put(f"/admin/access/users/{c1_key}", headers=admin,
                      json={"page_key": "dashboard_overview", "allowed": True}).status_code == 200
    assert client.get("/admin/dashboard", headers=c1).status_code == 200
    detail = client.get(f"/admin/access/users/{c1_key}", headers=admin).json()
    by = {p["key"]: p for p in detail["pages"]}
    assert by["dashboard_orders"]["role_default"] is True and by["dashboard_orders"]["override"] is False
    assert by["dashboard_overview"]["override"] is True
    # back to the role default
    client.put(f"/admin/access/users/{c1_key}", headers=admin, json={"page_key": "dashboard_orders", "allowed": None})
    assert client.get("/admin/orders/filter-options", headers=c1).status_code == 200
    assert client.put("/admin/access/matrix", headers=admin,
                      json={"page_key": "nope", "role": "cashier", "allowed": True}).status_code == 422
    client.put("/admin/access/matrix", headers=admin,
               json={"page_key": "dashboard_orders", "role": "cashier", "allowed": False})


def test_access_request_flow(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    me, _ = login(client, db_conn, "accounts")
    colleague, _ = login(client, db_conn, "accounts")
    before = client.get("/admin/access/requests/count", headers=admin).json()["pending"]

    assert client.post("/access/request", headers=me,
                       json={"page_key": "dashboard_overview", "reason": "no"}).status_code == 422
    assert client.post("/access/request", headers=me, json={"page_key": "nope", "reason": "need it"}).status_code == 422
    r = client.post("/access/request", headers=me,
                    json={"page_key": "dashboard_overview", "reason": "Need daily numbers"})
    assert r.status_code == 201
    assert client.post("/access/request", headers=me,
                       json={"page_key": "dashboard_overview", "reason": "again please"}).status_code == 409
    assert client.get("/admin/access/requests/count", headers=admin).json()["pending"] == before + 1
    mine = client.get("/access/pages", headers=me).json()
    assert mine["requests"][0]["status"] == "pending" and mine["pages"] == []

    everything = client.get("/admin/access/requests", headers=admin).json()
    waiting = [x for x in everything if x["request_key"] == r.json()["request_key"]]
    assert waiting[0]["reason"] == "Need daily numbers" and waiting[0]["page_label"] == "Dashboard - Overview"
    assert client.post(f"/admin/access/requests/{waiting[0]['request_key']}/approve", headers=me).status_code == 403

    key = waiting[0]["request_key"]
    assert client.post(f"/admin/access/requests/{key}/approve", headers=admin, json={"note": "ok"}).status_code == 200
    assert client.post(f"/admin/access/requests/{key}/reject", headers=admin).status_code == 409   # already decided
    assert client.get("/admin/dashboard", headers=me).status_code == 200
    assert client.get("/admin/dashboard", headers=colleague).status_code == 403    # only that person got it
    assert client.post("/access/request", headers=me,
                       json={"page_key": "dashboard_overview", "reason": "already have"}).status_code == 409

    r2 = client.post("/access/request", headers=colleague,
                     json={"page_key": "o2d_receiving", "reason": "cover for a day"})
    assert client.post(f"/admin/access/requests/{r2.json()['request_key']}/reject", headers=admin,
                       json={"note": "ask again Monday"}).status_code == 200
    assert client.get("/o2d/receiving", headers=colleague).status_code == 403
    seen = client.get("/access/pages", headers=colleague).json()["requests"][0]
    assert seen["status"] == "rejected" and seen["decision_note"] == "ask again Monday"
    # a rejected request can be made again
    assert client.post("/access/request", headers=colleague,
                       json={"page_key": "o2d_receiving", "reason": "cover again"}).status_code == 201


def _make_order(client, db_conn):
    shop, _ = login(client, db_conn, "shop")
    r = client.post("/o2d/orders", headers=shop, json={
        "orderRcvdDate": date.today().isoformat(), "orderVia": "Call", "dcNo": "TL-" + uuid.uuid4().hex[:5],
        "typeOfSubmission": "Challan", "shippingLocation": "Rohini"})
    assert r.status_code == 200, r.text
    return shop, r.json()["slNo"]


def test_order_timeline_records_each_step(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    shop, sl = _make_order(client, db_conn)
    godown, _ = login(client, db_conn, "godown")
    assert client.put(f"/o2d/orders/{sl}/godown", headers=godown,
                      json={"readyByWhom": "Ravi", "deliveryStatus": "Delivered"}).json()["success"]
    # saving the same values again is not a new event
    client.put(f"/o2d/orders/{sl}/godown", headers=godown, json={"readyByWhom": "Ravi", "deliveryStatus": "Delivered"})

    d = client.get(f"/admin/orders/{sl}/detail", headers=admin).json()
    assert d["order"]["sl_no"] == sl and d["order"]["shipping_location"] == "Rohini" and d["order"]["archived"] is False
    events = d["timeline"]
    assert [e["type"] for e in events] == ["created", "godown"]
    assert events[0]["title"] == "Order logged" and events[0]["role"] == "shop" and events[0]["at"]
    assert events[1]["role"] == "godown" and events[1]["by"].startswith("ac_godown_")
    assert {c["field"]: (c["from"], c["to"]) for c in events[1]["changes"]} == {
        "Ready by": (None, "Ravi"), "Delivery status": (None, "Delivered")}
    assert not any(e["reconstructed"] for e in events) and d["timeline_note"] is None
    assert client.get("/admin/orders/999999/detail", headers=admin).status_code == 404
    assert client.get(f"/admin/orders/{sl}/detail", headers=shop).status_code == 403


def test_order_without_recorded_events_gets_a_reconstructed_timeline(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    shop, sl = _make_order(client, db_conn)
    with db_conn.cursor() as cur:   # an order from before event tracking: no events at all
        cur.execute("DELETE FROM order_event WHERE order_key = "
                    "(SELECT order_key FROM fact_orders WHERE sl_no = %s)", (sl,))
        cur.execute("UPDATE fact_orders SET material_delivery_datetime = now(), "
                    "delivery_status_key = (SELECT status_key FROM dim_delivery_status "
                    "WHERE status_name = 'Delivered') "
                    "WHERE sl_no = %s", (sl,))
    db_conn.commit()
    d = client.get(f"/admin/orders/{sl}/detail", headers=admin).json()
    types = [e["type"] for e in d["timeline"]]
    assert types[0] == "created" and "dispatch" in types and "godown" in types
    assert all(e["reconstructed"] for e in d["timeline"]) and "rebuilt" in d["timeline_note"]
