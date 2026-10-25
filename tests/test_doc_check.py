"""The PERFECT check: is the challan / invoice attached to an order the right one (number AND date)?"""
import io
from datetime import date

import pytest
from reportlab.pdfgen import canvas

from app import attachments, doc_check, o2d_screens
from tests.conftest import bill_no
from tests.test_o2d_features import FakeR2, _order_at_dispatch
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)

TODAY = date(2026, 10, 25)


def pdf_with(*lines: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


class Store(FakeR2):
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture()
def r2(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("DOC_CHECK_VISION", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = Store()
    attachments.set_storage(fake)
    yield fake
    attachments.set_storage(None)


# ------------------------------------------------------------------ reading text
def test_parse_text_finds_the_number_date_and_total():
    p = doc_check.parse_text("VARDHMAN TRADERS\nTax Invoice\nInvoice No. : VT/25-26/0016    Dated: 25-Oct-2026\nSubtotal 900.00\nGrand Total Rs. 1,180.50\n")
    assert p["numbers"] == ["VT/25-26/0016"] and p["datesLabelled"] == ["2026-10-25"] and p["total"] == 1180.5
    c = doc_check.parse_text("DELIVERY CHALLAN\nChallan No: 16\nDate: 25/10/2026\nDC No. 16\n")
    assert c["numbers"] == ["16"] and c["dates"] == ["2026-10-25"]               # the same number twice is one number
    assert doc_check.parse_text("Inv No 7 Date 25.10.26")["dates"] == ["2026-10-25"]
    assert doc_check.parse_text("Bill No: Date: 25/10/2026")["numbers"] == []     # a word after "No" is not a number
    assert doc_check.parse_text("Oct 25, 2026")["dates"] == ["2026-10-25"] and doc_check.parse_text("31/02/2026")["dates"] == []
    assert doc_check.parse_text("")["numbers"] == [] and doc_check.parse_text(None)["total"] is None


def test_number_matching_rules():
    exp = doc_check.expected_tokens
    assert doc_check.number_matches(["0016"], exp("16")) == "0016"                # leading zeros do not matter
    assert doc_check.number_matches(["VT/25-26/0016"], exp("16")) == "VT/25-26/0016"   # a composite number counts by its last group
    assert doc_check.number_matches(["16A"], exp("16")) is None                   # 16A is not 16
    assert doc_check.number_matches(["26"], exp("16")) is None
    assert doc_check.number_matches(["345"], exp("12/345")) and doc_check.number_matches(["12"], exp("12/345"))   # a "both" order has two
    assert doc_check.number_matches(["abc-1"], exp("ABC-1")) == "abc-1"


def _read(numbers=(), dates=(), labelled=None, total=None, readable=True, note=""):
    return {"readable": readable, "numbers": list(numbers), "dates": list(dates), "datesLabelled": list(dates if labelled is None else labelled),
            "total": total, "note": note}


def test_verdict_is_perfect_only_when_number_and_date_both_match():
    order = {"dc_inv_no": "16", "order_received_date": TODAY}
    v = doc_check.verdict
    assert v(order, [], 0)["status"] == "none" and v(order, [], 1)["status"] == "unchecked"
    assert v(order, [_read(["16"], ["2026-10-25"])], 1)["status"] == "perfect"
    wrong_no = v(order, [_read(["15"], ["2026-10-25"])], 1)
    assert wrong_no["status"] == "mismatch" and "15" in wrong_no["message"] and "16" in wrong_no["message"]
    wrong_day = v(order, [_read(["16"], ["2026-10-24"])], 1)
    assert wrong_day["status"] == "mismatch" and "24-10-2026" in wrong_day["message"]      # challan 16 of yesterday is not challan 16 of today
    assert v(order, [_read(["16"], [])], 1)["status"] == "review"                          # no date on the document: a person must look
    assert v(order, [_read([], [], readable=False, note="It is a photo.")], 1)["status"] == "unreadable"
    assert v({"dc_inv_no": "", "order_received_date": TODAY}, [_read(["16"], ["2026-10-25"])], 1)["status"] == "review"
    # two documents: one right one is enough; a wrong one next to it does not spoil it
    assert v(order, [_read(["15"], ["2026-10-25"]), _read(["16"], ["2026-10-25"], total=500.0)], 2)["status"] == "perfect"
    # a date next to the word "date" outranks other dates printed on the page (a due date, a print date)
    assert v(order, [_read(["16"], ["2026-10-30", "2026-10-25"], labelled=["2026-10-25"])], 1)["status"] == "perfect"
    assert v(order, [_read(["16"], ["2026-10-25", "2026-10-30"], labelled=["2026-10-30"])], 1)["status"] == "mismatch"
    assert v(order, [_read(["16"], ["2026-10-25"], total=1180.5)], 1)["checks"][-1] == {"field": "Total on the document", "entered": "", "found": "1,180.50", "ok": None}


# ------------------------------------------------------------------ attached to a real order
def _attach(client, headers, sl, data, ctype="application/pdf", kind="dispatch"):
    r = client.post(f"/o2d/orders/{sl}/photos?kind={kind}", content=data, headers={**headers, "Content-Type": ctype})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_a_matching_pdf_is_read_on_upload_and_the_order_is_perfect(client, db_conn, seed, r2):
    dc = bill_no()
    sl = _order_at_dispatch(client, db_conn, dc, "Delivered")
    gd, admin = login(client, db_conn, "godown_dispatch"), login(client, db_conn, "admin")
    today = o2d_screens.today_ist()
    assert client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()["verdict"]["status"] == "none"
    _attach(client, gd, sl, pdf_with("DELIVERY CHALLAN", f"Challan No: {dc}", f"Date: {today.strftime('%d-%m-%Y')}", "Grand Total Rs. 2,500.00"))
    got = client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()          # read in the background right after the upload
    assert got["verdict"]["status"] == "perfect" and got["verdict"]["label"] == "PERFECT" and got["verdict"]["foundTotal"] == 2500.0
    assert got["documents"][0]["method"] == "pdf_text" and got["documents"][0]["numbers"] == [dc]
    # the order's list entry still says what it did; and the admin sees the same
    assert client.get(f"/o2d/orders/{sl}/doc-check", headers=admin).json()["verdict"]["status"] == "perfect"
    # fixing the order's number afterwards takes the mark away, with no new reading
    with db_conn.cursor() as cur:
        cur.execute("UPDATE fact_orders SET dc_inv_no = %s WHERE sl_no = %s", (str(int(dc) + 1), sl))
    db_conn.commit()
    after = client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()["verdict"]
    assert after["status"] == "mismatch" and dc in after["message"]


def test_a_wrong_challan_is_a_mismatch_and_a_photo_is_never_perfect(client, db_conn, seed, r2):
    dc = bill_no()
    sl = _order_at_dispatch(client, db_conn, dc, "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    today = o2d_screens.today_ist()
    _attach(client, gd, sl, pdf_with("Challan No: 7", f"Date: {today.strftime('%d-%m-%Y')}"))
    v = client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()["verdict"]
    assert v["status"] == "mismatch" and v["checks"][0]["ok"] is False and v["checks"][1]["ok"] is True
    # a photo: nothing reads it unless the picture reader is on - it says so and is not PERFECT
    sl2 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    _attach(client, gd, sl2, b"\xff\xd8photo", "image/jpeg")
    v2 = client.get(f"/o2d/orders/{sl2}/doc-check", headers=gd).json()
    assert v2["verdict"]["status"] == "skipped" and v2["verdict"]["message"] == "" and v2["readerOn"] is False   # silent, never PERFECT
    # a PDF with no text (a scan) is also reported as unreadable, not guessed
    sl3 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    _attach(client, gd, sl3, pdf_with(), "application/pdf")
    assert client.get(f"/o2d/orders/{sl3}/doc-check", headers=gd).json()["verdict"]["status"] == "unreadable"
    # something that is not a PDF at all, called a PDF
    sl4 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    _attach(client, gd, sl4, b"this is not a pdf", "application/pdf")
    bad = client.get(f"/o2d/orders/{sl4}/doc-check", headers=gd).json()["verdict"]
    assert bad["status"] == "unreadable" and "PDF" in bad["message"]


def test_the_check_follows_order_visibility_and_storage(client, db_conn, seed, r2, monkeypatch):
    sl = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    gd, admin, cashier = login(client, db_conn, "godown_dispatch"), login(client, db_conn, "admin"), login(client, db_conn, "cashier")
    assert client.get(f"/o2d/orders/{sl}/doc-check", headers=cashier).status_code in (403, 404)
    assert client.post(f"/o2d/orders/{sl}/doc-check", headers=cashier).status_code in (403, 404)
    assert client.post(f"/o2d/orders/{sl}/doc-check?refresh=true", headers=gd).status_code == 403        # re-reading everything is the admin's
    assert client.post(f"/o2d/orders/{sl}/doc-check?refresh=true", headers=admin).status_code == 200
    assert client.get("/o2d/orders/999999/doc-check", headers=admin).status_code == 404
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert client.post(f"/o2d/orders/{sl}/doc-check", headers=admin).status_code == 503


def test_the_button_reads_what_was_not_read_and_a_failure_is_reported_not_stored(client, db_conn, seed, r2, monkeypatch):
    dc = bill_no()
    sl = _order_at_dispatch(client, db_conn, dc, "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    today = o2d_screens.today_ist()
    monkeypatch.setattr(doc_check, "check_after_upload", lambda *a, **k: None)            # as if the background read never happened
    _attach(client, gd, sl, pdf_with(f"Challan No: {dc}", f"Date: {today.strftime('%d-%m-%Y')}"))
    assert client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()["verdict"]["status"] == "unchecked"
    # the file cannot be fetched right now: the answer says which file and why, and nothing is kept, so a retry works
    key = next(iter(r2.objects))
    body, r2.objects[key] = r2.objects[key], None
    r2.get_object = lambda Bucket, Key: (_ for _ in ()).throw(OSError("storage is down"))
    down = client.post(f"/o2d/orders/{sl}/doc-check", headers=gd).json()
    assert down["verdict"]["status"] == "unchecked" and "storage is down" in down["problems"][0]
    del r2.get_object
    r2.objects[key] = body
    ok = client.post(f"/o2d/orders/{sl}/doc-check", headers=gd).json()
    assert ok["problems"] == [] and ok["verdict"]["status"] == "perfect"


def test_the_optional_picture_reader_reads_photos_but_is_never_told_the_answer(client, db_conn, seed, r2, monkeypatch):
    dc = bill_no()
    sl = _order_at_dispatch(client, db_conn, dc, "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    today = o2d_screens.today_ist()
    monkeypatch.setenv("DOC_CHECK_VISION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    seen = []

    def fake_post(url, headers, body):
        seen.append((url, headers, body))
        return {"content": [{"type": "text", "text": '{"bill_number": "%s", "bill_date": "%s", "total_amount": 640}' % (dc, today.isoformat())}]}

    monkeypatch.setattr(doc_check, "_post_json", fake_post)
    _attach(client, gd, sl, b"\xff\xd8photo", "image/jpeg")
    v = client.get(f"/o2d/orders/{sl}/doc-check", headers=gd).json()
    assert v["verdict"]["status"] == "perfect" and v["documents"][0]["method"] == "vision" and v["readerOn"] is True
    url, headers, body = seen[0]
    assert url == doc_check.VISION_URL and headers["x-api-key"] == "k" and body["messages"][0]["content"][0]["type"] == "image"
    assert dc not in str(body) and today.isoformat() not in str(body)                  # the order's own values are never sent along
    # it reads a different number: mismatch. An unreadable answer is "cannot read", not PERFECT
    monkeypatch.setattr(doc_check, "_post_json", lambda *a: {"content": [{"type": "text", "text": '{"bill_number": "3", "bill_date": null, "total_amount": null}'}]})
    sl2 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    _attach(client, gd, sl2, b"\xff\xd8photo2", "image/jpeg")
    assert client.get(f"/o2d/orders/{sl2}/doc-check", headers=gd).json()["verdict"]["status"] == "mismatch"
    monkeypatch.setattr(doc_check, "_post_json", lambda *a: {"content": [{"type": "text", "text": "I cannot see a bill."}]})
    sl3 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    monkeypatch.setattr(doc_check, "check_after_upload", lambda *a, **k: None)
    _attach(client, gd, sl3, b"\xff\xd8photo3", "image/jpeg")
    res = client.post(f"/o2d/orders/{sl3}/doc-check", headers=gd).json()
    assert res["verdict"]["status"] == "unchecked" and "expected form" in res["problems"][0]     # reported, not stored
    # HEIC cannot be sent to the reader: a lasting "cannot read", with the reason
    sl4 = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    _attach(client, gd, sl4, b"heic-bytes", "image/heic")
    heic = client.post(f"/o2d/orders/{sl4}/doc-check", headers=gd).json()["verdict"]
    assert heic["status"] == "unreadable" and "HEIC" in heic["message"]


def test_pdfs_are_accepted_as_order_documents_and_other_types_are_not(client, db_conn, seed, r2):
    sl = _order_at_dispatch(client, db_conn, bill_no(), "Delivered")
    gd = login(client, db_conn, "godown_dispatch")
    assert client.post(f"/o2d/orders/{sl}/photos?kind=dispatch", content=b"x", headers={**gd, "Content-Type": "text/html"}).status_code == 415
    _attach(client, gd, sl, pdf_with("x"))
    listed = client.get(f"/o2d/orders/{sl}/photos", headers=gd).json()["photos"]
    assert listed[0]["contentType"] == "application/pdf" and ".pdf" in listed[0]["url"]
