"""Browser tests: the no-access page and request flow, the order-details popup with its timeline, and one ribbon height
on every screen. Needs Playwright + Chromium; skipped when missing."""
import re
import uuid
from datetime import date

import psycopg2
import pytest

from tests.test_admin_ui import PW, base_url  # noqa: F401  (the app-server fixture, shared)

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.slow


def _sign_in(page, url, username):
    page.goto(url)
    page.fill("input[autocomplete=username]", username)
    page.fill("input[autocomplete=current-password]", PW)
    page.click("button[type=submit]")


def test_no_access_request_approval_and_order_timeline(base_url, migrated_db_url):  # noqa: F811
    from app import auth
    expect = sync_api.expect
    u = uuid.uuid4().hex[:5]
    cash = f"ui_cash_{u}"
    conn = psycopg2.connect(migrated_db_url)
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s, %s, 'cashier', %s, false)", (cash, auth.hash_password(PW), f"Cash {u}"))
    conn.close()
    errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        person = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        boss = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        for pg in (person, boss):
            pg.on("pageerror", lambda e: errors.append(str(e)))

        # ---- no access: a message and a way to ask, never a blank page
        _sign_in(person, base_url, cash)
        expect(person.locator("#noAccess")).to_contain_text("Access not provided", timeout=8000)
        person.click("#requestAccessBtn")
        person.locator("dialog select").select_option("dashboard_overview")
        person.locator("dialog input[type=text]").fill("Need the daily numbers")
        person.locator("dialog button[type=submit]").click()
        expect(person.locator("#myRequests")).to_contain_text("pending", timeout=8000)

        # ---- the admin sees it waiting, with the reason, and approves
        _sign_in(boss, base_url, "ui_admin")
        expect(boss.locator("#tileSetup")).to_contain_text("access request", timeout=8000)
        boss.click("#tileSetup")
        boss.locator(".module-tabs .subtab", has_text=re.compile(r"^Access requests")).click()
        row = boss.locator("#requestsTable tbody tr", has_text=cash)
        expect(row).to_contain_text("Need the daily numbers", timeout=8000)
        expect(row).to_contain_text("Dashboard - Overview")
        row.get_by_role("button", name="Approve").click()
        boss.locator("dialog input[type=text]").fill("ok")
        boss.locator("dialog button[type=submit]").click()
        expect(boss.locator("#requestsTable tbody tr", has_text=cash)).to_have_count(0, timeout=8000)

        # ---- the person gets in, and only to the page they were given
        person.get_by_role("button", name="Check again").click()
        expect(person.locator("#tileDashboard")).to_be_visible(timeout=8000)
        expect(person.locator("#tileSetup")).to_have_count(0)
        person.click("#tileDashboard")
        expect(person.locator(".module-tabs .subtab")).to_have_text(["Overview"])
        expect(person.locator("#archiveNote")).to_be_visible(timeout=8000)

        # ---- order details popup with the timeline (admin)
        login = boss.request.post(base_url + "auth/login", data={"username": "ui_admin", "password": PW})
        token = login.json()["access_token"]
        dc = f"TLUI-{u}"
        made = boss.request.post(base_url + "o2d/orders", headers={"Authorization": "Bearer " + token}, data={
            "orderRcvdDate": date.today().isoformat(), "orderVia": "Call", "dcNo": dc, "typeOfSubmission": "Challan"})
        assert made.json()["success"], made.text()
        boss.click("#homeBtn")
        boss.click("#tileDashboard")
        boss.locator(".module-tabs .subtab", has_text="All orders").click()
        boss.fill("#f_sl", dc)
        row = boss.locator("#ordersTable tbody tr", has_text=dc)
        expect(row).to_have_count(1, timeout=8000)
        row.click()
        modal = boss.locator("dialog.order-modal")
        expect(modal.locator("#orderTimeline")).to_contain_text("Order logged", timeout=8000)
        expect(modal.locator("#orderTimeline")).to_contain_text("By UI Admin")
        expect(modal.locator("#orderTimeline")).to_contain_text("IST")
        expect(modal).to_contain_text(dc)
        modal.get_by_role("button", name="Close").click()

        # ---- edit then delete: a plain-words summary appears at the top of the page
        row.get_by_role("button", name="Edit").click()
        boss.locator("dialog .field", has_text="Remarks").locator("input").fill("checked by test")
        boss.locator("dialog button[type=submit]").click()
        expect(boss.locator("#actionSummary")).to_contain_text("was updated", timeout=8000)
        expect(boss.locator("#actionSummary")).to_contain_text("Remarks: empty → checked by test")
        boss.locator("#ordersTable tbody tr", has_text=dc).get_by_role("button", name="Delete").click()
        boss.locator("dialog").get_by_role("button", name="Delete").click()
        expect(boss.locator("#actionSummary")).to_contain_text("was deleted", timeout=8000)
        expect(boss.locator("#ordersTable tbody tr")).to_have_count(0, timeout=8000)

        # ---- one ribbon height on the portal and on the O2D page, and the Back button never wraps
        boss.click("#homeBtn")
        boss.wait_for_selector("#tileO2d", state="visible", timeout=8000)
        portal_h = boss.locator(".topbar").bounding_box()["height"]
        boss.click("#tileO2d")
        boss.wait_for_selector("#backToPortalBtn", state="visible", timeout=8000)
        o2d_h = boss.locator("#appScreen .topbar").bounding_box()["height"]
        assert abs(portal_h - o2d_h) <= 1, (portal_h, o2d_h)
        assert boss.locator("#backToPortalBtn").bounding_box()["height"] < 44
        browser.close()
    assert not errors, errors
