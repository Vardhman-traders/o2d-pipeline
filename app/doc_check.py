"""Does the challan / invoice attached to an order really belong to it?

Staff sometimes attach the wrong challan to an order. This reads the attached document, pulls out the bill number,
the bill date and the
total, and compares them with what was entered on the order. The order is marked PERFECT only when the number AND
the date on the
document are the ones on the order.

How it works:
  - an attachment is read ONCE (the file never changes) and the numbers read are kept in order_doc_read. The verdict
    is worked out from
    those numbers against the order AS IT IS NOW every time it is asked for, so editing the order's number or date
    afterwards takes the
    PERFECT mark away by itself;
  - a PDF with a text layer (a BUSY / computer-made invoice or challan) is read with pypdf - nothing leaves the server;
  - a photo, or a scanned PDF with no text layer, cannot be read by a PDF parser. Those are read only if the admin
    switched on the
    picture reader (environment DOC_CHECK_VISION=1 and ANTHROPIC_API_KEY): the file is then sent to Anthropic's API
    to read the three
    values. It is OFF by default because it sends business documents to a third party. Without it a photo is
    reported as "cannot be read
    automatically" - never as PERFECT;
  - the reader is never told what the order says (so it cannot be led to agree with it): it only reports what is
    printed;
  - the number alone is weak (a Challan number restarts at 1 every day), so the date must match too. When the
    document shows the right
    number but no readable date the order is "Check by eye", not PERFECT. A total is shown for information, never
    compared.

  GET  /o2d/orders/{sl_no}/doc-check   what is known now (no reading is done)
  POST /o2d/orders/{sl_no}/doc-check   reads the attachments not read yet, then answers like GET   (?refresh=true:
    admin, read all again)
"""

import base64
import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import Json

from . import attachments, db, doc_numbers

log = logging.getLogger("o2d.doccheck")
router = APIRouter(prefix="/o2d", tags=["o2d-doc-check"])

MAX_PDF_PAGES = 5
MAX_TEXT = 60_000
VISION_URL = "https://api.anthropic.com/v1/messages"
VISION_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}  # what the picture reader accepts (not HEIC)
VISION_MAX_BYTES = 5 * 1024 * 1024
STATUS_LABEL = {
    "skipped": "",
    "perfect": "PERFECT",
    "mismatch": "MISMATCH",
    "review": "CHECK BY EYE",
    "unreadable": "CANNOT READ",
    "unchecked": "NOT CHECKED YET",
    "none": "NO DOCUMENT",
}


# ------------------------------------------------------------------ reading the text
_NUMBER = re.compile(
    r"\b(?:tax\s+invoice|invoice|inv|bill|delivery\s+challan|challan|chalan|d\.?\s?c|voucher|vch)\s*"
    r"(?:no|num|number|nbr|#)\s*\.?\s*[:\-#.]*\s*([A-Za-z0-9][A-Za-z0-9/\-_.]{0,29})",
    re.I,
)
_DATE_LIKE = re.compile(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4}|\d{2})(?!\d)")
_DATE_DMY_TXT = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([A-Za-z]{3,9})\.?[\s\-/.,]*(\d{4}|\d{2})(?!\d)")
_DATE_MDY_TXT = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)")
_DATE_LABEL = re.compile(r"(?:date|dated|dt)\s*[:.\-]?\s*$", re.I)
_TOTAL = re.compile(
    r"(?:grand\s+total|net\s+(?:amount|payable|total)|total\s+amount|invoice\s+(?:value|total)|amount\s+payable|bill\s+amount|total)"
    r"\s*(?:\(?\s*(?:rs\.?|inr|₹)\s*\)?)?\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)",
    re.I,
)
_MONTHS = {
    m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
}


def _mk_date(d: int, m: int, y: int) -> date | None:
    y = y + 2000 if y < 100 else y
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_text(text: str) -> dict:
    """From the text of a document: {numbers: [as printed], dates: [iso], datesLabelled: [iso], total}."""
    text = (text or "")[:MAX_TEXT]
    numbers: list[str] = []
    for m in _NUMBER.finditer(text):
        tok = m.group(1).strip(".,:;-/_")
        if not tok or not re.search(r"\d", tok) or _DATE_LIKE.fullmatch(tok) or tok in numbers:
            continue
        numbers.append(tok)
    found: list[tuple[int, date]] = []
    for m in _DATE_NUM.finditer(text):
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    for m in _DATE_DMY_TXT.finditer(text):
        mon = _MONTHS.get(m.group(2)[:3].lower())
        d = _mk_date(int(m.group(1)), mon, int(m.group(3))) if mon else None
        if d:
            found.append((m.start(), d))
    for m in _DATE_MDY_TXT.finditer(text):
        mon = _MONTHS.get(m.group(1)[:3].lower())
        d = _mk_date(int(m.group(2)), mon, int(m.group(3))) if mon else None
        if d:
            found.append((m.start(), d))
    found.sort(key=lambda x: x[0])
    dates, labelled = [], []
    for start, d in found:
        iso = d.isoformat()
        if iso not in dates:
            dates.append(iso)
        if _DATE_LABEL.search(text[max(0, start - 20) : start]) and iso not in labelled:
            labelled.append(iso)
    total = None
    for m in _TOTAL.finditer(text):  # the last one is the bottom line of the bill
        try:
            total = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
    return {"numbers": numbers[:10], "dates": dates[:10], "datesLabelled": labelled[:5], "total": total}


def read_pdf_text(data: bytes) -> str:
    """The text layer of a PDF (first pages), '' for a scan. ValueError for a file that is not a readable PDF."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:  # pypdf is in requirements.txt; this only happens on a half-installed server
        raise RuntimeError("The PDF reader (pypdf) is not installed on the server.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("The PDF is password-protected.")
        return "\n".join((p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES])
    except PyPdfError as exc:
        raise ValueError(f"The PDF could not be opened ({exc}).")


# ------------------------------------------------------------------ the optional picture reader
def vision_enabled() -> bool:
    return os.environ.get("DOC_CHECK_VISION", "").strip() == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


VISION_PROMPT = (
    "This is an Indian delivery challan or tax invoice (a photo or scan). "
    "Read ONLY what is clearly printed or written on it and reply with one JSON object and nothing else: "
    '{"bill_number": <the invoice / challan / DC number as a string, or null>, '
    '"bill_date": <the date of the bill as YYYY-MM-DD, or null>, '
    '"total_amount": <the grand total as a number, or null>}. '
    "Dates are day-month-year. If a value is not clearly legible use null; never guess."
)


def _post_json(url: str, headers: dict, body: dict) -> dict:
    """The one network call of the picture reader (tests replace it)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def read_with_vision(data: bytes, content_type: str) -> dict:
    """The same shape as parse_text, as the model read it. RuntimeError with the reason when it could not."""
    is_pdf = content_type == "application/pdf"
    if not is_pdf and content_type not in VISION_IMAGE_TYPES:
        raise ValueError(
            "This kind of photo (for example HEIC) cannot be read automatically - JPEG, PNG, WebP or PDF can."
        )
    if len(data) > VISION_MAX_BYTES:
        raise ValueError("The file is too large to read automatically (5 MB maximum).")
    block = {
        "type": "document" if is_pdf else "image",
        "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(data).decode()},
    }
    body = {
        "model": os.environ.get("DOC_CHECK_MODEL", "claude-haiku-5-5"),
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [block, {"type": "text", "text": VISION_PROMPT}]}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        reply = _post_json(VISION_URL, headers, body)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"The picture reader refused the request (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"The picture reader could not be reached ({exc}).") from exc
    text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        got = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        got = None
    if not isinstance(got, dict):
        raise RuntimeError("The picture reader did not answer in the expected form.")
    number = str(got.get("bill_number") or "").strip()
    d = None
    try:
        d = date.fromisoformat(str(got.get("bill_date") or "").strip())
    except ValueError:
        d = None
    total = got.get("total_amount")
    return {
        "numbers": [number] if number else [],
        "dates": [d.isoformat()] if d else [],
        "datesLabelled": [d.isoformat()] if d else [],
        "total": float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None,
    }


# ------------------------------------------------------------------ comparing with the order
def _norm(tok: str) -> str:
    t = re.sub(r"[^A-Za-z0-9]", "", tok or "").upper()
    return (t.lstrip("0") or "0") if t.isdigit() else t


def expected_tokens(dc_inv_no: str | None) -> set[str]:
    """How the order's number may look on its document: '16' -> {'16'}; a 'both' order '12/345' -> {'12', '345'}."""
    dc = (dc_inv_no or "").strip()
    both = doc_numbers.split_both(dc)
    return {_norm(t) for t in (list(both) if both else [dc]) if t}


def number_matches(found: list[str], expected: set[str]) -> str | None:
    """The printed number that equals the order's number, or None. A composite number such as VT/25-26/0016 counts
    by its last group (16);
    a plain one (16A) only as a whole, so 16A is not 16."""
    for tok in found:
        cands = {_norm(tok)}
        groups = re.findall(r"\d+", tok)
        if groups and re.search(r"[/\-_.]", tok):
            cands.add(_norm(groups[-1]))
        if cands & expected:
            return tok
    return None


def date_matches(read: dict, order_date: date | None) -> bool | None:
    """True / False when the document shows a date, None when it shows none. A date after the word "date" goes first."""
    pool = read.get("datesLabelled") or read.get("dates") or []
    if not pool:
        return None
    return order_date is not None and order_date.isoformat() in pool


def _dmy(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%m-%Y")


def _shown_dates(r: dict) -> list[str]:
    return [_dmy(d) for d in (r.get("datesLabelled") or r.get("dates") or [])]


def _checks(r: dict, entered_no: str, entered_date: date | None, hit: str | None, dm: bool | None) -> list[dict]:
    checks: list[dict] = [
        {
            "field": "DC / Invoice no.",
            "entered": entered_no,
            "found": hit or ", ".join(r["numbers"]) or "not found",
            "ok": bool(hit),
        },
        {
            "field": "Date",
            "entered": entered_date.strftime("%d-%m-%Y") if entered_date else "",
            "found": ", ".join(_shown_dates(r)) or "not found",
            "ok": dm,
        },
    ]
    if r.get("total") is not None:
        checks.append({"field": "Total on the document", "entered": "", "found": f"{r['total']:,.2f}", "ok": None})
    return checks


def verdict(order: dict, reads: list[dict], attachments_n: int) -> dict:
    """order: dc_inv_no, order_received_date. reads: the stored reads (readable, numbers, dates, datesLabelled,
    total, note).
    PERFECT = some attached document shows the order's number AND the order's date. Anything less is never PERFECT."""
    entered_no = (order.get("dc_inv_no") or "").strip()
    entered_date = order.get("order_received_date")
    base = {"enteredNumber": entered_no, "enteredDate": entered_date.isoformat() if entered_date else ""}

    def out(status: str, message: str, best: dict | None = None, checks: list | None = None) -> dict:
        return {
            **base,
            "status": status,
            "label": STATUS_LABEL[status],
            "message": message,
            "checks": checks or [],
            "foundTotal": (best or {}).get("total"),
        }

    if attachments_n < 0:  # only photos, and the picture reader is off: nothing is said about them
        return out("skipped", "")
    if attachments_n == 0:
        return out("none", "No challan or invoice is attached to this order.")
    if not reads:
        return out("unchecked", 'Attached, but not checked yet. Press "Check attached document".')
    readable = [r for r in reads if r["readable"]]
    if not readable:
        return out(
            "unreadable",
            reads[0].get("note") or "The attached file could not be read automatically. Please check it by eye.",
        )
    if not entered_no:
        return out("review", "This order has no DC / Invoice number to compare the document with.", readable[0])
    expected = expected_tokens(entered_no)
    judged = [(r, number_matches(r["numbers"], expected), date_matches(r, entered_date)) for r in readable]
    for r, hit, dm in judged:
        if hit and dm is True:
            return out(
                "perfect",
                "The number and the date on the document are the same as on the order.",
                r,
                _checks(r, entered_no, entered_date, hit, dm),
            )
    for r, hit, dm in judged:
        if hit and dm is None:
            return out(
                "review",
                "The number matches, but no date could be read on the document. Please check the date by eye.",
                r,
                _checks(r, entered_no, entered_date, hit, dm),
            )
    r, hit, dm = next((j for j in judged if j[1]), judged[0])  # show the document that came closest
    why = []
    if not hit:
        why.append(
            "the document shows number "
            + (", ".join(r["numbers"]) if r["numbers"] else "none that could be read")
            + f" but the order has {entered_no}"
        )
    if dm is False:
        order_on = entered_date.strftime("%d-%m-%Y") if entered_date else "-"
        why.append(f"the document is dated {', '.join(_shown_dates(r))} but the order is dated {order_on}")
    if dm is None:
        why.append("no date could be read on the document")
    return out(
        "mismatch",
        "Wrong document? " + "; ".join(why).capitalize() + ".",
        r,
        _checks(r, entered_no, entered_date, hit, dm),
    )


# ------------------------------------------------------------------ reading an attachment and keeping the result
def _fetch(object_key: str) -> bytes:
    return attachments.storage().get_object(Bucket=attachments._bucket(), Key=object_key)["Body"].read()


def read_attachment(att: dict) -> dict:
    """Read one stored attachment (att: attachment_key, object_key, content_type). Returns the row to keep:
    {method, readable, numbers, dates, datesLabelled, total, note}. Raises RuntimeError when it could not be done
    NOW (nothing is kept, so
    asking again retries)."""
    ctype = (att["content_type"] or "").lower()
    data = _fetch(att["object_key"])
    empty: dict = {"numbers": [], "dates": [], "datesLabelled": [], "total": None}
    if ctype == "application/pdf":
        try:
            text = read_pdf_text(data)
        except ValueError as exc:
            return {**empty, "method": "none", "readable": False, "note": str(exc)}
        parsed = parse_text(text)
        if text.strip() and (parsed["numbers"] or parsed["dates"]):
            return {**parsed, "method": "pdf_text", "readable": True, "note": ""}
        if not vision_enabled():
            why = (
                "This PDF is a scan or picture (it has no text to read)"
                if not text.strip()
                else "No bill number or date could be found in the text of this PDF"
            )
            return {
                **empty,
                "method": "none",
                "readable": False,
                "note": why + ", so it cannot be checked automatically. Please check it by eye.",
            }
    elif not vision_enabled():
        return {
            **empty,
            "method": "none",
            "readable": False,
            "note": "This is a photo. Photos cannot be read automatically (only PDFs made by a computer, like the BUSY "
            "invoice, can). Please check it by eye, or attach the PDF.",
        }
    try:
        got = read_with_vision(data, ctype)
    except ValueError as exc:  # this particular file cannot be read this way: a lasting answer
        return {**empty, "method": "none", "readable": False, "note": str(exc)}
    readable = bool(got["numbers"] or got["dates"])
    return {
        **got,
        "method": "vision",
        "readable": readable,
        "note": "" if readable else "Nothing could be read clearly on this picture. Please check it by eye.",
    }


def _store(cur, att: dict, order_key: int, res: dict, user_key: int | None) -> None:
    cur.execute(
        """INSERT INTO order_doc_read (attachment_key, order_key, method, readable, doc_numbers, doc_dates,
                                      doc_dates_labelled, doc_total, note, read_by_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (attachment_key) DO UPDATE SET method = EXCLUDED.method, readable = EXCLUDED.readable,
               doc_numbers = EXCLUDED.doc_numbers, doc_dates = EXCLUDED.doc_dates,
               doc_dates_labelled = EXCLUDED.doc_dates_labelled, doc_total = EXCLUDED.doc_total,
               note = EXCLUDED.note, read_at = now(), read_by_key = EXCLUDED.read_by_key""",
        (
            att["attachment_key"],
            order_key,
            res["method"],
            res["readable"],
            Json(res["numbers"]),
            Json(res["dates"]),
            Json(res["datesLabelled"]),
            res["total"],
            (res.get("note") or "")[:300] or None,
            user_key,
        ),
    )


def read_pending(order_key: int, user: dict, refresh: bool = False) -> list[str]:
    """Read the order's attachments that have no stored read (all of them when refresh). Returns a sentence per
    attachment that could not be
    read right now (a network failure, a missing file); those are retried the next time."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT a.attachment_key, a.object_key, a.content_type, a.kind FROM order_attachments a
                       LEFT JOIN order_doc_read r ON r.attachment_key = a.attachment_key
                       WHERE a.order_key = %s AND (%s OR r.attachment_key IS NULL) ORDER BY a.attachment_key""",
            (order_key, refresh),
        )
        todo = cur.fetchall()
    if not vision_enabled():  # no picture reader: leave photos unread, so enabling it later reads them
        todo = [a for a in todo if (a["content_type"] or "").lower() == "application/pdf"]
    problems = []
    for att in todo:
        try:
            res = read_attachment(att)
        except Exception as exc:
            log.exception(
                "document check: could not read attachment %s of order_key %s (user %s %s, content type %s)",
                att["attachment_key"],
                order_key,
                user.get("user_key"),
                user.get("username"),
                att["content_type"],
            )
            problems.append(f"{att['kind']} file #{att['attachment_key']}: {exc}")
            continue
        with db.cursor() as cur:
            _store(cur, att, order_key, res, user.get("user_key"))
    return problems


def check_after_upload(order_key: int, attachment_key: int, user: dict) -> None:
    """Background job after an upload: read the new file so the order shows its verdict at once. Never raises."""
    try:
        read_pending(order_key, user)
    except Exception:
        log.exception(
            "document check after upload failed: order_key %s attachment %s user %s (%s)",
            order_key,
            attachment_key,
            user.get("user_key"),
            user.get("username"),
        )


def state(order: dict) -> dict:
    """The verdict and the documents of an order, from what is stored (no reading)."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT a.attachment_key, a.kind, a.content_type, r.method, r.readable, r.doc_numbers, r.doc_dates,
                      r.doc_dates_labelled, r.doc_total, r.note
               FROM order_attachments a LEFT JOIN order_doc_read r ON r.attachment_key = a.attachment_key
               WHERE a.order_key = %s ORDER BY a.attachment_key""",
            (order["order_key"],),
        )
        rows = cur.fetchall()
    reads, docs = [], []
    for r in rows:
        done = r["method"] is not None
        read = {
            "readable": bool(r["readable"]),
            "numbers": r["doc_numbers"] or [],
            "dates": r["doc_dates"] or [],
            "datesLabelled": r["doc_dates_labelled"] or [],
            "total": float(r["doc_total"]) if r["doc_total"] is not None else None,
            "note": r["note"] or "",
        }
        if done:
            reads.append(read)
        docs.append(
            {
                "id": r["attachment_key"],
                "kind": r["kind"],
                "contentType": r["content_type"],
                "checked": done,
                "method": r["method"] or "",
                **read,
            }
        )
    reader_on = vision_enabled()
    unread_photos = [r for r in rows if r["method"] is None and (r["content_type"] or "").lower() != "application/pdf"]
    n = len(rows)
    if not reader_on and unread_photos and len(unread_photos) == n:
        n = -1
    return {"verdict": verdict(order, reads, n), "documents": docs, "readerOn": reader_on}


# ------------------------------------------------------------------ the routes
@router.get("/orders/{sl_no}/doc-check")
def get_doc_check(sl_no: int, user=Depends(attachments.ANY_VIEWER)):
    order = attachments._visible_order(user, sl_no)
    return {"ok": True, "problems": [], **state(order)}


@router.post("/orders/{sl_no}/doc-check")
def run_doc_check(sl_no: int, refresh: bool = Query(default=False), user=Depends(attachments.ANY_VIEWER)):
    if not attachments.configured():
        raise HTTPException(
            503, "Photo storage is not set up yet, so attached documents cannot be read. Ask the administrator."
        )
    if refresh and user["role"] != "admin":
        raise HTTPException(403, "Only an admin can read the documents again.")
    order = attachments._visible_order(user, sl_no)
    problems = read_pending(order["order_key"], user, refresh)
    return {"ok": True, "problems": problems, **state(order)}
