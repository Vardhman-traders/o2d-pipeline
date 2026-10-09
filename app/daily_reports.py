"""Automatic daily delivery reports.

Every day at 7:30 PM IST the app builds one PDF per delivery person (the people offered on the Godown Dispatch screen) listing every
order that person delivered that day, keeps the PDFs in the database (Reports page: download any day's), and posts a one-message
summary to the WhatsApp group set under Setup > WhatsApp ("Daily delivery summary").

How it runs: a small background thread inside the web service wakes every minute and does the day's work once, after 7:30 PM
(also if the service was restarted later in the evening). `daily_report_run` plus a database lock make that safe if more than one
copy of the service is running. Set VT_SCHEDULER=off to switch the thread off (the tests do). An admin can also press
"Generate now" on the Reports page.
"""
import logging
import os
import threading
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from . import access, db, o2d_reports, whatsapp

log = logging.getLogger("daily_reports")
IST = ZoneInfo("Asia/Kolkata")
RUN_AT = time(19, 30)
LOCK_ID = 7003
RETRY_AFTER_FAILURE_S = 600
router = APIRouter(prefix="/o2d/daily-reports", tags=["daily-reports"])

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


def generate(day: date, now: datetime | None = None) -> list[dict]:
    """(Re)build and store the day's report for every delivery person. Returns [{person, orders}]."""
    now = now or datetime.now(IST)
    out = []
    for person in people_for(day):
        rows = rows_for(person, day)
        if not rows:
            continue
        pdf = build_pdf(person, day, rows, now)
        with db.cursor() as cur:
            cur.execute("INSERT INTO daily_delivery_report (report_date, person_name, order_count, pdf, generated_at) VALUES (%s, %s, %s, %s, now()) "
                        "ON CONFLICT (report_date, person_name) DO UPDATE SET order_count = EXCLUDED.order_count, pdf = EXCLUDED.pdf, "
                        "generated_at = now()", (day, person, len(rows), pdf))
        out.append({"person": person, "orders": len(rows), "cartage": sum(float(r.get("cartage") or 0) for r in rows)})
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
    """Do today's run once, any time after 7:30 PM IST. True when this call did it."""
    now = now or datetime.now(IST)
    if now.time() < RUN_AT:
        return False
    day = now.date()
    with db.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,))
        if not cur.fetchone()["ok"]:
            return False                                   # another copy of the service is already on it
        cur.execute("SELECT 1 FROM daily_report_run WHERE report_date = %s", (day,))
        if cur.fetchone():
            return False
        done = generate(day, now)
        cur.execute("INSERT INTO daily_report_run (report_date, persons) VALUES (%s, %s) ON CONFLICT DO NOTHING", (day, len(done)))
    send_summary(day, done)
    log.info("Daily delivery reports for %s: %s person(s)", day, len(done))
    return True


class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="daily-reports", daemon=True)

    def _loop(self):
        wait = 30
        while not self._stop.wait(wait):
            wait = 60
            try:
                run_if_due()
            except Exception:
                log.exception("Daily delivery report run failed - trying again in %s minutes", RETRY_AFTER_FAILURE_S // 60)
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
        cur.execute("SELECT report_key, person_name, order_count, generated_at FROM daily_delivery_report WHERE report_date = %s "
                    "ORDER BY person_name", (day,))
        rows = cur.fetchall()
        cur.execute("SELECT report_date FROM daily_report_run ORDER BY report_date DESC LIMIT 14")
        days = [r["report_date"].isoformat() for r in cur.fetchall()]
    return {"ok": True, "date": day.isoformat(), "days": days, "runsAt": "7:30 PM IST", "canGenerate": user["role"] == "admin",
            "reports": [{"key": r["report_key"], "person": r["person_name"], "orders": r["order_count"],
                         "generatedAt": r["generated_at"].astimezone(IST).strftime("%d-%m-%Y %H:%M")} for r in rows]}


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
    return {"ok": True, "date": day.isoformat(), "persons": len(done), "message": f"Built {len(done)} report(s) for {day:%d-%m-%Y}."}
