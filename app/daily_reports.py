"""Automatic daily reports, in two batches a day (IST):

  7:30 PM  one PDF per delivery person (the people offered on the Godown Dispatch screen) listing every order that person delivered that
           day, plus Godown / Shop received & payments (cash), (other mode), Godown + Shop (bank) and (all other transactions);
  9:00 PM  Awaiting receiving, and the Godown / Shop Dispatch / Godown Dispatch pending-orders reports.

The PDFs are kept in the database (Reports page: download any day's). After each batch ONE message goes to the Reports group set under
Setup > WhatsApp ("Daily reports - 7:30 PM" / "9:00 PM"): every report with a download link that works without a login and expires after
3 days (needs PUBLIC_BASE_URL, or Render's RENDER_EXTERNAL_URL; without it the message lists the reports and points to the portal). The
7:30 PM batch also posts the one-message delivery summary ("Daily delivery summary").

How it runs: a small background thread inside the web service wakes every minute and does each batch once, after its time (also if the
service was restarted later in the evening). `daily_report_run` plus a database lock make that safe if more than one copy of the service
is running. Set VT_SCHEDULER=off to switch the thread off (the tests do). An admin can also press "Generate now" on the Reports page.
"""
import hashlib
import hmac
import logging
import os
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from . import access, auth, db, o2d_reports, whatsapp

log = logging.getLogger("daily_reports")
IST = ZoneInfo("Asia/Kolkata")
LOCK_ID = 7003
RETRY_AFTER_FAILURE_S = 600
LINK_DAYS = 3
MESSAGE_LIMIT = 3300           # WhatsApp takes ~4,000 characters: a long list goes out as several messages
router = APIRouter(prefix="/o2d/daily-reports", tags=["daily-reports"])

# kinds = every order-based report kept for the batch (the Reports card's); whatsapp = the ones listed in the group message, in order
BATCHES = {
    "evening": {"at": time(19, 30), "label": "7:30 PM", "persons": True, "alert": "reports_evening",
                "kinds": ("godown-received-cash", "shop-received-cash", "godown-received-other", "shop-received-other", "received-bank", "received-other-all"),
                "whatsapp": ("godown-received-cash", "shop-received-cash", "godown-received-other", "shop-received-other")},
    "night": {"at": time(21, 0), "label": "9:00 PM", "persons": False, "alert": "reports_night",
              "kinds": ("receiving-pending", "godown-pending", "shop-dispatch-pending", "godown-dispatch-pending"),
              "whatsapp": ("receiving-pending", "godown-pending", "shop-dispatch-pending", "godown-dispatch-pending")},
}
RUN_AT = BATCHES["evening"]["at"]
DAILY_REPORT_KINDS = BATCHES["evening"]["kinds"] + BATCHES["night"]["kinds"]
# who the automatic reports are built for: sees every (active) order, like an admin
_SYSTEM_USER = {"role": "admin", "username": "automatic daily report", "display_name": "Automatic daily report"}

_COLS = [("Order date", "order_received_date", 22, "L"), ("DC / Inv No", "dc_inv_no", 24, "L"), ("Address", "shipping_location", 68, "L"),
         ("Delivered at", "material_delivery_datetime", 32, "L"), ("Delivered by", "delivered_by_full", 50, "L"),
         ("Cartage (Rs)", "cartage", 26, "R"), ("Receiving / payment", "payment_status", 40, "L")]


def _delivered_that_day(day: date) -> str:
    return "(o.material_delivery_datetime AT TIME ZONE 'Asia/Kolkata')::date = %s"


def people_for(day: date) -> list[str]:
    """Delivery people (offered on the Godown Dispatch screen) who delivered at least one order on `day`."""
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT dp.full_name, dp.sort_order FROM fact_orders o JOIN dim_person dp ON dp.person_key = o.delivered_by_person_key "
                    f"WHERE {_delivered_that_day(day)} AND NOT o.is_cancelled AND o.archived_at IS NULL "
                    "AND dp.dispatch_scope IN ('both', 'godown') ORDER BY dp.sort_order, dp.full_name", (day,))
        return [r["full_name"] for r in cur.fetchall()]


def rows_for(person: str, day: date) -> list[dict]:
    from . import main
    with db.cursor() as cur:
        cur.execute(main.ORDER_SELECT + f" WHERE dp.full_name = %s AND {_delivered_that_day(day)} AND NOT o.is_cancelled "
                    f"AND o.archived_at IS NULL ORDER BY {o2d_reports._BY_DOC_NO}", (person, day))
        return [o2d_reports._prepare(r) for r in cur.fetchall()]


def build_pdf(person: str, day: date, rows: list[dict], generated_at: datetime) -> bytes:
    section = {"heading": f"Orders delivered by {person} on {day:%d-%m-%Y}", "columns": _COLS, "rows": rows, "totals": True}
    return o2d_reports.build_pdf(f"Delivery report - {person} - {day:%d-%m-%Y}", [section], "Automatic daily report", generated_at,
                                 f"Deliveries on {day:%d-%m-%Y}", "#1b7f8c")


def generate(day: date, now: datetime | None = None, batches: tuple = ("evening", "night")) -> list[dict]:
    """(Re)build and store the day's reports of the given batches (all by default). Returns the delivery people's [{person, orders, cartage}]."""
    now = now or datetime.now(IST)
    persons: list[dict] = []
    for b in batches:
        got, _ = generate_batch(day, now, b)
        persons += got
    return persons


def generate_batch(day: date, now: datetime, batch: str) -> tuple[list[dict], list[dict]]:
    """One batch: (the delivery people reports built, every report stored as [{key, title, orders, kind}])."""
    spec = BATCHES[batch]
    persons, stored = [], []
    if spec["persons"]:
        for person in people_for(day):
            rows = rows_for(person, day)
            if not rows:
                continue
            pdf = build_pdf(person, day, rows, now)
            with db.cursor() as cur:
                cur.execute("INSERT INTO daily_delivery_report (report_date, person_name, order_count, pdf, generated_at) VALUES (%s, %s, %s, %s, now()) "
                            "ON CONFLICT (report_date, person_name) DO UPDATE SET order_count = EXCLUDED.order_count, pdf = EXCLUDED.pdf, "
                            "generated_at = now() RETURNING report_key", (day, person, len(rows), pdf))
                key = cur.fetchone()["report_key"]
            persons.append({"person": person, "orders": len(rows), "cartage": sum(float(r.get("cartage") or 0) for r in rows)})
            stored.append({"key": key, "title": f"Delivery - {person}", "orders": len(rows), "kind": None})
    stored += generate_reports(day, now, spec["kinds"])
    return persons, stored


def generate_reports(day: date, now: datetime, kinds: tuple = DAILY_REPORT_KINDS) -> list[dict]:
    """(Re)build and store pending / received reports. One failing report is logged and skipped so the others are still kept.
    Returns [{key, title, orders, kind}] for each one stored."""
    stored = []
    for kind in kinds:
        spec = o2d_reports.REPORTS[kind]
        received = spec.get("date_field") == o2d_reports._RECEIVED_ON
        try:
            pdf, count = o2d_reports.build_report(spec, _SYSTEM_USER, day if received else None, day if received else None,
                                                  "Automatic daily report", now)
        except Exception:
            log.exception("Could not build the daily report %s for %s", kind, day)
            continue
        with db.cursor() as cur:
            cur.execute("INSERT INTO daily_delivery_report (report_date, person_name, order_count, pdf, generated_at, report_kind) "
                        "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (report_date, person_name) DO UPDATE SET "
                        "order_count = EXCLUDED.order_count, pdf = EXCLUDED.pdf, generated_at = now(), report_kind = EXCLUDED.report_kind "
                        "RETURNING report_key", (day, spec["title"], count, pdf, kind))
            key = cur.fetchone()["report_key"]
        stored.append({"key": key, "title": spec["title"], "orders": count, "kind": kind})
    return stored


# ------------------------------------------------------------------ links that work without a login (for the WhatsApp message)
def _base_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


def _sign(report_key: int, exp: int) -> str:
    return hmac.new(auth._secret().encode(), f"report:{report_key}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def shared_link(report_key: int, now: datetime | None = None) -> str:
    """A download link for one stored report: no login, valid LINK_DAYS days. Empty when the public address of the app is not known."""
    base = _base_url()
    if not base:
        return ""
    exp = int(((now or datetime.now(IST)) + timedelta(days=LINK_DAYS)).timestamp())
    return f"{base}/o2d/daily-reports/shared/{report_key}.pdf?exp={exp}&sig={_sign(report_key, exp)}"


def report_blocks(stored: list[dict], batch: str) -> list[str]:
    """The numbered lines of the group message, each with its link: the delivery people first, then the batch's WhatsApp reports in order."""
    spec = BATCHES[batch]
    by_kind = {r["kind"]: r for r in stored if r["kind"]}
    entries = [r for r in stored if r["kind"] is None] + [by_kind[k] for k in spec["whatsapp"] if k in by_kind]
    blocks = []
    for n, r in enumerate(entries, 1):
        link = shared_link(r["key"])
        blocks.append(f"{n}. {r['title']} ({r['orders']} order{'s' if r['orders'] != 1 else ''})" + (f"\n{link}" if link else ""))
    return blocks


def send_reports(day: date, batch: str, stored: list[dict]) -> dict:
    """Post the batch's reports to the Reports group (one message, or several when the list is long). Never raises."""
    spec = BATCHES[batch]
    blocks = report_blocks(stored, batch)
    if not blocks:
        return {"skipped": True, "reason": "No reports in this batch."}
    if not _base_url():
        blocks.append("(Download them in the portal: O2D > Reports.)")
    chunks, cur_len, cur = [], 0, []
    for b in blocks:
        if cur and cur_len + len(b) > MESSAGE_LIMIT:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b) + 2
    chunks.append(cur)
    out: dict = {}
    for part in chunks:
        try:
            out = whatsapp.send_alert(spec["alert"], {"date": f"{day:%d-%m-%Y}", "time": spec["label"], "list": "\n\n".join(part)})
        except Exception:  # the reports are saved; a failed message must not undo them
            log.exception("Could not send the %s reports on WhatsApp", spec["label"])
            return {"skipped": True, "reason": "WhatsApp failed."}
        if out.get("skipped"):
            break
    return out


def summary_text(done: list[dict]) -> str:
    return "\n".join(f"{d['person']} - {d['orders']} order{'s' if d['orders'] != 1 else ''}" +
                     (f", cartage Rs {d['cartage']:,.0f}" if d["cartage"] else "") for d in done)


def send_summary(day: date, done: list[dict]) -> dict:
    if not done:
        return {"skipped": True, "reason": "No deliveries that day."}
    try:
        return whatsapp.send_alert("delivery_day_summary", {"date": f"{day:%d-%m-%Y}", "summary": summary_text(done)})
    except Exception:  # the reports are saved; a failed message must not undo them
        log.exception("Could not send the daily delivery summary on WhatsApp")
        return {"skipped": True, "reason": "WhatsApp failed."}


def run_if_due(now: datetime | None = None) -> bool:
    """Do today's batches once each, any time after their time (7:30 PM, 9:00 PM IST). True when this call did at least one."""
    now = now or datetime.now(IST)
    did = False
    for batch, spec in BATCHES.items():
        if now.time() < spec["at"]:
            continue
        day = now.date()
        with db.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,))
            if not cur.fetchone()["ok"]:
                return did                                 # another copy of the service is already on it
            cur.execute("SELECT 1 FROM daily_report_run WHERE report_date = %s AND batch = %s", (day, batch))
            if cur.fetchone():
                continue
            persons, stored = generate_batch(day, now, batch)
            cur.execute("INSERT INTO daily_report_run (report_date, batch, persons) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (day, batch, len(persons)))
        if batch == "evening":
            send_summary(day, persons)
        send_reports(day, batch, stored)
        log.info("Daily %s reports for %s: %s report(s)", spec["label"], day, len(stored))
        did = True
    return did


class Scheduler:
    """A thread that calls `job()` every minute (and again after RETRY_AFTER_FAILURE_S when it raises)."""
    def __init__(self, job=None, name="daily-reports"):
        self._job = job or run_if_due
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)

    def _loop(self):
        wait = 30
        while not self._stop.wait(wait):
            wait = 60
            try:
                self._job()
            except Exception:
                log.exception("Background job %s failed - trying again in %s minutes", self._thread.name, RETRY_AFTER_FAILURE_S // 60)
                wait = RETRY_AFTER_FAILURE_S

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()


def start_scheduler() -> Scheduler | None:
    if os.environ.get("VT_SCHEDULER", "on").lower() == "off":
        return None
    s = Scheduler()
    s.start()
    return s


# ------------------------------------------------------------------ the Reports page
@router.get("")
def list_reports(day: date | None = Query(default=None, alias="date"), user=Depends(access.require_page("o2d_reports"))):
    day = day or datetime.now(IST).date()
    with db.cursor() as cur:
        cur.execute("SELECT report_key, person_name, order_count, generated_at, report_kind FROM daily_delivery_report WHERE report_date = %s "
                    "ORDER BY report_kind IS NOT NULL, person_name", (day,))
        rows = cur.fetchall()
        cur.execute("SELECT DISTINCT report_date FROM daily_report_run ORDER BY report_date DESC LIMIT 14")
        days = [r["report_date"].isoformat() for r in cur.fetchall()]
    return {"ok": True, "date": day.isoformat(), "days": days, "runsAt": "7:30 PM and 9:00 PM IST", "canGenerate": user["role"] == "admin",
            "reports": [{"key": r["report_key"], "person": r["person_name"], "orders": r["order_count"], "kind": r["report_kind"],
                         "generatedAt": r["generated_at"].astimezone(IST).strftime("%d-%m-%Y %H:%M")} for r in rows]}


@router.get("/shared/{report_key}.pdf")
def download_shared(report_key: int, exp: int = Query(), sig: str = Query()):
    """The link in the WhatsApp message: no login, but only with the signature made for this report, and only until it expires."""
    if exp < int(datetime.now(IST).timestamp()) or not hmac.compare_digest(_sign(report_key, exp), sig):
        raise HTTPException(403, "This link has expired or is not valid. Open the report from the portal (O2D > Reports).")
    with db.cursor() as cur:
        cur.execute("SELECT report_date, person_name, pdf FROM daily_delivery_report WHERE report_key = %s", (report_key,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "Report not found.")
    name = "report_" + "".join(c if c.isalnum() else "_" for c in r["person_name"]) + f"_{r['report_date']:%Y%m%d}.pdf"
    return Response(bytes(r["pdf"]), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/{report_key}.pdf")
def download(report_key: int, user=Depends(access.require_page("o2d_reports"))):
    with db.cursor() as cur:
        cur.execute("SELECT report_date, person_name, pdf FROM daily_delivery_report WHERE report_key = %s", (report_key,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "Report not found.")
    name = "delivery_" + "".join(c if c.isalnum() else "_" for c in r["person_name"]) + f"_{r['report_date']:%Y%m%d}.pdf"
    return Response(bytes(r["pdf"]), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/run")
def generate_now(day: date | None = Query(default=None, alias="date"), user=Depends(access.require_page("o2d_reports"))):
    """Admin: build the reports for a day right now (default today), e.g. to try it out or after a correction."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only an admin can generate the reports on demand.")
    day = day or datetime.now(IST).date()
    done = generate(day)
    return {"ok": True, "date": day.isoformat(), "persons": len(done),
            "message": f"Built the delivery reports ({len(done)} person(s)) and the {len(DAILY_REPORT_KINDS)} pending / received reports for {day:%d-%m-%Y} (nothing is sent to WhatsApp from here)."}
