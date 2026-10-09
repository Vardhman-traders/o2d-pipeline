# ruff: noqa: F811
"""The Missing Numbers panel in a real browser (Shop dashboard): enter 1 and 8 -> 2 to 7 appear on top; click one, enter it,
and it disappears. Needs Playwright + Chromium; skipped when missing."""
from datetime import date, timedelta

import pytest

from tests.test_sales_ui import PW, base_url, sync_api  # noqa: F401  (fixture + helpers)

pytestmark = pytest.mark.slow


def test_missing_numbers_panel_lists_clicks_and_clears(base_url):
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base_url)
        page.fill(".login-screen input[type=text]", "ui_shop")
        page.fill(".login-screen input[type=password]", PW)
        page.click(".login-screen button[type=submit]")
        page.click("text=O2D Portal")
        page.wait_for_selector("#appScreen", state="visible")
        expect(page.locator("#shop_orderVia option", has_text="Call")).to_have_count(1, timeout=8000)

        day = date.today() - timedelta(days=3)   # a day of its own: other tests in the run punch numbers for today
        for dc in ("1", "8"):
            page.evaluate(f"setDatePickerValue('shop_orderRcvdDate', '{day.isoformat()}')")
            page.select_option("#shop_orderVia", label="Call")
            page.select_option("#shop_typeOfSubmission", label="Challan")
            page.fill("#shop_dcNo", dc)
            page.click("#shop_saveBtn")
            expect(page.locator("#shop_statusMsg")).to_contain_text("Order saved", timeout=8000)

        sel = "#shop_missingContainer .gap-pill[onclick*=\"'" + day.isoformat() + "'\"]"
        pills = page.locator(sel)   # that day's pills only
        expect(pills).to_have_text(["2", "3", "4", "5", "6", "7"], timeout=8000)
        
        pills.filter(has_text="5").click()
        expect(page.locator("#mm_dcNo")).to_have_value("5")
        page.select_option("#mm_via", label="Call")
        page.fill("#mm_loc", "Rohini")
        page.click("#mm_save")
        expect(pills).to_have_text(["2", "3", "4", "6", "7"], timeout=8000)
        assert not errors, errors
        browser.close()


def test_reports_are_one_multi_select_and_download_one_pdf_each(base_url, migrated_db_url):
    import psycopg2

    from app import auth
    conn = psycopg2.connect(migrated_db_url)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES ('ui_admin_reports', %s, 'admin', 'UI admin', false) ON CONFLICT DO NOTHING", (auth.hash_password(PW),))
    conn.commit()
    conn.close()
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True).new_page()
        page.goto(base_url)
        page.fill(".login-screen input[type=text]", "ui_admin_reports")
        page.fill(".login-screen input[type=password]", PW)
        page.click(".login-screen button[type=submit]")
        page.click("text=O2D Portal")
        page.wait_for_selector("#overview_reportsCard", state="visible", timeout=10000)
        assert page.locator("#overview_reportsCard .ov-btn", has_text="pending orders").count() == 0   # no more button wall
        page.click("#rep_pickBtn")
        for kind in ("godown-pending", "receiving-pending", "shop-received-cash"):
            page.check(f'#rep_picker input[data-report="{kind}"]')
        expect(page.locator("#rep_downloadBtn")).to_have_text("⬇ Download 3 reports")
        with page.expect_download(timeout=20000) as first:
            page.click("#rep_downloadBtn")
        assert first.value.suggested_filename.endswith(".pdf")
        expect(page.locator("#rep_msg")).to_have_text("Downloaded 3 reports.", timeout=20000)
        # cartage history needs a delivery person
        page.click("#rep_pickBtn")
        page.check('#rep_picker input[data-report="cartage-detail"]')
        page.click("#rep_downloadBtn")
        expect(page.locator("#rep_msg")).to_contain_text("Choose a delivery person")
        browser.close()
