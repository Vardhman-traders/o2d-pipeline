"""Browser test of the native O2D screens (app/static/sales): one order travels through every role's real page.
Needs Playwright + Chromium (`pip install playwright && playwright install chromium`); skipped when missing."""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import date, timedelta

import psycopg2
import pytest

from tests.conftest import ROOT

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.slow
PW = "Pass-12345"
ROLES = ("shop", "godown", "godown_dispatch", "shop_dispatch", "receiving")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(migrated_db_url):
    from app import auth
    conn = psycopg2.connect(migrated_db_url)
    cur = conn.cursor()
    d0 = date.today()
    for i in range(-15, 5):
        d = d0 + timedelta(days=i)
        cur.execute("INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (int(d.strftime("%Y%m%d")), d, d.day, d.month, d.year, d.strftime("%A"), d.weekday() == 0))
    for table, col, names in (("dim_order_channel", "channel_name", ("Call", "Walk-in", "Online")),
                              ("dim_submission_type", "type_name", ("Challan", "Invoice")),
                              ("dim_delivery_status", "status_name", ("Shop", "Delivered", "Cancelled")),
                              ("dim_payment_status", "status_name", ("Paid", "Pending"))):
        for n in names:
            cur.execute(f"INSERT INTO {table} ({col}) VALUES (%s) ON CONFLICT DO NOTHING", (n,))
    for n, r in (("Ravi", "ready_by"), ("Sonu", "colour_making"), ("Amit", "delivery")):
        cur.execute("INSERT INTO dim_person (full_name, person_role) VALUES (%s,%s) ON CONFLICT DO NOTHING", (n, r))
    for role in ROLES:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s,%s,%s,%s,false) ON CONFLICT DO NOTHING",
                    ("ui_" + role, auth.hash_password(PW), role, "UI " + role))
    conn.commit()
    conn.close()

    port = _free_port()
    env = {**os.environ, "DATABASE_URL": migrated_db_url, "JWT_SECRET": "k" * 48}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("app did not start")
    yield url + "/sales/"
    proc.terminate()
    proc.wait(timeout=10)


def test_one_order_through_every_role_screen(base_url):
    expect = sync_api.expect
    dc = "UI-" + uuid.uuid4().hex[:5]
    errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()

        def page_for(role, password=PW):
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            page.on("pageerror", lambda e: errors.append(f"{role}: {e}"))
            page.goto(base_url)
            page.fill("#loginUsername", "ui_" + role)
            page.fill("#loginPassword", password)
            page.click("#loginForm button[type=submit]")
            return page

        bad = page_for("shop", "wrong-password-1")
        expect(bad.locator("#loginError")).to_contain_text("Invalid")

        shop = page_for("shop")
        shop.wait_for_selector("#appScreen", state="visible")
        # other tests share this database and add their own channels, so wait for the ones we need, not an exact count
        expect(shop.locator("#shop_orderVia option", has_text=re.compile(r"^Call$"))).to_have_count(1, timeout=8000)
        shop.select_option("#shop_orderVia", label="Call")
        shop.select_option("#shop_typeOfSubmission", label="Challan")
        shop.fill("#shop_dcNo", dc)
        shop.fill("#shop_shippingLocation", "Rohini")
        shop.click("#shop_saveBtn")
        expect(shop.locator("#shop_statusMsg")).to_contain_text("Order saved", timeout=8000)
        expect(shop.locator("#shop_ordersBody")).to_contain_text(dc, timeout=8000)

        godown = page_for("godown")
        expect(godown.locator("#godown_pendingBody")).to_contain_text(dc, timeout=8000)
        godown.locator("#godown_pendingBody tr", has_text=dc).get_by_role("button").first.click()
        godown.select_option("#godown_readyByWhom", label="Ravi")
        godown.select_option("#godown_colourMakingBy", label="Sonu")
        godown.select_option("#godown_deliveryStatus", label="Delivered")
        godown.click("#godown_saveBtn")
        expect(godown.locator("#godown_completedBody")).to_contain_text(dc, timeout=8000)

        dispatch = page_for("godown_dispatch")
        expect(dispatch.locator("#dispatch_pendingBody")).to_contain_text(dc, timeout=8000)
        dispatch.locator("#dispatch_pendingBody tr", has_text=dc).get_by_role("button").first.click()
        dispatch.select_option("#dispatch_deliveredByWhom", label="Amit")
        dispatch.fill("#dispatch_cartage", "50")
        dispatch.click("#dispatch_saveBtn")
        expect(dispatch.locator("#dispatch_completedBody")).to_contain_text(dc, timeout=8000)

        receiving = page_for("receiving")
        expect(receiving.locator("#receiving_pendingBody")).to_contain_text(dc, timeout=8000)
        receiving.locator("#receiving_pendingBody tr", has_text=dc).get_by_role("button").first.click()
        receiving.evaluate("setDatePickerValue('receiving_dateOfReceiving', isoToday_())")
        receiving.select_option("#receiving_paymentStatus", label="Paid")
        receiving.fill("#receiving_amountReceived", "1500")
        receiving.click("#receiving_saveBtn")
        expect(receiving.locator("#receiving_completedBody")).to_contain_text(dc, timeout=8000)
        browser.close()

    assert not errors, errors
