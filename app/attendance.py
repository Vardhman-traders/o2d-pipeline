"""Attendance module: employees check in and out from their phone (GPS spot + photo), the owner sees the day, the month, salary,
advances and streaks. This replaces the Google Apps Script "Attendance" project; both pages are the original ones.

How it works:
  - Two sets of calls, both "one call per button" (POST .../rpc {action, args}), named exactly like the script's functions:
      /attendance/public/rpc  what an employee does on the check-in page: pick own name, check location, check in / out, see own
                              salary after the PIN. Open to anyone with the link (as before) while Setup > Module settings
                              "Employee page open to anyone with the link" is 1; set it to 0 and the page needs a portal sign-in.
      /attendance/owner/rpc   the owner dashboard: today, reports, salary, advances, PINs, employees. Needs the page access
                              "Attendance - Owner dashboard" (the old OWNER_PASSWORD / MASTER_PASSWORD no longer exist).
  - the rules are Setup, not code: the Godown / Shop spots and radius (Setup > Dropdown values), the late / half-day / absent times,
    the 8 PM auto check-out time, the weekly-off day and the 30 base salary days (Setup > Module settings), who gets no WhatsApp
    message (employee list), and the message wording and group (Setup > WhatsApp);
  - an employee's salary PIN is kept hashed. Five wrong tries lock it for fifteen minutes (both numbers are settings). The owner
    can set or reset a PIN but can never read one;
  - WhatsApp messages (check-in, check-out, 8 PM summary) are sent right after the check-in / check-out and retried every five
    minutes for today's entries that did not go out; a background job also does the auto check-out once a day after 9 PM.
"""
import logging
import math
import os
import threading
import time as _time
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2.extras import Json
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, daily_reports, db, module_admin, modcommon as mc, whatsapp

log = logging.getLogger("attendance")
router = APIRouter(prefix="/attendance", tags=["attendance"])
OWNER_PAGE = "att_owner"
JS_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTHS_SHORT = [m[:3] for m in MONTHS]
WA = {"IN_PENDING": "IN Pending", "IN_SENT": "IN Sent", "OUT_PENDING": "OUT Pending", "BOTH_PENDING": "IN+OUT Pending",
      "DONE": "Done", "AUTO_DONE": "Done (Auto)", "SKIPPED": "Skipped (No WA)"}
AUTO_RUN_LOCK = 7004


# ------------------------------------------------------------------ rules (Setup > Module settings)
class Rules:
    """The numbers the script kept as constants, read once per request from Setup."""
    def __init__(self):
        spec = {s["key"]: s for s in module_admin.SETTINGS}
        val = lambda k: module_admin.setting_value(spec[k])  # noqa: E731
        mins = lambda k: self._minutes(str(val(k)), str(spec[k]["default"]))  # noqa: E731
        self.late_after = mins("att_late_after")
        self.half_checkin = mins("att_half_day_checkin")
        self.absent_before = mins("att_absent_before")
        self.half_before = mins("att_half_day_before")
        self.full_until = mins("att_full_day_until")
        self.auto_out = mins("att_auto_checkout_time")
        self.auto_out_text = f"{self.auto_out // 60:02d}:{self.auto_out % 60:02d}"
        self.auto_run_at = mins("att_auto_run_time")
        self.off_day = int(val("att_weekly_off_day"))
        self.base_days = int(val("att_salary_base_days"))
        self.link_on = int(val("att_employee_link_on")) == 1
        self.pin_attempts = int(val("att_pin_max_attempts"))
        self.pin_lock_minutes = int(val("att_pin_lock_minutes"))

    @staticmethod
    def _minutes(text: str, default: str) -> int:
        try:
            h, m = text.split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            h, m = default.split(":")
            return int(h) * 60 + int(m)

    # --- the classification rules, exactly as the script had them
    def checkin_status(self, in_min: int) -> str:
        if in_min >= self.half_checkin:
            return "Half Day"
        if in_min >= self.late_after:
            return "Late"
        return "Present"

    def final_status(self, in_min: int, out_min: int) -> str:
        if out_min < self.absent_before:
            return "Absent"
        if out_min < self.half_before:
            return "Half Day"
        if out_min <= self.full_until:
            if in_min >= self.half_checkin:
                return "Half Day"
            return "Late" if in_min >= self.late_after else "Present"
        return "Late" if in_min >= self.late_after else "Present"

    def resolve(self, final: str | None, in_min: int | None) -> str:
        """An 'Incomplete' day counts as an auto check-out at the auto time (so salary, reports and streaks agree)."""
        st = (final or "").strip()
        if st and st != "Incomplete":
            return st
        return self.final_status(in_min, self.auto_out) if in_min is not None else "Incomplete"

    def js_dow(self, d: date) -> int:
        return (d.weekday() + 1) % 7

    def is_off(self, d: date) -> bool:
        return self.js_dow(d) == self.off_day


def _mins(t: time | None) -> int | None:
    return t.hour * 60 + t.minute if t else None


def _hhmm(t: time | None) -> str:
    return t.strftime("%H:%M") if t else ""


def _to12h(hhmm: str) -> str:
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        return hhmm or "—"
    return f"{(h % 12) or 12:02d}:{m:02d} {'PM' if h >= 12 else 'AM'}"


def _hours(in_min: int | None, out_min: int | None) -> tuple[str, int | None]:
    if in_min is None or out_min is None or out_min - in_min < 0:
        return "—", None
    d = out_min - in_min
    return f"{d // 60}h {d % 60:02d}m", d


def _hours_text(minutes: int | None) -> str:
    return "—" if minutes is None else f"{minutes // 60}h {minutes % 60:02d}m"


def _round(x: float) -> int:
    return int(math.floor(x + 0.5))   # JavaScript's Math.round (halves go up)


def _now() -> datetime:
    return datetime.now(mc.IST)


def _dmy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


# ------------------------------------------------------------------ employees, places
def _find_employee(cur, name, active_only=True) -> dict | None:
    cur.execute("SELECT * FROM dim_att_employee WHERE lower(btrim(employee_name)) = lower(btrim(%s))" +
                (" AND is_active" if active_only else ""), (str(name or ""),))
    return cur.fetchone()


def _haversine(lat1, lng1, lat2, lng2) -> float:
    r, f1, f2 = 6371000, math.radians(lat1), math.radians(lat2)
    df, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def allowed_place_keys(cur, emp_key: int | None) -> list[int]:
    """The work places this employee is tagged to (Attendance owner dashboard > Employees). Empty = any work place."""
    if not emp_key:
        return []
    cur.execute("SELECT location_key FROM att_employee_location WHERE employee_key = %s", (emp_key,))
    return [r["location_key"] for r in cur.fetchall()]


def check_location(cur, lat, lng, emp_key: int | None = None) -> dict:
    """Nearest work place whose own radius contains the phone; otherwise 'out_of_range' with every distance.

    An employee tagged to particular work places can only mark from those: standing at another work place (or anywhere else)
    gives 'wrong_location' with a sentence naming where they may mark from."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"status": "error", "msg": "Location not received. Allow location access and try again."}
    cur.execute("SELECT * FROM dim_att_location ORDER BY sort_order, location_key")
    places = [(p, _haversine(lat, lng, float(p["latitude"]), float(p["longitude"]))) for p in cur.fetchall()]
    if not places:
        return {"status": "error", "msg": "No work location is set up. Ask the administrator (Setup > Dropdown values)."}
    inside = [(p, d) for p, d in places if d <= p["radius_m"]]
    out: dict = {"radius": max(p["radius_m"] for p, _ in places)}
    for p, d in places:   # the page shows the two spots as "Godown" / "Shop"
        out["dist" + p["location_name"].strip().title().replace(" ", "")] = _round(d)
    allowed = allowed_place_keys(cur, emp_key)
    if allowed:
        mine = [(p, d) for p, d in places if p["location_key"] in allowed]
        if mine and not any(p["location_key"] in allowed for p, _ in inside):
            names = [p["location_name"] for p, _ in mine]
            at = min(inside, key=lambda x: x[1])[0]["location_name"] if inside else None
            where = f"You are at {at}, which is not one of your work places." if at else "You are not at any of your work places."
            near, near_d = min(mine, key=lambda x: x[1])
            return {**out, "status": "wrong_location", "site": near["location_name"], "dist": _round(near_d), "allowed": names,
                    "msg": f"Attendance can only be marked from {' or '.join(names)}. {where}"}
        if mine:
            inside = [(p, d) for p, d in inside if p["location_key"] in allowed]
    if inside:
        p, d = min(inside, key=lambda x: x[1])
        return {**out, "status": "ok", "site": p["location_name"], "dist": _round(d), "location_key": p["location_key"], "icon": p["icon"]}
    p, d = min(places, key=lambda x: x[1])
    return {**out, "status": "out_of_range", "site": p["location_name"], "dist": _round(d)}


# ------------------------------------------------------------------ WhatsApp
def _wa_ok(code: int, body: str) -> bool:
    return whatsapp.reply_ok(code, body)


def _log_wa(kind: str, name: str, code, ok: bool, body: str):
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO att_wa_log (kind, employee_name, http_code, ok, response) VALUES (%s, %s, %s, %s, %s)",
                        (kind, name, code if isinstance(code, int) else None, ok, (body or "")[:500]))
    except Exception:
        log.exception("could not write the WhatsApp log")


def _send(alert_key: str, values: dict, kind: str, name: str) -> str:
    """'sent' | 'failed' | 'off' (the alert is switched off in Setup). Every attempt is logged."""
    try:
        out = whatsapp.send_alert(alert_key, values, post=whatsapp._http_post)   # looked up now, so tests can swap the sender
    except Exception as exc:
        _log_wa(kind, name, None, False, f"EXCEPTION: {exc}")
        return "failed"
    if out.get("skipped"):
        if "switched off" in out["reason"]:
            return "off"
        _log_wa(kind, name, None, False, out["reason"])
        return "failed"
    ok = _wa_ok(out.get("code"), out.get("body", ""))
    _log_wa(kind, name, out.get("code"), ok, out.get("body", ""))
    return "sent" if ok else "failed"


ROW_SQL = """SELECT a.*, e.employee_name, e.skip_whatsapp, l.location_name, l.icon
             FROM fact_attendance a JOIN dim_att_employee e ON e.employee_key = a.employee_key
             LEFT JOIN dim_att_location l ON l.location_key = a.location_key"""


def _in_values(r: dict, rules: Rules) -> dict:
    st = rules.checkin_status(_mins(r["check_in_time"]))
    return {"icon": {"Present": "🟢", "Late": "🟡"}.get(st, "🟠"), "name": r["employee_name"], "time": _to12h(_hhmm(r["check_in_time"])),
            "status": st, "site_icon": r["icon"] or "📍", "site": r["location_name"] or "",
            "dist": f" ({r['distance_m']}m)" if r["distance_m"] is not None else "", "date": r["work_date"].strftime("%d %b %Y")}


def _out_values(r: dict) -> dict:
    return {"name": r["employee_name"], "check_in": _to12h(_hhmm(r["check_in_time"])), "check_out": _to12h(_hhmm(r["check_out_time"])),
            "hours": _hours_text(r["hours_worked_min"]), "final_status": r["final_status"],
            "by_line": "🙋 *Checkout by:* Owner (Manual)\n" if r["checkout_source"] == "Manual (Owner)" else "",
            "date": r["work_date"].strftime("%d %b %Y")}


def process_wa_row(attendance_key: int) -> dict:
    """Send what is pending for one attendance row. One at a time per row (a second caller gets 'busy', never a double message)."""
    rules = Rules()
    with db.cursor() as cur:
        cur.execute(ROW_SQL + " WHERE a.attendance_key = %s FOR UPDATE OF a SKIP LOCKED", (attendance_key,))
        r = cur.fetchone()
        if r is None:
            return {"status": "busy"}
        st = (r["wa_status"] or "").strip()
        need_in, need_out = st in (WA["IN_PENDING"], WA["BOTH_PENDING"]), st in (WA["OUT_PENDING"], WA["BOTH_PENDING"])
        if not need_in and not need_out:
            return {"status": "nothing", "waStatus": st}
        if r["skip_whatsapp"]:
            cur.execute("UPDATE fact_attendance SET wa_status = %s WHERE attendance_key = %s", (WA["SKIPPED"], attendance_key))
            return {"status": "nothing", "waStatus": WA["SKIPPED"]}
        if need_in:
            res = _send("att_checkin", _in_values(r, rules), "CHECK-IN", r["employee_name"])
            if res == "failed":
                return {"status": "failed", "waStatus": st}
            st = WA["OUT_PENDING"] if need_out else WA["IN_SENT"]
            cur.execute("UPDATE fact_attendance SET wa_status = %s WHERE attendance_key = %s", (st, attendance_key))
        if need_out:
            res = _send("att_checkout", _out_values(r), "CHECK-OUT", r["employee_name"])
            if res == "failed":
                cur.execute("UPDATE fact_attendance SET wa_status = %s WHERE attendance_key = %s", (WA["OUT_PENDING"], attendance_key))
                return {"status": "failed", "waStatus": WA["OUT_PENDING"]}
            st = WA["DONE"]
            cur.execute("UPDATE fact_attendance SET wa_status = %s WHERE attendance_key = %s", (st, attendance_key))
        return {"status": "sent", "waStatus": st}


def process_pending_whatsapp() -> dict:
    today = mc.today_ist()
    with db.cursor() as cur:
        cur.execute("SELECT attendance_key FROM fact_attendance WHERE work_date = %s AND wa_status IN (%s, %s, %s) ORDER BY attendance_key LIMIT 100",
                    (today, WA["IN_PENDING"], WA["OUT_PENDING"], WA["BOTH_PENDING"]))
        keys = [r["attendance_key"] for r in cur.fetchall()]
    sent = failed = 0
    for k in keys:
        res = process_wa_row(k)["status"]
        sent += res == "sent"
        failed += res == "failed"
    return {"sent": sent, "failed": failed}


def _next_wa_after_checkout(current: str | None) -> str:
    return WA["BOTH_PENDING"] if (current or "").strip() in (WA["IN_PENDING"], WA["BOTH_PENDING"]) else WA["OUT_PENDING"]


# ------------------------------------------------------------------ photos
def _store_photo(attendance_key: int, kind: str, data_url: str) -> int | None:
    """Keep the check-in / check-out photo; without storage (or on a bad file) the attendance still counts, just without a photo."""
    if not data_url or not attachments.configured():
        return None
    if not data_url.startswith("data:"):
        data_url = "data:image/jpeg;base64," + data_url
    try:
        return attachments.put_data_url("attendance", attendance_key, kind, {"user_key": None}, data_url)
    except HTTPException:
        return None
    except Exception:
        log.exception("attendance photo upload failed")
        return None


def _photo_url(attachment_key: int | None) -> str:
    if not attachment_key or not attachments.configured():
        return ""
    with db.cursor() as cur:
        cur.execute("SELECT object_key FROM file_attachment WHERE attachment_key = %s", (attachment_key,))
        r = cur.fetchone()
    if not r:
        return ""
    return attachments.storage().generate_presigned_url("get_object", Params={"Bucket": attachments._bucket(), "Key": r["object_key"]},
                                                        ExpiresIn=attachments.LINK_SECONDS)


# ------------------------------------------------------------------ the employee page (public)
_ELIGIBLE = "u.role NOT IN ('admin', 'legacy') AND u.password_hash <> %s"
_sync_at = 0.0


def sync_members(force: bool = False) -> None:
    """Keep the attendance employee list in step with the portal members: a member with the same name is linked to the existing
    employee, any other member is added, a renamed member renames the employee, a disabled member is made inactive. At most every 5 s."""
    global _sync_at
    if not force and _time.time() - _sync_at < 5:
        return
    _sync_at = _time.time()
    d = auth.DISABLED_HASH
    with db.cursor() as cur:
        cur.execute(f"""UPDATE dim_att_employee e SET user_key = u.user_key FROM dim_user u
                        WHERE e.user_key IS NULL AND {_ELIGIBLE} AND lower(btrim(e.employee_name)) = lower(btrim(u.display_name))
                          AND NOT EXISTS (SELECT 1 FROM dim_att_employee x WHERE x.user_key = u.user_key)""", (d,))
        cur.execute(f"""INSERT INTO dim_att_employee (employee_name, role_title, user_key)
                        SELECT DISTINCT ON (lower(btrim(u.display_name))) left(btrim(u.display_name), 100), initcap(replace(u.role, '_', ' ')), u.user_key
                        FROM dim_user u WHERE {_ELIGIBLE} AND btrim(u.display_name) <> ''
                          AND NOT EXISTS (SELECT 1 FROM dim_att_employee x WHERE x.user_key = u.user_key)
                          AND NOT EXISTS (SELECT 1 FROM dim_att_employee x WHERE lower(btrim(x.employee_name)) = lower(btrim(u.display_name)))
                        ORDER BY lower(btrim(u.display_name)), u.user_key""", (d,))
        cur.execute("""UPDATE dim_att_employee e SET employee_name = left(btrim(u.display_name), 100) FROM dim_user u
                       WHERE e.user_key = u.user_key AND e.employee_name <> left(btrim(u.display_name), 100) AND btrim(u.display_name) <> ''
                         AND NOT EXISTS (SELECT 1 FROM dim_att_employee x WHERE x.employee_key <> e.employee_key
                                         AND lower(btrim(x.employee_name)) = lower(btrim(u.display_name)))""")
        cur.execute("""UPDATE dim_att_employee e SET is_active = false FROM dim_user u
                       WHERE e.user_key = u.user_key AND e.is_active AND (u.password_hash = %s OR u.role = 'legacy')""", (d,))


def get_employees(rules=None) -> list[dict]:
    sync_members()
    with db.cursor() as cur:
        cur.execute("SELECT employee_name, role_title FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)")
        return [{"name": r["employee_name"], "role": r["role_title"] or ""} for r in cur.fetchall()]


def _today_row(cur, emp_key: int, day: date) -> dict | None:
    cur.execute(ROW_SQL + " WHERE a.employee_key = %s AND a.work_date = %s", (emp_key, day))
    return cur.fetchone()


def get_today_status(name):
    day = mc.today_ist()
    with db.cursor() as cur:
        emp = _find_employee(cur, name)
        r = _today_row(cur, emp["employee_key"], day) if emp else None
    if not r:
        return {"checkedIn": False, "checkedOut": False}
    out = r["check_out_time"] is not None
    return {"checkedIn": True, "checkedOut": out, "checkInTime": _hhmm(r["check_in_time"]), "checkOutTime": _hhmm(r["check_out_time"]),
            "hoursWorked": _hours_text(r["hours_worked_min"]) if out else "", "finalStatus": r["final_status"], "rowIndex": r["attendance_key"]}


def check_location_rpc(lat, lng, name=None):
    with db.cursor() as cur:
        emp = _find_employee(cur, name) if name else None
        out = check_location(cur, lat, lng, emp["employee_key"] if emp else None)
    out.pop("location_key", None)
    return out


def save_attendance(data: dict):
    data = data or {}
    rules = Rules()
    with db.cursor() as cur:
        emp = _find_employee(cur, data.get("name"))
        if not emp:
            return {"status": "error", "msg": "Employee list mein naam nahi mila. Page reload karo."}
        loc = check_location(cur, data.get("lat"), data.get("lng"), emp["employee_key"])
        if loc["status"] == "error":
            return loc
        if loc["status"] != "ok":
            return {k: v for k, v in loc.items() if k.startswith("dist") or k in ("radius", "status", "msg", "allowed")}
        now = _now()
        existing = _today_row(cur, emp["employee_key"], now.date())
        if existing:
            return {"status": "already_marked", "time": _to12h(_hhmm(existing["check_in_time"])), "rowIndex": existing["attendance_key"]}
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, location_key, distance_m, final_status, wa_status) "
                    "VALUES (%s, %s, %s, %s, %s, 'Incomplete', %s) ON CONFLICT (employee_key, work_date) DO NOTHING RETURNING attendance_key",
                    (emp["employee_key"], now.date(), now.time().replace(microsecond=0), loc["location_key"], loc["dist"],
                     WA["SKIPPED"] if emp["skip_whatsapp"] else WA["IN_PENDING"]))
        row = cur.fetchone()
        if not row:   # a second tap a moment later
            again = _today_row(cur, emp["employee_key"], now.date())
            return {"status": "already_marked", "time": _to12h(_hhmm(again["check_in_time"])), "rowIndex": again["attendance_key"]}
        key = row["attendance_key"]
    photo = _store_photo(key, "in", data.get("photo") or "")
    if photo:
        with db.cursor() as cur:
            cur.execute("UPDATE fact_attendance SET photo_in_key = %s WHERE attendance_key = %s", (photo, key))
    return {"status": "ok", "time": now.strftime("%I:%M %p"), "dist": loc["dist"],
            "site": loc["site"], "email": "Not logged in", "rowIndex": key, "photoSaved": bool(photo)}


def save_checkout(data: dict):
    data = data or {}
    rules = Rules()
    with db.cursor() as cur:
        emp = _find_employee(cur, data.get("name"))
        if not emp:
            return {"status": "error", "msg": "Employee list mein naam nahi mila. Page reload karo."}
        loc = check_location(cur, data.get("lat"), data.get("lng"), emp["employee_key"])
        if loc["status"] == "error":
            return loc
        if loc["status"] != "ok":
            return {k: v for k, v in loc.items() if k.startswith("dist") or k in ("radius", "status", "msg", "allowed")}
        now = _now()
        r = _today_row(cur, emp["employee_key"], now.date())
        if not r:
            return {"status": "no_checkin", "msg": "Aaj ka check-in nahi mila"}
        if r["check_out_time"] is not None:
            return {"status": "already_checkout", "time": _to12h(_hhmm(r["check_out_time"])), "rowIndex": r["attendance_key"]}
        in_min, out_min = _mins(r["check_in_time"]), now.hour * 60 + now.minute
        hours_text, hours_min = _hours(in_min, out_min)
        status = rules.final_status(in_min, out_min)
        cur.execute("UPDATE fact_attendance SET check_out_time = %s, checkout_source = 'Self', hours_worked_min = %s, final_status = %s, "
                    "wa_status = %s, updated_at = now() WHERE attendance_key = %s",
                    (now.time().replace(second=0, microsecond=0), hours_min, status,
                     WA["SKIPPED"] if emp["skip_whatsapp"] else _next_wa_after_checkout(r["wa_status"]), r["attendance_key"]))
        key, check_in_text = r["attendance_key"], _to12h(_hhmm(r["check_in_time"]))
    photo = _store_photo(key, "out", data.get("photo") or "")
    if photo:
        with db.cursor() as cur:
            cur.execute("UPDATE fact_attendance SET photo_out_key = %s WHERE attendance_key = %s", (photo, key))
    return {"status": "ok", "time": now.strftime("%I:%M %p"), "hoursWorked": hours_text, "checkInTime": check_in_text,
            "finalStatus": status, "rowIndex": key, "photoSaved": bool(photo)}


def notify_attendance(row_index, name):
    try:
        key = int(row_index)
    except (TypeError, ValueError):
        return {"status": "error", "msg": "Invalid row"}
    with db.cursor() as cur:
        cur.execute("SELECT e.employee_name FROM fact_attendance a JOIN dim_att_employee e ON e.employee_key = a.employee_key "
                    "WHERE a.attendance_key = %s", (key,))
        r = cur.fetchone()
    if not r:
        return {"status": "error", "msg": "Invalid row"}
    if not name or r["employee_name"].strip().lower() != str(name).strip().lower():
        return {"status": "error", "msg": "Name mismatch"}
    return process_wa_row(key)


def _month_records(cur, emp_key: int, year: int, month: int) -> dict[int, dict]:
    cur.execute("SELECT work_date, check_in_time, check_out_time, checkout_source, final_status, hours_worked_min, l.location_name "
                "FROM fact_attendance a LEFT JOIN dim_att_location l ON l.location_key = a.location_key "
                "WHERE a.employee_key = %s AND extract(year FROM work_date) = %s AND extract(month FROM work_date) = %s", (emp_key, year, month))
    return {r["work_date"].day: r for r in cur.fetchall()}


def _advances_for(cur, emp_key: int, year: int, month: int) -> list[dict]:
    cur.execute("SELECT advance_date, created_at, amount, note FROM fact_att_advance WHERE employee_key = %s "
                "AND extract(year FROM advance_date) = %s AND extract(month FROM advance_date) = %s ORDER BY advance_key", (emp_key, year, month))
    return [{"date": r["created_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M"), "amount": float(r["amount"]), "note": r["note"] or ""}
            for r in cur.fetchall()]


PUBLIC = {
    "getEmployees": lambda a: get_employees(),
    "getTodayStatus": lambda a: get_today_status(a[0]),
    "checkLocation": lambda a: check_location_rpc(a[0], a[1], a[2] if len(a) > 2 else None),
    "saveAttendance": lambda a: save_attendance(a[0]),
    "saveCheckout": lambda a: save_checkout(a[0]),
    "notifyAttendance": lambda a: notify_attendance(a[0], a[1]),
    "getDashboardUrl": lambda a: "/attendance/dashboard.html",
}


# ------------------------------------------------------------------ throttling the open endpoints
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()
RATE_PER_MINUTE = 90


def _throttle(request: Request):
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = _time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(recent) >= RATE_PER_MINUTE:
            raise HTTPException(429, "Too many requests. Wait a minute and try again.")
        recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)


class RpcIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    args: list = []


@router.post("/public/rpc")
def public_rpc(body: RpcIn, request: Request):
    _throttle(request)
    if not Rules().link_on:
        raise HTTPException(403, "The employee check-in page is switched off. Ask the administrator.")
    fn = PUBLIC.get(body.action)
    if not fn:
        raise HTTPException(404, "Unknown action")
    return fn(body.args)


@router.get("/public/status")
def public_status():
    """Tells the employee page whether it may be used without signing in (so it can ask for the portal sign-in instead)."""
    return {"open": Rules().link_on}


# ------------------------------------------------------------------ the owner dashboard
OWNER_DEP = access.require_page(OWNER_PAGE)


def _need_emp(cur, name) -> dict:
    emp = _find_employee(cur, name, active_only=False)
    if not emp:
        raise HTTPException(404, "Employee not found")
    return emp


def _marked_row(r: dict, rules: Rules, urls: dict) -> dict:
    out = r["check_out_time"] is not None
    return {"name": r["employee_name"], "time": _hhmm(r["check_in_time"]), "site": r["location_name"] or "",
            "checkinStatus": rules.checkin_status(_mins(r["check_in_time"])), "finalStatus": r["final_status"], "checkedOut": out,
            "checkoutTime": _hhmm(r["check_out_time"]) if out else "—", "hoursWorked": _hours_text(r["hours_worked_min"]) if out else "—",
            "photoIn": urls.get(r["photo_in_key"], ""), "photoOut": urls.get(r["photo_out_key"], "")}


def _signed(keys: list[int]) -> dict[int, str]:
    keys = [k for k in keys if k]
    if not keys or not attachments.configured():
        return {}
    with db.cursor() as cur:
        cur.execute("SELECT attachment_key, object_key FROM file_attachment WHERE attachment_key = ANY(%s)", (keys,))
        rows = cur.fetchall()
    return {r["attachment_key"]: attachments.storage().generate_presigned_url(
        "get_object", Params={"Bucket": attachments._bucket(), "Key": r["object_key"]}, ExpiresIn=attachments.LINK_SECONDS) for r in rows}


def get_today_attendance(user):
    rules, today = Rules(), mc.today_ist()
    with db.cursor() as cur:
        cur.execute(ROW_SQL + " WHERE a.work_date = %s ORDER BY a.check_in_time", (today,))
        rows = cur.fetchall()
        cur.execute("SELECT employee_key, employee_name, role_title FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)")
        emps = cur.fetchall()
    urls = _signed([r["photo_in_key"] for r in rows] + [r["photo_out_key"] for r in rows])
    marked = [_marked_row(r, rules, urls) for r in rows]
    seen = {r["employee_key"] for r in rows}
    off = rules.is_off(today)
    absent = [] if off else [{"name": e["employee_name"], "role": e["role_title"] or ""} for e in emps if e["employee_key"] not in seen]
    clock = lambda m: _to12h(f"{m // 60:02d}:{m % 60:02d}").lstrip("0").replace(":00 ", " ")  # noqa: E731
    return {"status": "ok", "date": _dmy(today), "isWeeklyOff": off, "marked": marked, "absent": absent, "totalEmployees": len(emps),
            "rules": {"absentBefore": clock(rules.absent_before), "halfBefore": clock(rules.half_before), "fullUntil": clock(rules.full_until),
                      "lateAfter": clock(rules.late_after)}}


def manual_checkout(user, name, checkout_time):
    rules, now = Rules(), _now()
    with db.cursor() as cur:
        emp = _find_employee(cur, name)
        if not emp:
            return {"status": "error", "msg": "Aaj ka check-in nahi mila"}
        r = _today_row(cur, emp["employee_key"], now.date())
        if not r:
            return {"status": "error", "msg": "Aaj ka check-in nahi mila"}
        if r["check_out_time"] is not None:
            return {"status": "already_checkout", "time": _hhmm(r["check_out_time"])}
        out_text = str(checkout_time or "").strip() or now.strftime("%H:%M")
        try:
            oh, om = (int(x) for x in out_text.split(":")[:2])
            out_t = time(oh, om)
        except (ValueError, TypeError):
            return {"status": "error", "msg": "Checkout time is not valid."}
        in_min, out_min = _mins(r["check_in_time"]), oh * 60 + om
        hours_text, hours_min = _hours(in_min, out_min)
        status = rules.final_status(in_min, out_min)
        cur.execute("UPDATE fact_attendance SET check_out_time = %s, checkout_source = 'Manual (Owner)', hours_worked_min = %s, final_status = %s, "
                    "wa_status = %s, updated_at = now() WHERE attendance_key = %s",
                    (out_t, hours_min, status, WA["SKIPPED"] if emp["skip_whatsapp"] else _next_wa_after_checkout(r["wa_status"]), r["attendance_key"]))
        key = r["attendance_key"]
        cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) VALUES (%s, %s, 'attendance.manual_checkout', %s, %s)",
                    (user["user_key"], user["username"], emp["employee_name"], Json({"time": out_text, "status": status})))
    wa = process_wa_row(key)
    return {"status": "ok", "hoursWorked": hours_text, "time": out_text, "finalStatus": status, "waSent": wa["status"] == "sent"}


# --- salary
def set_employee_salary(user, name, salary):
    try:
        amount = float(salary)
    except (TypeError, ValueError):
        amount = 0
    if not name or amount <= 0:
        return {"status": "error", "msg": "Valid name and salary required"}
    with db.cursor() as cur:
        emp = _need_emp(cur, name)
        had = emp["monthly_salary"] is not None
        cur.execute("UPDATE dim_att_employee SET monthly_salary = %s, salary_set_at = now(), salary_set_by_user_key = %s WHERE employee_key = %s",
                    (round(amount, 2), user["user_key"], emp["employee_key"]))
    return {"status": "ok", "action": "updated" if had else "set", "name": emp["employee_name"], "salary": amount}


def get_all_salaries(user):
    with db.cursor() as cur:
        cur.execute("SELECT e.employee_name, e.monthly_salary, e.salary_set_at, u.display_name FROM dim_att_employee e "
                    "LEFT JOIN dim_user u ON u.user_key = e.salary_set_by_user_key WHERE e.monthly_salary IS NOT NULL ORDER BY lower(e.employee_name)")
        return {"status": "ok", "salaries": [{"name": r["employee_name"], "salary": float(r["monthly_salary"]),
                                              "setOn": r["salary_set_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M") if r["salary_set_at"] else "",
                                              "setBy": r["display_name"] or "Owner"} for r in cur.fetchall()]}


def get_salary_report(user, name, year, month, override=None):
    rules = Rules()
    try:
        year, month = int(year), int(month)
        if not (1 <= month <= 12):
            raise ValueError
    except (TypeError, ValueError):
        return {"status": "error", "msg": "Year and month are not valid."}
    with db.cursor() as cur:
        emp = _find_employee(cur, name, active_only=False)
        if not emp:
            return {"status": "error", "msg": "Employee not found"}
        try:
            monthly = float(override or 0)
        except (TypeError, ValueError):
            monthly = 0
        if monthly <= 0:
            if emp["monthly_salary"] and float(emp["monthly_salary"]) > 0:
                monthly = float(emp["monthly_salary"])
            else:
                return {"status": "error", "msg": "Salary not set. Please set salary from Salary tab first."}
        recs = _month_records(cur, emp["employee_key"], year, month)
        advances = _advances_for(cur, emp["employee_key"], year, month)
    days_in_month = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    per_day = monthly / rules.base_days
    today = mc.today_ist()
    arr = []
    for d in range(1, days_in_month + 1):
        dt = date(year, month, d)
        dow = rules.js_dow(dt)
        rec = recs.get(d)
        st, t_in, t_out, hrs = "Absent", "", "—", "—"
        if dow == rules.off_day:
            st = "Weekly Off"
        elif dt > today:
            st = "Future"
        elif rec:
            st = rules.resolve(rec["final_status"], _mins(rec["check_in_time"]))
            t_in = _hhmm(rec["check_in_time"])
            t_out = _hhmm(rec["check_out_time"]) if rec["check_out_time"] else "—"
            hrs = _hours_text(rec["hours_worked_min"]) if rec["check_out_time"] else "—"
        arr.append({"day": d, "dow": dow, "status": st, "time": t_in, "checkoutTime": t_out, "hoursWorked": hrs, "sandwichApplied": False})
    for i, day in enumerate(arr):   # sandwich rule: a weekly off between two absent days is also an absent day
        if day["dow"] == rules.off_day and day["status"] == "Weekly Off" and 0 <= i - 1 and i + 1 < len(arr):
            if arr[i - 1]["status"] in ("Absent", "Incomplete") and arr[i + 1]["status"] in ("Absent", "Incomplete"):
                day["status"], day["sandwichApplied"] = "Absent (Sandwich)", True
    stats = {"present": 0, "late": 0, "halfDay": 0, "absent": 0, "sandwich": 0, "incomplete": 0, "weeklyOff": 0, "future": 0}
    ded = 0.0
    for day in arr:
        s = day["status"]
        if s == "Present":
            stats["present"] += 1
        elif s == "Late":
            stats["late"] += 1
        elif s == "Half Day":
            stats["halfDay"] += 1
            ded += 0.5
        elif s == "Absent":
            stats["absent"] += 1
            ded += 1
        elif s == "Absent (Sandwich)":
            stats["absent"] += 1
            stats["sandwich"] += 1
            ded += 1
        elif s == "Incomplete":
            stats["incomplete"] += 1
            ded += 1
        elif s == "Weekly Off":
            stats["weeklyOff"] += 1
        elif s == "Future":
            stats["future"] += 1
    total_adv = sum(a["amount"] for a in advances)
    net = max(0, monthly - ded * per_day - total_adv)
    return {"status": "ok", "employeeName": emp["employee_name"], "year": year, "month": month, "monthlySalary": monthly,
            "perDaySalary": round(per_day * 100) / 100, "baseDays": rules.base_days, "totalDeductionDays": ded,
            "deductionAmount": _round(ded * per_day), "totalAdvance": total_adv, "advances": advances, "netSalary": _round(net),
            "totalPaidDays": round(max(0, rules.base_days - ded) * 10) / 10, "stats": stats, "days": arr}


# --- advances
def add_advance(user, name, amount, note):
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0
    if not name or amt <= 0:
        return {"status": "error", "msg": "Valid name and amount required"}
    with db.cursor() as cur:
        emp = _find_employee(cur, name, active_only=False)
        if not emp:
            return {"status": "error", "msg": "Employee not found. Pick the name from the list."}
        cur.execute("INSERT INTO fact_att_advance (employee_key, advance_date, amount, note, added_by_user_key) VALUES (%s, %s, %s, %s, %s) RETURNING created_at",
                    (emp["employee_key"], mc.today_ist(), round(amt, 2), (note or "").strip() or None, user["user_key"]))
        created = cur.fetchone()["created_at"]
    return {"status": "ok", "name": emp["employee_name"], "amount": amt, "date": created.astimezone(mc.IST).strftime("%d-%m-%Y %H:%M")}


def get_all_advances(user, name=None):
    with db.cursor() as cur:
        cur.execute("SELECT a.advance_key, a.created_at, a.amount, a.note, e.employee_name, u.display_name FROM fact_att_advance a "
                    "JOIN dim_att_employee e ON e.employee_key = a.employee_key LEFT JOIN dim_user u ON u.user_key = a.added_by_user_key "
                    "WHERE (%s::text IS NULL OR lower(e.employee_name) LIKE '%%' || lower(%s) || '%%') ORDER BY a.advance_key DESC",
                    (str(name).strip() or None if name else None,) * 2)
        return {"status": "ok", "advances": [{"id": r["advance_key"], "date": r["created_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M"),
                                              "name": r["employee_name"], "amount": float(r["amount"]), "note": r["note"] or "",
                                              "addedBy": r["display_name"] or "Owner"} for r in cur.fetchall()]}


def delete_advance(user, advance_id):
    with db.cursor() as cur:
        cur.execute("DELETE FROM fact_att_advance WHERE advance_key = %s RETURNING advance_key", (int(advance_id or 0),))
        if not cur.fetchone():
            return {"status": "error", "msg": "Invalid row"}
    return {"status": "ok"}


# --- employees and PINs
def get_all_employees(user):
    sync_members()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM dim_att_employee ORDER BY lower(employee_name)")
        emps = cur.fetchall()
        cur.execute("SELECT el.employee_key, l.location_name FROM att_employee_location el "
                    "JOIN dim_att_location l ON l.location_key = el.location_key ORDER BY l.sort_order, l.location_key")
        places: dict[int, list[str]] = {}
        for r in cur.fetchall():
            places.setdefault(r["employee_key"], []).append(r["location_name"])
        return {"status": "ok", "employees": [{"name": r["employee_name"], "role": r["role_title"] or "", "active": r["is_active"],
                                               "addedOn": _dmy(r["added_on"]), "places": places.get(r["employee_key"], [])} for r in emps]}


def get_work_places(user):
    with db.cursor() as cur:
        cur.execute("SELECT location_name, icon FROM dim_att_location ORDER BY sort_order, location_key")
        return {"status": "ok", "places": [{"name": r["location_name"], "icon": r["icon"]} for r in cur.fetchall()]}


def set_employee_places(user, name, places):
    """Tag the work places an employee may mark attendance from. An empty list = any work place."""
    wanted = [str(p).strip().lower() for p in (places or []) if str(p).strip()]
    with db.cursor() as cur:
        emp = _need_emp(cur, name)
        cur.execute("SELECT location_key, location_name FROM dim_att_location")
        known = {r["location_name"].strip().lower(): r for r in cur.fetchall()}
        missing = [p for p in wanted if p not in known]
        if missing:
            return {"status": "error", "msg": "Unknown work place: " + ", ".join(missing)}
        keys = sorted({known[p]["location_key"] for p in wanted})
        cur.execute("DELETE FROM att_employee_location WHERE employee_key = %s", (emp["employee_key"],))
        for k in keys:
            cur.execute("INSERT INTO att_employee_location (employee_key, location_key) VALUES (%s, %s)", (emp["employee_key"], k))
        cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) VALUES (%s, %s, 'attendance.set_places', %s, %s)",
                    (user["user_key"], user["username"], emp["employee_name"], Json({"places": [known[p]["location_name"] for p in sorted(set(wanted))]})))
    return {"status": "ok", "name": emp["employee_name"], "count": len(keys)}


def add_employee(user, name, role):
    clean, role = " ".join(str(name or "").split()), str(role or "").strip()
    if not clean:
        return {"status": "error", "msg": "Name is required"}
    with db.cursor() as cur:
        ex = _find_employee(cur, clean, active_only=False)
        if ex:
            if not ex["is_active"]:
                cur.execute("UPDATE dim_att_employee SET is_active = true, role_title = COALESCE(NULLIF(%s, ''), role_title) WHERE employee_key = %s",
                            (role, ex["employee_key"]))
                return {"status": "reactivated", "name": ex["employee_name"]}
            return {"status": "error", "msg": "Employee already exists"}
        cur.execute("INSERT INTO dim_att_employee (employee_name, role_title) VALUES (%s, %s)", (clean[:100], role[:100] or None))
    return {"status": "ok", "name": clean}


def delete_employee(user, name):
    with db.cursor() as cur:
        cur.execute("UPDATE dim_att_employee SET is_active = false WHERE lower(btrim(employee_name)) = lower(btrim(%s)) RETURNING employee_key", (str(name or ""),))
        if not cur.fetchone():
            return {"status": "error", "msg": "Employee not found"}
    return {"status": "ok"}


def set_employee_pin(user, name, pin):
    pin = str(pin or "").strip()
    if not name or not pin:
        return {"status": "error", "msg": "Name aur PIN dono required hain"}
    if len(pin) < 4 or not pin.isdigit():
        return {"status": "error", "msg": "PIN kam se kam 4 digits ka hona chahiye (sirf numbers)"}
    with db.cursor() as cur:
        emp = _need_emp(cur, name)
        cur.execute("UPDATE dim_att_employee SET pin_hash = %s, pin_set_at = now(), pin_failed_count = 0, pin_locked_until = NULL WHERE employee_key = %s",
                    (auth.hash_password(pin), emp["employee_key"]))
        cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target) VALUES (%s, %s, 'attendance.set_pin', %s)",
                    (user["user_key"], user["username"], emp["employee_name"]))
    return {"status": "ok", "action": "updated" if emp["pin_hash"] else "set", "name": emp["employee_name"]}


def get_all_pins(user):
    with db.cursor() as cur:
        cur.execute("SELECT employee_name, pin_set_at FROM dim_att_employee WHERE pin_hash IS NOT NULL ORDER BY lower(employee_name)")
        # the PIN itself is never readable again; the page only needs to know who has one
        return {"status": "ok", "pins": [{"name": r["employee_name"], "pin": "••••",
                                          "setOn": r["pin_set_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M") if r["pin_set_at"] else ""}
                                         for r in cur.fetchall()]}


# --- reports
def _fmt_month_days(year: int, month: int) -> int:
    return (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day


def get_monthly_report(user, name, year, month):
    rules = Rules()
    try:
        year, month = int(year), int(month)
        n = _fmt_month_days(year, month)
    except (TypeError, ValueError):
        return {"status": "error", "msg": "Year and month are not valid."}
    with db.cursor() as cur:
        emp = _find_employee(cur, name, active_only=False)
        recs = _month_records(cur, emp["employee_key"], year, month) if emp else {}
    today, days = mc.today_ist(), []
    stats = {"present": 0, "late": 0, "halfDay": 0, "absent": 0, "weeklyOff": 0, "incomplete": 0}
    for d in range(1, n + 1):
        dt = date(year, month, d)
        dow, rec = rules.js_dow(dt), recs.get(d)
        status, t_in, t_out, hrs = "", "", "", "—"
        if rec:
            status = rules.resolve(rec["final_status"], _mins(rec["check_in_time"]))
            t_in = _hhmm(rec["check_in_time"])
            t_out = _hhmm(rec["check_out_time"]) if rec["check_out_time"] else "—"
            hrs = _hours_text(rec["hours_worked_min"]) if rec["check_out_time"] else "—"
            key = {"Present": "present", "Late": "late", "Half Day": "halfDay", "Absent": "absent", "Incomplete": "incomplete"}.get(status)
            if key:
                stats[key] += 1
        elif dow == rules.off_day:
            status = "Weekly Off"
            stats["weeklyOff"] += 1
        elif dt > today:
            status = "—"
        else:
            status = "Absent"
            stats["absent"] += 1
        days.append({"day": d, "dayName": JS_DAYS[dow], "status": status, "time": t_in, "checkoutTime": t_out, "hoursWorked": hrs})
    stats["totalWorkingDays"] = n - stats["weeklyOff"]
    stats["paidDays"] = stats["present"] + stats["late"] + stats["halfDay"] * 0.5
    return {"status": "ok", "days": days, "stats": stats}


def _working_days_back(rules: Rules, count: int, limit: int) -> list[date]:
    out, d, scanned = [], mc.today_ist(), 0
    while len(out) < count and scanned < limit:
        d -= timedelta(days=1)
        scanned += 1
        if not rules.is_off(d):
            out.append(d)
    return out


def get_absent_streak(user, days):
    rules = Rules()
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 0
    if days < 1:
        return {"status": "error", "msg": "Invalid days"}
    wd = _working_days_back(rules, days, days * 3)
    if not wd:
        return {"status": "ok", "days": days, "workingDays": 0, "employees": []}
    with db.cursor() as cur:
        cur.execute("SELECT employee_key, employee_name, role_title FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)")
        emps = cur.fetchall()
        cur.execute("SELECT employee_key, work_date, check_in_time, final_status FROM fact_attendance WHERE work_date = ANY(%s)", (wd,))
        present = {(r["employee_key"], r["work_date"]) for r in cur.fetchall()
                   if rules.resolve(r["final_status"], _mins(r["check_in_time"])) != "Absent"}
    out = [{"name": e["employee_name"], "role": e["role_title"] or ""} for e in emps
           if not any((e["employee_key"], d) in present for d in wd)]
    return {"status": "ok", "days": days, "workingDays": len(wd), "fromDate": _dmy(wd[-1]), "toDate": _dmy(wd[0]), "employees": out}


def get_employee_streak(user, name):
    rules = Rules()
    if not name:
        return {"status": "error", "msg": "Name required"}
    wd = _working_days_back(rules, 30, 90)
    with db.cursor() as cur:
        emp = _find_employee(cur, name, active_only=False)
        cur.execute("SELECT work_date, check_in_time, check_out_time, hours_worked_min, final_status FROM fact_attendance WHERE employee_key = %s "
                    "AND work_date = ANY(%s)", (emp["employee_key"] if emp else 0, wd))
        att = {r["work_date"]: r for r in cur.fetchall()}
    out, streak, max_streak = [], 0, 0
    tot = {"Absent": 0, "Present": 0, "Late": 0, "Half Day": 0}
    for d in wd:
        rec = att.get(d)
        if rec:
            status = rules.resolve(rec["final_status"], _mins(rec["check_in_time"]))
            t_in, t_out = _hhmm(rec["check_in_time"]), _hhmm(rec["check_out_time"]) if rec["check_out_time"] else "—"
            hrs = _hours_text(rec["hours_worked_min"]) if rec["check_out_time"] else "—"
        else:
            status, t_in, t_out, hrs = "Absent", "", "", "—"
        if status in tot:
            tot[status] += 1
        streak = streak + 1 if status == "Absent" else 0
        max_streak = max(max_streak, streak)
        out.append({"date": _dmy(d), "status": status, "time": t_in, "checkoutTime": t_out, "hoursWorked": hrs})
    current = 0
    for d in out:
        if d["status"] in ("Absent", "Incomplete"):
            current += 1
        else:
            break
    return {"status": "ok", "employeeName": name, "days": out, "currentStreak": current, "maxStreak": max_streak, "totalAbsent": tot["Absent"],
            "totalPresent": tot["Present"], "totalLate": tot["Late"], "totalHalfDay": tot["Half Day"], "workingDays": len(wd)}


# (name, function, takes the old password argument first)
OWNER = {
    "verifyOwnerPassword": lambda u, a: {"ok": True},
    "getEmployees": lambda u, a: get_employees(),
    "getTodayAttendance": lambda u, a: get_today_attendance(u),
    "manualCheckout": lambda u, a: manual_checkout(u, a[1], a[2] if len(a) > 2 else None),
    "getAllSalaries": lambda u, a: get_all_salaries(u),
    "setEmployeeSalary": lambda u, a: set_employee_salary(u, a[1], a[2]),
    "getAllAdvances": lambda u, a: get_all_advances(u, a[1] if len(a) > 1 else None),
    "addAdvancePayment": lambda u, a: add_advance(u, a[1], a[2], a[3] if len(a) > 3 else ""),
    "deleteAdvance": lambda u, a: delete_advance(u, a[1]),
    "getSalaryReport": lambda u, a: get_salary_report(u, a[1], a[2], a[3], a[4] if len(a) > 4 else None),
    "getMonthlyReport": lambda u, a: get_monthly_report(u, a[1], a[2], a[3]),
    "getAbsentStreak": lambda u, a: get_absent_streak(u, a[1]),
    "getEmployeeStreak": lambda u, a: get_employee_streak(u, a[1]),
    "getAllEmployees": lambda u, a: get_all_employees(u),
    "getWorkPlaces": lambda u, a: get_work_places(u),
    "setEmployeePlaces": lambda u, a: set_employee_places(u, a[1], a[2] if len(a) > 2 else []),
    "addEmployee": lambda u, a: add_employee(u, a[1], a[2] if len(a) > 2 else ""),
    "deleteEmployee": lambda u, a: delete_employee(u, a[1]),
    "getAllPINs": lambda u, a: get_all_pins(u),
    "setEmployeePIN": lambda u, a: set_employee_pin(u, a[1], a[2]),
}


@router.post("/owner/rpc")
def owner_rpc(body: RpcIn, user=Depends(OWNER_DEP)):
    if body.action in ("manualCheckout", "setEmployeeSalary", "addAdvancePayment", "deleteAdvance", "addEmployee", "deleteEmployee", "setEmployeePIN", "setEmployeePlaces"):
        access.require_edit(user, OWNER_PAGE)
    fn = OWNER.get(body.action)
    if not fn:
        raise HTTPException(404, "Unknown action")
    try:
        return fn(user, body.args)
    except HTTPException as e:
        return {"status": "error", "msg": e.detail}
    except IndexError:
        return {"status": "error", "msg": "Missing information."}


@router.get("/owner/me")
def owner_me(user=Depends(OWNER_DEP)):
    return {"ok": True, "name": user["display_name"]}


# ------------------------------------------------------------------ the 8 PM auto check-out and the five-minute WhatsApp retry
def auto_checkout(now: datetime | None = None) -> int:
    """Everyone who checked in today and never checked out is checked out at the auto time (default 8 PM). One summary message."""
    rules, now = Rules(), now or _now()
    day = now.date()
    done: list[dict] = []
    with db.cursor() as cur:
        cur.execute(ROW_SQL + " WHERE a.work_date = %s AND a.check_out_time IS NULL ORDER BY a.check_in_time FOR UPDATE OF a", (day,))
        for r in cur.fetchall():
            in_min = _mins(r["check_in_time"])
            status = rules.final_status(in_min, rules.auto_out)
            _, hmin = _hours(in_min, rules.auto_out)
            cur.execute("UPDATE fact_attendance SET check_out_time = %s, checkout_source = 'Auto (8 PM)', hours_worked_min = %s, final_status = %s, "
                        "wa_status = %s, updated_at = now() WHERE attendance_key = %s",
                        (time(rules.auto_out // 60, rules.auto_out % 60), hmin, status,
                         WA["SKIPPED"] if r["skip_whatsapp"] else WA["AUTO_DONE"], r["attendance_key"]))
            if not r["skip_whatsapp"]:
                done.append({"name": r["employee_name"], "in": _hhmm(r["check_in_time"]), "status": status})
    if done:
        lines = "\n".join(f"{i}. *{d['name']}* — In {_to12h(d['in'])} → {d['status']}" for i, d in enumerate(done, 1))
        _send("att_auto_summary", {"auto_time": _to12h(rules.auto_out_text), "list": lines, "date": day.strftime("%d %b %Y")},
              "AUTO-SUMMARY", f"{len(done)} employees")
    return len(done)


def run_jobs(now: datetime | None = None) -> dict:
    """Called every minute by the background thread."""
    rules, now = Rules(), now or _now()
    out = {"retried": None, "auto": None}
    if now.minute % 5 == 0:
        out["retried"] = process_pending_whatsapp()
    if now.hour * 60 + now.minute >= rules.auto_run_at:
        with db.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (AUTO_RUN_LOCK,))
            if cur.fetchone()["ok"]:
                cur.execute("SELECT 1 FROM att_auto_run WHERE run_date = %s", (now.date(),))
                if not cur.fetchone():
                    process_pending_whatsapp()
                    n = auto_checkout(now)
                    cur.execute("INSERT INTO att_auto_run (run_date, closed) VALUES (%s, %s) ON CONFLICT DO NOTHING", (now.date(), n))
                    cur.execute("DELETE FROM att_wa_log WHERE log_key < (SELECT COALESCE(max(log_key), 0) - 3000 FROM att_wa_log)")
                    out["auto"] = n
    return out


def start_scheduler():
    if os.environ.get("VT_SCHEDULER", "on").lower() == "off":
        return None
    s = daily_reports.Scheduler(run_jobs, "attendance-jobs")
    s.start()
    return s
