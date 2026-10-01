"""Browser test of the admin portal: filters + Excel export, bulk upload of orders (with an inline fix and an undo)
and bulk member creation. Needs Playwright + Chromium; skipped when missing."""
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


def _section(page, module, name):
    """Home tile -> module -> sub-tab (the admin area is Home > Dashboard | Setup)."""
    if page.locator("#homeBtn").count():
        page.click("#homeBtn")
    page.click("#tileDashboard" if module == "Dashboard" else "#tileSetup")
    page.locator(".module-tabs .subtab", has_text=re.compile(rf"^{name}$")).click()


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
        _section(page, "Setup", "Import")
        page.set_input_files("#importFile", str(csv))
        expect(page.locator("#errorTable")).to_contain_text("'Phone' not found", timeout=8000)
        page.locator("#errorTable input[aria-label^='Order Via']").fill("Walk-in")
        page.click("#recheckBtn")
        expect(page.locator("#importBtn")).to_have_text("Import 2 orders", timeout=8000)
        page.click("#importBtn")
        expect(page.locator("#importStatus")).to_contain_text("Imported 2 orders", timeout=8000)
        expect(page.locator("#batchTable")).to_contain_text("orders.csv", timeout=8000)

        # ---- orders tab: find orders by number (any date), narrow with a dropdown filter, export
        _section(page, "Dashboard", "All orders")
        page.fill("#f_sl", f"UI-{u}")
        expect(page.locator("#ordersTable")).to_contain_text(f"UI-{u}-1", timeout=8000)
        expect(page.locator("#ordersTable tbody tr")).to_have_count(2)
        channel_filter = page.locator(".multiselect", has=page.locator("button", has_text="Order via"))
        channel_filter.locator("button.multiselect-btn").click()
        channel_filter.get_by_label("Call").check()
        expect(page.locator("#ordersTable tbody tr")).to_have_count(1, timeout=8000)
        # the dropdown closes by itself after a pick and the button shows the choice
        expect(channel_filter.locator(".ms-panel")).to_be_hidden()
        expect(channel_filter.locator("button.multiselect-btn")).to_contain_text("Call")
        # Find button filters the table; digits-only exact; Clear brings the rest back
        page.fill("#f_sl", f"UI-{u}-2")
        page.click("#f_find")
        expect(page.locator("#ordersTable tbody tr")).to_have_count(0, timeout=8000)  # channel=Call still set
        channel_filter.locator("button.multiselect-btn").click()
        channel_filter.get_by_label("Call").uncheck()
        expect(page.locator("#ordersTable tbody tr")).to_have_count(1, timeout=8000)
        page.click("#f_clearfind")
        # header sorting: click once = sorted, click again = reversed
        sort_btn = page.locator("#ordersTable thead .sort-btn[data-sort=channel]")
        sort_btn.click()
        expect(sort_btn.locator(".sort-arrow")).to_have_text("▲", timeout=8000)
        sort_btn.click()
        expect(sort_btn.locator(".sort-arrow")).to_have_text("▼", timeout=8000)
        # sticky: after scrolling, the filter panel and the table header stay in view
        page.fill("#f_sl", f"UI-{u}")
        page.click("#f_find")
        page.set_viewport_size({"width": 1280, "height": 380})   # short window so the page must scroll
        page.evaluate("window.scrollTo(0, 100000)")
        assert page.evaluate("scrollY") > 0
        assert 100 < page.locator("#orderFilters").bounding_box()["y"] < 140  # pinned under the tab row
        head_y = page.locator("#ordersTable thead th").first.bounding_box()["y"]
        assert head_y < 330, head_y  # header pinned under the filters
        page.set_viewport_size({"width": 1280, "height": 900})
        with page.expect_download() as dl:
            page.click("#exportBtn")
        assert dl.value.suggested_filename.endswith(".xlsx")

        # ---- undo the order import from the Import tab
        _section(page, "Setup", "Import")
        page.locator("#batchTable tbody tr", has_text="orders.csv").get_by_role("button", name="Undo").click()
        page.locator("dialog").get_by_role("button", name="Undo import").click()
        undone = page.locator("#batchTable tbody tr", has_text="orders.csv").get_by_text("Undone")
        expect(undone).to_be_visible(timeout=8000)

        # ---- bulk members: passwords are shown once
        _section(page, "Setup", "Members")
        page.click("#bulkMembersBtn")
        page.set_input_files("#importFile", str(members))
        expect(page.locator("#importBtn")).to_have_text("Import 1 members", timeout=8000)
        page.click("#importBtn")
        expect(page.locator("#credTable")).to_contain_text(f"ui.person.{u.lower()}", timeout=8000)
        browser.close()

    assert not errors, errors


def test_admin_cleans_up_duplicate_names(base_url, migrated_db_url):
    expect = sync_api.expect
    u = uuid.uuid4().hex[:4].upper()
    conn = psycopg2.connect(migrated_db_url)
    cur = conn.cursor()
    keys = {}
    for name in (f"Suresh{u}", f"Sures{u}", f"Bablu{u}"):
        cur.execute("INSERT INTO dim_person (full_name, person_role) VALUES (%s, 'delivery') "
                    "RETURNING person_key", (name,))
        keys[name] = cur.fetchone()[0]
    cur.execute("INSERT INTO fact_orders (sl_no, dc_inv_no, order_received_date_key, delivered_by_person_key) "
                "SELECT COALESCE(max(sl_no), 0) + 1, %s, %s, %s FROM fact_orders",
                (f"CL-{u}", int(date.today().strftime("%Y%m%d")), keys[f"Sures{u}"]))
    conn.commit()
    errors: list[str] = []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "CSP" in m.text else None)
        page.goto(base_url)
        page.fill("input[autocomplete=username]", "ui_admin")
        page.fill("input[autocomplete=current-password]", PW)
        page.click("button[type=submit]")
        _section(page, "Setup", "Reconciliation")

        # the pair is flagged as a possible duplicate; merge it, keeping the correct spelling
        pair = page.locator("#rc_dups tr", has_text=f"Suresh{u}")
        expect(pair).to_have_count(1, timeout=8000)
        pair.get_by_role("button", name="Review & merge").click()
        page.select_option("#rc_keep", label=f"Suresh{u} — 0 orders")
        expect(page.locator("#rc_preview")).to_contain_text("1 order(s) will be moved", timeout=8000)
        page.click("#rc_confirm")
        expect(page.locator("#rc_table tbody tr", has_text=f"Sures{u}")).to_have_count(0, timeout=8000)
        expect(page.locator("#rc_table tbody tr", has_text=f"Suresh{u}")).to_have_count(1)
        expect(page.locator("#rc_table tbody tr", has_text=f"Suresh{u}")).to_contain_text("1")

        # rename a value in place
        page.locator("#rc_table tbody tr", has_text=f"Bablu{u}").get_by_role("button", name="Rename").click()
        page.fill("dialog input[type=text]", f"Babloo{u}")
        page.locator("dialog button[type=submit]").click()
        expect(page.locator("#rc_table tbody tr", has_text=f"Babloo{u}")).to_have_count(1, timeout=8000)
        browser.close()

    cur.execute("SELECT count(*) FROM dim_person WHERE full_name = %s", (f"Sures{u}",))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT p.full_name FROM fact_orders o JOIN dim_person p ON p.person_key = o.delivered_by_person_key "
                "WHERE o.dc_inv_no = %s", (f"CL-{u}",))
    assert cur.fetchone()[0] == f"Suresh{u}"
    conn.close()
    assert not errors, errors


def test_admin_home_screen_and_navigation(base_url):
    expect = sync_api.expect
    errors: list[str] = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base_url)
        page.fill("input[autocomplete=username]", "ui_admin")
        page.fill("input[autocomplete=current-password]", PW)
        page.click("button[type=submit]")

        # home: big tiles for Dashboard, the O2D Portal and Setup; nothing crowding the top bar
        expect(page.locator("#tileDashboard")).to_be_visible(timeout=8000)
        expect(page.locator(".app-tile", has_text="O2D Portal")).to_have_count(1)
        expect(page.locator("#tileSetup")).to_be_visible()
        expect(page.locator(".topbar .open-app")).to_have_count(0)
        expect(page.locator(".topbar")).not_to_contain_text("Open Sales Portal")
        assert page.locator(".app-tile").first.bounding_box()["width"] >= 180

        # Dashboard module: the overview and the full order list
        page.click("#tileDashboard")
        expect(page.locator(".module-tabs .subtab")).to_have_text(["Overview", "All orders"])
        # overview: dropdown-style filters under a "Filter by" label, a From/To period, and the archive note
        expect(page.locator(".filter-label", has_text="Filter by")).to_be_visible(timeout=8000)
        expect(page.locator(".multiselect-btn .ms-caret").first).to_be_visible()
        expect(page.locator("#dash_from")).to_be_visible()
        expect(page.locator("#dash_to")).to_be_visible()
        expect(page.locator("#archiveNote")).to_contain_text("Archived orders are not included")
        expect(page.locator("#archiveNote")).to_contain_text("7 days")
        # a reversed range is refused with a message instead of querying
        page.fill("#dash_from", "2999-01-01")
        expect(page.locator("#dash_range_msg")).to_contain_text("must be on or before")
        page.click("#dash_reset")
        expect(page.locator("#dash_range_msg")).to_have_text("")
        # Download Excel sits top-right of the tab row, and the big page title is gone
        expect(page.locator("#moduleActions")).to_contain_text("Download Excel")
        expect(page.locator(".module-title")).to_have_count(0)
        page.locator(".module-tabs .subtab", has_text="All orders").click()
        expect(page.locator("#orderCount")).to_be_visible(timeout=8000)
        # All orders: same Period / Filter by layout, export in the same top-right spot
        expect(page.locator(".filter-label", has_text="Period")).to_be_visible()
        expect(page.locator(".filter-label", has_text="Filter by")).to_be_visible()
        expect(page.locator("#f_sl")).to_be_visible()
        expect(page.locator("#f_from")).not_to_have_value("")  # defaults to the last 30 days, like the Overview
        expect(page.locator("#moduleActions #exportBtn")).to_be_visible()
        page.click("#f_last30")
        expect(page.locator("#f_from")).not_to_have_value("")

        # Setup module: members, import, reconciliation and the rest; no activity log
        page.click("#homeBtn")
        page.click("#tileSetup")
        expect(page.locator(".module-tabs .subtab")).to_have_text(
            [re.compile(p) for p in (
                r"^Members$", r"^Import$", r"^Reconciliation$", r"^Access$",
                r"^Access requests( \(\d+\))?$",   # carries a count while requests are waiting
                r"^Dropdown values$", r"^Permissions$", r"^Weekly off$", r"^Apps$")])
        page.locator(".module-tabs .subtab", has_text="Reconciliation").click()
        expect(page.locator("#rc_table")).to_be_visible(timeout=8000)
        assert "Activity log" not in page.content()
        browser.close()
    assert not errors, errors
