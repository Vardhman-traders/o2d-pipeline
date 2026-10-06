"""Apps Script and Google Sheets are gone: nothing in the app may depend on them, and the app's own pages
replace them (single sign-in, the /sales/ page, links to own pages)."""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ROOT

PW = "Pass-12345"


@pytest.fixture()
def client(migrated_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
    # A leftover client-key variable on Render must now be ignored, not enforced.
    monkeypatch.setenv("APPS_SCRIPT_CLIENT_KEY", "some-old-secret")
    from app import config
    from app.main import app
    config._view_cache = None
    with TestClient(app) as c:
        yield c


def login(client, db_conn, role):
    from app import auth
    username = f"r_{role}_{uuid.uuid4().hex[:6]}"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s, %s, %s, %s, false)", (username, auth.hash_password(PW), role, username))
    db_conn.commit()
    tok = client.post("/auth/login", json={"username": username, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_no_client_key_needed_and_old_variable_ignored(client, db_conn):
    shop = login(client, db_conn, "shop")
    assert client.get("/lookups/channels", headers=shop).status_code == 200
    assert client.get("/orders", headers=shop).status_code == 200


@pytest.mark.parametrize("method,path", [("post", "/auth/sso-ticket"), ("post", "/auth/sso-exchange"),
                                         ("get", "/admin/security"), ("post", "/admin/security/client-key/rotate")])
def test_apps_script_hooks_are_gone(client, db_conn, method, path):
    admin = login(client, db_conn, "admin")
    r = getattr(client, method)(path, headers=admin)
    assert r.status_code in (404, 405)


def test_sales_page_is_served_with_its_own_csp_and_others_stay_strict(client):
    sales = client.get("/sales/")
    assert sales.status_code == 200 and "Sales Portal" in sales.text
    assert "'unsafe-inline'" in sales.headers["content-security-policy"]
    assert "'unsafe-inline'" not in client.get("/").headers["content-security-policy"]
    assert client.get("/sales/api.js").status_code == 200
    assert client.get("/sales/autologin.js").status_code == 200


def test_sales_portal_link_is_seeded_for_operating_roles(client, db_conn):
    shop = login(client, db_conn, "shop")
    links = client.get("/links", headers=shop).json()
    assert "/sales/" in [link["url"] for link in links]
    assert "O2D Portal" in [link["name"] for link in links]              # migration 010 renamed the built-in link
    cashier = login(client, db_conn, "cashier")
    assert "/sales/" not in [link["url"] for link in client.get("/links", headers=cashier).json()]


def test_admin_can_add_links_to_own_pages_but_not_unsafe_ones(client, db_conn):
    admin = login(client, db_conn, "admin")
    ok = client.post("/admin/links", headers=admin, json={"name": "T", "url": "/sales/?x=1", "roles": ["shop"]})
    assert ok.status_code == 201
    for bad in ("//evil.example/x", "javascript:alert(1)", "http://insecure.example", "sales/"):
        r = client.post("/admin/links", headers=admin, json={"name": "T", "url": bad, "roles": []})
        assert r.status_code == 422, bad


def test_database_rejects_unsafe_link_urls(db_conn):
    import psycopg2
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO app_links (name, url) VALUES ('ok', '/sales/')")
    db_conn.rollback()
    for bad in ("//evil.example", "javascript:alert(1)"):
        with pytest.raises(psycopg2.errors.CheckViolation), db_conn.cursor() as cur:
            cur.execute("INSERT INTO app_links (name, url) VALUES ('bad', %s)", (bad,))
        db_conn.rollback()


def test_no_apps_script_or_google_sheets_left_in_the_app():
    banned = re.compile(r"script\.google\.com|google\.script\.run|require_client_key|sso[-_]ticket|SpreadsheetApp|"
                        r"UrlFetchApp|docs\.google\.com/spreadsheets|gspread", re.I)
    offenders = []
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in (".py", ".js", ".html", ".css") and path.is_file():
            text = path.read_text(encoding="utf-8")
            # the shim deliberately defines window.google.script.run for the carried-over HTML; that is not Apps Script
            text = text.replace("google.script.run", "") if path.name in ("api.js", "index.html") else text
            offenders += [f"{path.relative_to(ROOT)}: {m.group(0)}" for m in banned.finditer(text)]
    assert not offenders, offenders
