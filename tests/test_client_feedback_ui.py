# ruff: noqa: F811, E501
"""The client's feedback round in a real browser: dates start as today, the Shop Dispatch & Receiving box is "Received", the Overview's
Under-processing card filters the board, and the Shop box keeps tracking an order as it moves on. Needs Playwright + Chromium."""
import json
import urllib.request
import uuid
from datetime import date

import psycopg2
import pytest

from tests.test_sales_ui import PW, base_url, sync_api  # noqa: F401  (fixture + helpers)

pytestmark = pytest.mark.slow


def _call(base, method, path, token=None, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def _token(base, username):
    return _call(base, "POST", "/auth/login", body={"username": username, "password": PW})["access_token"]


def _admin(migrated_db_url):
    from app import auth
    conn = psycopg2.connect(migrated_db_url)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES ('ui_admin_cf', %s, 'admin', 'UI admin', false) ON CONFLICT DO NOTHING", (auth.hash_password(PW),))
        cur.execute("INSERT INTO dim_delivery_status (status_name) VALUES ('Processing'), ('Sent to the Company') ON CONFLICT DO NOTHING")
        cur.execute("UPDATE dim_delivery_status SET dispatch_route = 'none' WHERE status_name = 'Processing'")
    conn.commit()
    conn.close()


def _order_to_receiving(base, status="Sent to the Company"):
    dc = "4" + str(uuid.uuid4().int)[:8]
    today = date.today().isoformat()
    shop = _token(base, "ui_shop")
    sl = _call(base, "POST", "/o2d/orders", shop, {"orderRcvdDate": today, "orderVia": "Call", "dcNo": dc, "typeOfSubmission": "Challan"})["slNo"]
    _call(base, "PUT", f"/o2d/orders/{sl}/godown", _token(base, "ui_godown"), {"deliveryStatus": status})
    return dc, sl


def _sign_in(browser, base, username):
    page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    page.goto(base)
    page.fill(".login-screen input[type=text]", username)
    page.fill(".login-screen input[type=password]", PW)
    page.click(".login-screen button[type=submit]")
    page.click("text=O2D Portal")
    try:
        page.wait_for_selector("#appScreen", state="visible", timeout=15000)
    except Exception:
        print("PAGE AFTER SIGN-IN", username, "->", page.url, "|", " / ".join(page.inner_text("body")[:400].splitlines()))
        raise
    return page


def test_godown_receiving_date_starts_as_today_and_shop_dispatch_second_box_is_received(base_url, migrated_db_url):
    expect = sync_api.expect
    _admin(migrated_db_url)
    dc, sl = _order_to_receiving(base_url)
    _call(base_url, "PUT", f"/o2d/orders/{sl}/dispatch", _token(base_url, "ui_godown_dispatch"),
          {"materialDeliveryDateTime": date.today().isoformat() + "T10:00", "deliveredByWhom": "Amit"})
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        rec = _sign_in(browser, base_url, "ui_receiving")
        expect(rec.locator("#receiving_pendingBody")).to_contain_text(dc, timeout=30000)
        rec.locator("#receiving_pendingBody tr", has_text=dc).get_by_role("button").first.click()
        assert rec.input_value("#receiving_dateOfReceiving_iso") == rec.evaluate("isoToday_()")       # point 1: starts as today
        godown_dispatch = _sign_in(browser, base_url, "ui_godown_dispatch")
        expect(godown_dispatch.locator("#dispatch_completedTitle")).to_have_text("Awaiting Receiving")
        shop_dispatch = _sign_in(browser, base_url, "ui_shop_dispatch")
        expect(shop_dispatch.locator("#dispatch_completedTitle")).to_have_text("Received")             # point 8
        browser.close()


def test_overview_under_processing_card_filters_the_board_and_the_shop_box_keeps_tracking(base_url, migrated_db_url):
    expect = sync_api.expect
    _admin(migrated_db_url)
    dc, _ = _order_to_receiving(base_url, status="Processing")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        shop = _sign_in(browser, base_url, "ui_shop")
        expect(shop.locator("#shop_ordersBody")).to_contain_text(dc, timeout=30000)                    # Logic 1: still on the Shop box,
        expect(shop.locator("#shop_ordersBody tr", has_text=dc)).to_contain_text("Processing")         # with its status updated
        admin = _sign_in(browser, base_url, "ui_admin_cf")
        problems = []
        admin.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        admin.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        card = admin.locator("#ov_underProcessing")
        try:
            expect(card).to_be_visible(timeout=30000)
        except AssertionError:
            print("OVERVIEW DEBUG", problems, "| board:", admin.inner_text("#overview_board")[:300], "| summary:", admin.inner_text("#overview_summary")[:300])
            raise
        assert int(card.locator(".n").inner_text()) >= 1
        card.click()                                                                                   # point 6: the card filters the board
        expect(admin.locator(".ov-col")).to_have_count(1, timeout=30000)
        expect(admin.locator(".ov-col .ov-col-head strong")).to_have_text("Under processing (godown)")
        expect(admin.locator(".ov-col")).to_contain_text(dc)
        browser.close()


def test_purchase_dates_start_as_today(base_url, migrated_db_url):
    _admin(migrated_db_url)
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.goto(base_url)
        page.fill(".login-screen input[type=text]", "ui_admin_cf")
        page.fill(".login-screen input[type=password]", PW)
        page.click(".login-screen button[type=submit]")
        page.wait_for_selector("text=Purchase", timeout=30000)
        page.goto(base_url + "/purchase/")
        page.wait_for_function("typeof resetAddForm === 'function'", timeout=30000)
        page.evaluate("resetAddForm()")
        today = page.evaluate("new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })")
        assert page.input_value("#materialReceivedDate") == today and page.input_value("#invoiceDate") == today
        browser.close()
