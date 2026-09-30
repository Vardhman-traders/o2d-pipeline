"""Browser test of the admin portal: filters + Excel export, bulk upload of orders (with an inline fix and an undo)
and bulk member creation. Needs Playwright + Chromium; skipped when missing."""
import os
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
HEAD = ("Order Date,Order Via,Type of Submission,DC/Inv No,Address,Remarks,Ready By,Colour Making By,Delivery Status,"
        "Delivered By,Delivery Date & Time,Cartage,Date of Receiving,Payment Status,Amount Received")


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
    for i in range(-30, 5):
        d = d0 + timedelta(days=i)
        cur.execute("INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (int(d.strftime("%Y%m%d")), d, d.day, d.month, d.year, d.strftime("%A"), d.weekday() == 0))
    for table, col, names in (("dim_order_channel", "channel_name", ("Call", "Walk-in")),
                              ("dim_submission_type", "type_name", ("Challan", "Invoice"))):
        for n in names:
            cur.execute(f"INSERT INTO {table} ({col}) VALUES (%s) ON CONFLICT DO NOTHING", (n,))
    cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                "VALUES ('ui_admin', %s, 'admin', 'UI Admin', false) ON CONFLICT DO NOTHING", (auth.hash_password(PW),))
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
    yield url + "/"
    proc.terminate()
    proc.wait(timeout=10)


def test_admin_filters_bulk_upload_and_undo(base_url, tmp_path):
    expect = sync_api.expect
    u = uuid.uuid4().hex[:5].upper()
    def d(n):
        return (date.today() + timedelta(days=n)).strftime("%d-%m-%Y")
    csv = tmp_path / "orders.csv"
    csv.write_text(HEAD + "\n"
                   f"{d(-5)},Call,Challan,UI-{u}-1,Rohini,,,,,,,,,,\n"
                   f"{d(-5)},Phone,Invoice,UI-{u}-2,Pitampura,,,,,,,,,,\n", encoding="utf-8")
    members = tmp_path / "team.csv"
    members.write_text(f"Display Name,Username,Role\nUI Person,ui.person.{u.lower()},receiving\n", encoding="utf-8")
    errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "CSP" in m.text else None)
        page.goto(base_url)
        page.fill("input[autocomplete=username]", "ui_admin")
        page.fill("input[autocomplete=current-password]", PW)
        page.click("button[type=submit]")

        # ---- bulk upload: the "Phone" channel row is rejected, fixed in place, re-checked and imported
        page.locator(".tabs > .tab", has_text="Import").click()
        page.set_input_files("#importFile", str(csv))
        expect(page.locator("#errorTable")).to_contain_text("'Phone' not found", timeout=8000)
        page.locator("#errorTable input[aria-label^='Order Via']").fill("Walk-in")
        page.click("#recheckBtn")
        expect(page.locator("#importBtn")).to_have_text("Import 2 orders", timeout=8000)
        page.click("#importBtn")
        expect(page.locator("#importStatus")).to_contain_text("Imported 2 orders", timeout=8000)
        expect(page.locator("#batchTable")).to_contain_text("orders.csv", timeout=8000)

        # ---- orders tab: filter by text, save the filter, reload it, export
        page.locator(".tabs > .tab", has_text="Orders").click()
        page.fill("#f_q", f"UI-{u}")
        page.click("#applyBtn")
        expect(page.locator("#ordersTable")).to_contain_text(f"UI-{u}-1", timeout=8000)
        expect(page.locator("#ordersTable tbody tr")).to_have_count(2)
        page.locator("details[data-key=channel] summary").click()
        page.locator("details[data-key=channel] input[value=Call]").check()
        page.click("#applyBtn")
        expect(page.locator("#ordersTable tbody tr")).to_have_count(1, timeout=8000)
        page.get_by_role("button", name="Save current…").click()
        page.fill("dialog input[type=text]", "UI test filter")
        page.locator("dialog button[type=submit]").click()
        expect(page.locator("#savedFilters option", has_text="UI test filter")).to_have_count(1, timeout=8000)
        page.get_by_role("button", name="Reset").click()
        page.select_option("#savedFilters", label="UI test filter")
        expect(page.locator("#ordersTable tbody tr")).to_have_count(1, timeout=8000)
        with page.expect_download() as dl:
            page.click("#exportBtn")
        assert dl.value.suggested_filename.endswith(".xlsx")

        # ---- undo the order import from the Import tab
        page.locator(".tabs > .tab", has_text="Import").click()
        page.locator("#batchTable tbody tr", has_text="orders.csv").get_by_role("button", name="Undo").click()
        page.locator("dialog").get_by_role("button", name="Undo import").click()
        undone = page.locator("#batchTable tbody tr", has_text="orders.csv").get_by_text("Undone")
        expect(undone).to_be_visible(timeout=8000)

        # ---- bulk members: passwords are shown once
        page.locator(".tabs > .tab", has_text="Members").click()
        page.click("#bulkMembersBtn")
        page.set_input_files("#importFile", str(members))
        expect(page.locator("#importBtn")).to_have_text("Import 1 members", timeout=8000)
        page.click("#importBtn")
        expect(page.locator("#credTable")).to_contain_text(f"ui.person.{u.lower()}", timeout=8000)
        browser.close()

    assert not errors, errors
