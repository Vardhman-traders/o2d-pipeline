"""Characterisation tests for the existing O2D API: they pin today's login and access behaviour so
later refactors (Stage 6, removing the Apps Script hooks) cannot silently change it."""
import uuid

import pytest
from fastapi.testclient import TestClient

JWT = "x" * 48


@pytest.fixture()
def client(migrated_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("JWT_SECRET", JWT)
    monkeypatch.delenv("APPS_SCRIPT_CLIENT_KEY", raising=False)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _make_user(db_conn, role="shop", must_change=True, password="Pass-12345"):
    from app import auth
    username = "t_" + uuid.uuid4().hex[:8]
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
            "VALUES (%s, %s, %s, %s, %s)",
            (username, auth.hash_password(password), role, "Test " + role, must_change))
    db_conn.commit()
    return username, password


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_orders_require_a_token(client):
    assert client.get("/orders").status_code == 401


def test_wrong_password_is_rejected(client, db_conn):
    username, _ = _make_user(db_conn)
    r = client.post("/auth/login", json={"username": username, "password": "nope"})
    assert r.status_code == 401


def test_login_returns_token_and_forces_password_change(client, db_conn):
    username, password = _make_user(db_conn)
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["must_change_password"] is True
    assert body["user"]["role"] == "shop"


def test_user_must_change_password_before_reading_orders(client, db_conn):
    username, password = _make_user(db_conn, must_change=True)
    token = client.post("/auth/login", json={"username": username, "password": password}).json()["access_token"]
    r = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_shop_user_can_list_orders_after_password_set(client, db_conn):
    username, password = _make_user(db_conn, must_change=False)
    token = client.post("/auth/login", json={"username": username, "password": password}).json()["access_token"]
    r = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_admin_can_list_orders(client, db_conn):
    # README says admin sees nothing, but the code lets admin in (ANY_ORDER_ROLE_OR_ADMIN); pin the real behaviour.
    username, password = _make_user(db_conn, role="admin", must_change=False)
    token = client.post("/auth/login", json={"username": username, "password": password}).json()["access_token"]
    r = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_cashier_can_open_list_but_sees_no_orders_by_default(client, db_conn):
    username, password = _make_user(db_conn, role="cashier", must_change=False)
    token = client.post("/auth/login", json={"username": username, "password": password}).json()["access_token"]
    r = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    rows = body if isinstance(body, list) else body.get("orders", body.get("items", []))
    assert rows == []


def test_unknown_role_is_forbidden(client, db_conn):
    username, password = _make_user(db_conn, role="legacy", must_change=False)
    token = client.post("/auth/login", json={"username": username, "password": password}).json()["access_token"]
    r = client.get("/orders", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
