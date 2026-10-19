"""Checklist, inside the Delegation module: routine and compliance work that comes back on a schedule (GST returns, licence
renewals, the daily shop-opening round ...). Three screens - a compliance calendar, a form to create and manage checklists, and
"My checklist" where the person it is assigned to marks each occurrence Done or Not Done.

How it works:
  - a checklist (dim_checklist) says what, how often and who; each due date becomes one row in fact_checklist_occurrence, created
    as the calendar or list is looked at (never twice: unique on checklist + date) - so there is no background job to go wrong;
  - people are the same people as everywhere else (dim_user); who sees what is page access (Setup > Access): chk_mine (my
    checklist), chk_calendar (the calendar) and chk_manage (create, assign, see everyone's). Switching a page off switches that
    part of the module off;
  - the categories are a master list (Setup > Dropdown values), the weekly-off day that Daily checklists skip is the Attendance
    setting, and checklists can be bulk uploaded from Setup > Import.
"""
import calendar
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict

from . import access, auth, db, modcommon as mc, notify, roles
from .admin import audit

router = APIRouter(prefix="/checklist", tags=["checklist"])
PAGES = ("chk_mine", "chk_calendar", "chk_manage")
ANY = auth.require_roles(*roles.ALL_ROLES)
FREQUENCIES = ("Daily", "Weekly", "Monthly", "Quarterly", "Yearly", "Once")
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
LOOK_AHEAD_DAYS = 45      # how far ahead "My checklist" shows
LOOK_BACK_DAYS = 90       # how far back an unseen checklist is caught up (older gaps are not invented)
MAX_CALENDAR_ITEMS = 2000


# ------------------------------------------------------------------ the schedule (pure date arithmetic)
def js_dow(d: date) -> int:
    """0 = Sunday ... 6 = Saturday - the numbering the weekly-off setting uses."""
    return (d.weekday() + 1) % 7


def _clamp(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def due_dates(frequency: str, start: date, end: date | None, lo: date, hi: date, *, dow: int | None = None,
              day: int | None = None, month: int | None = None, off_dow: int | None = None) -> list[date]:
    """Every due date of one checklist that falls between lo and hi (both included)."""
    first, last = max(start, lo), min(end, hi) if end else hi
    if first > last:
        return []
    if frequency == "Once":
        return [start] if first <= start <= last else []
    out: list[date] = []
    if frequency in ("Daily", "Weekly"):
        d = first
        while d <= last:
            if (frequency == "Daily" and js_dow(d) != off_dow) or (frequency == "Weekly" and js_dow(d) == dow):
                out.append(d)
            d += timedelta(days=1)
        return out
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        hit = (frequency == "Monthly"
               or (frequency == "Quarterly" and month is not None and (m - month) % 3 == 0)
               or (frequency == "Yearly" and m == month))
        if hit and day:
            d = _clamp(y, m, day)
            if first <= d <= last:
                out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _off_day() -> int:
    return mc.int_setting("att_weekly_off_day", 1)


def _materialize(cur, lo: date, hi: date, assignee_key: int | None = None, checklist_key: int | None = None):
    """Create the missing occurrence rows for the active checklists between lo and hi."""
    cur.execute("""SELECT c.checklist_key, c.assignee_user_key, c.frequency, c.due_dow, c.due_day, c.due_month,
                          sd.full_date AS start_date, ed.full_date AS end_date
                   FROM dim_checklist c JOIN dim_date sd ON sd.date_key = c.start_date_key
                   LEFT JOIN dim_date ed ON ed.date_key = c.end_date_key
                   WHERE c.is_active AND (%s::int IS NULL OR c.assignee_user_key = %s)
                     AND (%s::int IS NULL OR c.checklist_key = %s)""", (assignee_key, assignee_key, checklist_key, checklist_key))
    off = _off_day()
    for c in cur.fetchall():
        keys = [mc.date_key(d) for d in due_dates(c["frequency"], c["start_date"], c["end_date"], lo, hi, dow=c["due_dow"],
                                                  day=c["due_day"], month=c["due_month"], off_dow=off)]
        if keys:
            # joined to the calendar table so a date it does not hold is skipped rather than failing the whole request
            cur.execute("INSERT INTO fact_checklist_occurrence (checklist_key, due_date_key, assignee_user_key) "
                        "SELECT %s, d.date_key, %s FROM dim_date d WHERE d.date_key = ANY(%s) "
                        "ON CONFLICT (checklist_key, due_date_key) DO NOTHING", (c["checklist_key"], c["assignee_user_key"], keys))


# ------------------------------------------------------------------ who is who
def _who(user) -> str:
    if user["role"] == "admin":
        return "Admin"
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    held = access.effective_access(user)
    if "chk_manage" in held:
        return "Manager"
    if set(PAGES) & set(held):
        return "Staff"
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _has(user, page: str) -> bool:
    return user["role"] == "admin" or page in access.effective_access(user)


def _need_page(user, page: str):
    _who(user)
    if not _has(user, page):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _need_manager(user):
    _need_page(user, "chk_manage")
    access.require_edit(user, "chk_manage")


def assignable_users(cur) -> list[dict]:
    """People a checklist can be given to: everyone who holds My checklist, plus admins."""
    keys = sorted(access.users_with_page("chk_mine"))
    cur.execute("SELECT user_key, username, display_name FROM dim_user WHERE (user_key = ANY(%s) OR role = 'admin') AND password_hash <> %s "
                "ORDER BY lower(display_name)", (keys, auth.DISABLED_HASH))
    return [{"id": r["username"], "name": r["display_name"], "user_key": r["user_key"]} for r in cur.fetchall()]


# ------------------------------------------------------------------ reading
ITEM_SQL = """
SELECT o.occurrence_key, o.checklist_key, dd.full_date AS due_date, o.status, o.remark, o.marked_at, c.title, c.description,
       c.frequency, cat.category_name, u.user_key AS assignee_key, u.username AS assignee_id, u.display_name AS assignee_name,
       mu.display_name AS marked_by
FROM fact_checklist_occurrence o
JOIN dim_checklist c ON c.checklist_key = o.checklist_key
JOIN dim_date dd ON dd.date_key = o.due_date_key
JOIN dim_user u ON u.user_key = o.assignee_user_key
LEFT JOIN dim_checklist_category cat ON cat.category_key = c.category_key
LEFT JOIN dim_user mu ON mu.user_key = o.marked_by_user_key
"""


def _item(r: dict, today: date) -> dict:
    return {"key": r["occurrence_key"], "checklistKey": r["checklist_key"], "title": r["title"], "description": r["description"] or "",
            "category": r["category_name"] or "", "frequency": r["frequency"], "due": r["due_date"].isoformat(), "status": r["status"],
            "overdue": r["status"] == "Pending" and r["due_date"] < today, "remark": r["remark"] or "",
            "markedAt": mc.iso(r["marked_at"]), "markedBy": r["marked_by"] or "",
            "assigneeId": r["assignee_id"], "assignee": r["assignee_name"]}


@router.get("/me")
def me(user=Depends(ANY)):
    who = _who(user)
    held = access.effective_access(user)
    pages = sorted(p for p in PAGES if user["role"] == "admin" or p in held)
    with db.cursor() as cur:
        cur.execute("SELECT category_key, category_name FROM dim_checklist_category ORDER BY sort_order, lower(category_name)")
        cats = [r["category_name"] for r in cur.fetchall()]
        staff = assignable_users(cur) if "chk_manage" in pages else []
    return {"success": True, "id": user["username"], "name": user["display_name"], "role": who, "pages": pages,
            "canWrite": user["role"] == "admin" or not access.is_view_only(user, "chk_manage"),
            "today": mc.today_ist().isoformat(), "categories": cats, "staff": staff,
            "frequencies": list(FREQUENCIES), "weeklyOffDay": _off_day()}


@router.get("/mine")
def mine(user=Depends(ANY)):
    """My checklist: what is overdue, due today, coming up, and what I finished lately."""
    _need_page(user, "chk_mine")
    today = mc.today_ist()
    lo, hi = today - timedelta(days=LOOK_BACK_DAYS), today + timedelta(days=LOOK_AHEAD_DAYS)
    with db.cursor() as cur:
        _materialize(cur, lo, hi, assignee_key=user["user_key"])
        cur.execute(ITEM_SQL + " WHERE o.assignee_user_key = %s AND dd.full_date BETWEEN %s AND %s AND (c.is_active OR o.status <> 'Pending') "
                    "ORDER BY dd.full_date, lower(c.title)", (user["user_key"], lo, hi))
        rows = [_item(r, today) for r in cur.fetchall()]
    recent = today - timedelta(days=14)
    return {"today": today.isoformat(),
            "overdue": [r for r in rows if r["overdue"]],
            "dueToday": [r for r in rows if r["status"] == "Pending" and r["due"] == today.isoformat()],
            "upcoming": [r for r in rows if r["status"] == "Pending" and r["due"] > today.isoformat()],
            "finished": [r for r in reversed(rows) if r["status"] != "Pending" and r["due"] >= recent.isoformat()]}


class MarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    remark: str = ""


@router.post("/occurrences/{occurrence_key}/mark")
def mark(occurrence_key: int, body: MarkIn, user=Depends(ANY)):
    who = _who(user)
    if body.status not in ("Done", "Not Done", "Pending"):
        raise HTTPException(422, "Status must be Done or Not Done.")
    remark = " ".join(body.remark.split())[:500]
    today = mc.today_ist()
    with db.cursor() as cur:
        cur.execute("SELECT o.occurrence_key, o.assignee_user_key, o.status, dd.full_date AS due_date FROM fact_checklist_occurrence o "
                    "JOIN dim_date dd ON dd.date_key = o.due_date_key WHERE o.occurrence_key = %s FOR UPDATE OF o", (occurrence_key,))
        o = cur.fetchone()
        if not o:
            raise HTTPException(404, "Checklist item not found.")
        manager = who in ("Admin", "Manager") and _has(user, "chk_manage")
        if o["assignee_user_key"] != user["user_key"] and not manager:
            raise HTTPException(403, "This checklist item belongs to someone else.")
        if o["assignee_user_key"] == user["user_key"]:
            if not _has(user, "chk_mine"):
                raise HTTPException(403, access.NO_ACCESS_MESSAGE)
            access.require_edit(user, "chk_mine")
        else:
            access.require_edit(user, "chk_manage")
        if body.status == "Pending":
            if not manager:
                raise HTTPException(403, "Only a manager can re-open a finished item.")
        elif o["status"] != "Pending" and not manager:
            raise HTTPException(403, "This item is already marked. Ask a manager to re-open it if it needs to change.")
        if body.status == "Not Done":
            if o["due_date"] > today:
                raise HTTPException(422, "It is not due yet - mark it Done, or wait until the due date.")
            if not remark:
                raise HTTPException(422, "Say why it was not done (a short remark).")
        if body.status == "Pending":
            cur.execute("UPDATE fact_checklist_occurrence SET status = 'Pending', marked_at = NULL, marked_by_user_key = NULL, remark = %s "
                        "WHERE occurrence_key = %s", (remark or None, occurrence_key))
        else:
            cur.execute("UPDATE fact_checklist_occurrence SET status = %s, marked_at = now(), marked_by_user_key = %s, remark = %s "
                        "WHERE occurrence_key = %s", (body.status, user["user_key"], remark or None, occurrence_key))
        info = None
        if body.status != "Pending":
            cur.execute("SELECT c.title, u.display_name AS who, u.user_key FROM fact_checklist_occurrence o JOIN dim_checklist c ON c.checklist_key = o.checklist_key "
                        "JOIN dim_user u ON u.user_key = o.assignee_user_key WHERE o.occurrence_key = %s", (occurrence_key,))
            info = cur.fetchone()
    if info:
        notify.fire("chk_done", {"name": info["who"], "title": info["title"], "due": f"{o['due_date']:%d-%m-%Y}", "status": body.status,
                                 "remark": remark or "-", "marked_by": user.get("display_name") or user["username"]}, assignee_user_key=info["user_key"],
                    actor_user_key=user["user_key"])
    return {"ok": True, "status": body.status}


@router.get("/calendar")
def calendar_month(year: int = Query(default=0), month: int = Query(default=0), assignee: str = Query(default=""),
                   category: str = Query(default=""), user=Depends(ANY)):
    """The compliance calendar for one month. A manager sees everyone's (and can filter); everyone else sees their own."""
    _need_page(user, "chk_calendar")
    today = mc.today_ist()
    year, month = year or today.year, month or today.month
    if not (1 <= month <= 12 and 2024 <= year <= 2035):
        raise HTTPException(422, "Choose a month between 2024 and 2035.")
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    wide = _has(user, "chk_manage")
    with db.cursor() as cur:
        who_key = user["user_key"]
        if wide and assignee:
            cur.execute("SELECT user_key FROM dim_user WHERE lower(username) = lower(%s)", (assignee,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Person not found.")
            who_key = row["user_key"]
        scope = who_key if (not wide or assignee) else None
        _materialize(cur, max(first, today - timedelta(days=LOOK_BACK_DAYS)), last, assignee_key=scope)
        cur.execute(ITEM_SQL + " WHERE dd.full_date BETWEEN %s AND %s AND (c.is_active OR o.status <> 'Pending') AND (%s::int IS NULL OR o.assignee_user_key = %s) "
                    "AND (%s = '' OR lower(cat.category_name) = lower(%s)) ORDER BY dd.full_date, lower(c.title) LIMIT %s",
                    (first, last, scope, scope, category, category, MAX_CALENDAR_ITEMS))
        items = [_item(r, today) for r in cur.fetchall()]
    days: dict[str, list] = {}
    for it in items:
        days.setdefault(it["due"], []).append(it)
    counts = {"total": len(items), "done": sum(i["status"] == "Done" for i in items),
              "notDone": sum(i["status"] == "Not Done" for i in items), "overdue": sum(i["overdue"] for i in items),
              "pending": sum(i["status"] == "Pending" and not i["overdue"] for i in items)}
    return {"year": year, "month": month, "monthName": MONTHS[month - 1], "firstDow": js_dow(first), "daysInMonth": last.day,
            "today": today.isoformat(), "weeklyOffDay": _off_day(), "days": days, "counts": counts, "scope": "all" if scope is None else "person"}


# ------------------------------------------------------------------ creating and managing
class ChecklistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    description: str = ""
    category: str = ""
    assignee: str                       # username
    frequency: str
    dueDow: int | None = None
    dueDay: int | None = None
    dueMonth: int | None = None
    startDate: str = ""
    endDate: str = ""


def check_schedule(frequency: str, dow, day, month):
    """The (dow, day, month) a frequency needs, or a plain 422 saying what is missing."""
    if frequency not in FREQUENCIES:
        raise HTTPException(422, "Choose how often: " + ", ".join(FREQUENCIES) + ".")
    if frequency == "Weekly":
        if dow is None or not 0 <= dow <= 6:
            raise HTTPException(422, "Choose the day of the week.")
        return dow, None, None
    if frequency in ("Monthly", "Quarterly", "Yearly"):
        if day is None or not 1 <= day <= 31:
            raise HTTPException(422, "Choose the day of the month (1 to 31; 31 means the last day).")
        if frequency == "Monthly":
            return None, day, None
        if month is None or not 1 <= month <= 12:
            raise HTTPException(422, "Choose the month" + (" the quarters start from." if frequency == "Quarterly" else "."))
        return None, day, month
    return None, None, None


def _category_key(cur, name: str) -> int | None:
    name = " ".join((name or "").split())
    if not name:
        return None
    key = mc.lookup_key(cur, "dim_checklist_category", "category_key", "category_name", name)
    if key is None:
        raise HTTPException(422, f"Category '{name}' is not in the list (Setup > Dropdown values > Checklist - categories).")
    return key


def _assignee(cur, username: str) -> dict:
    for u in assignable_users(cur):
        if u["id"].lower() == (username or "").strip().lower():
            return u
    raise HTTPException(422, "That person cannot be given checklists (they need the 'Checklist - My checklist' page in Setup > Access).")


def _values(cur, body: ChecklistIn, today: date, existing_start: date | None = None) -> dict:
    title = " ".join(body.title.split())
    if not title or len(title) > 200:
        raise HTTPException(422, "Title must be 1 to 200 characters.")
    dow, day, month = check_schedule(body.frequency, body.dueDow, body.dueDay, body.dueMonth)
    start = mc.parse_date(body.startDate, "Start date") or today
    if start < today and start != existing_start:
        raise HTTPException(422, "The start date cannot be in the past.")
    end = mc.parse_date(body.endDate, "End date")
    if end and end < start:
        raise HTTPException(422, "The end date is before the start date.")
    if body.frequency == "Once":
        end = None
    return {"title": title, "description": (body.description or "").strip()[:1000] or None, "category_key": _category_key(cur, body.category),
            "assignee": _assignee(cur, body.assignee), "frequency": body.frequency, "due_dow": dow, "due_day": day, "due_month": month,
            "start": start, "end": end}


@router.get("/checklists")
def list_checklists(user=Depends(ANY)):
    _need_page(user, "chk_manage")
    today = mc.today_ist()
    with db.cursor() as cur:
        cur.execute("""SELECT c.checklist_key, c.title, c.description, c.frequency, c.due_dow, c.due_day, c.due_month, c.is_active,
                              cat.category_name, u.username, u.display_name, sd.full_date AS start_date, ed.full_date AS end_date,
                              count(o.*) FILTER (WHERE o.status = 'Pending' AND od.full_date < %s) AS overdue,
                              count(o.*) FILTER (WHERE o.status = 'Done') AS done,
                              count(o.*) FILTER (WHERE o.status = 'Not Done') AS not_done
                       FROM dim_checklist c JOIN dim_user u ON u.user_key = c.assignee_user_key
                       JOIN dim_date sd ON sd.date_key = c.start_date_key LEFT JOIN dim_date ed ON ed.date_key = c.end_date_key
                       LEFT JOIN dim_checklist_category cat ON cat.category_key = c.category_key
                       LEFT JOIN fact_checklist_occurrence o ON o.checklist_key = c.checklist_key
                       LEFT JOIN dim_date od ON od.date_key = o.due_date_key
                       GROUP BY c.checklist_key, cat.category_name, u.username, u.display_name, sd.full_date, ed.full_date
                       ORDER BY c.is_active DESC, lower(c.title), c.checklist_key""", (today,))
        rows = cur.fetchall()
    return [{"key": r["checklist_key"], "title": r["title"], "description": r["description"] or "", "category": r["category_name"] or "",
             "assigneeId": r["username"], "assignee": r["display_name"], "frequency": r["frequency"], "dueDow": r["due_dow"],
             "dueDay": r["due_day"], "dueMonth": r["due_month"], "startDate": r["start_date"].isoformat(),
             "endDate": r["end_date"].isoformat() if r["end_date"] else "", "active": r["is_active"], "overdue": r["overdue"],
             "done": r["done"], "notDone": r["not_done"], "when": describe(r["frequency"], r["due_dow"], r["due_day"], r["due_month"])}
            for r in rows]


def describe(frequency: str, dow, day, month) -> str:
    if frequency == "Weekly" and dow is not None:
        return f"Every {DAYS[dow]}"
    ord_day = "the last day" if day == 31 else f"day {day}"
    if frequency == "Monthly":
        return f"Monthly, {ord_day}"
    if frequency == "Quarterly" and month:
        return f"Every 3 months from {MONTHS[month - 1]}, {ord_day}"
    if frequency == "Yearly" and month:
        return f"Yearly, {ord_day} of {MONTHS[month - 1]}"
    return {"Daily": "Every day (except the weekly off)", "Once": "One time"}.get(frequency, frequency)


def _drop_future_untouched(cur, checklist_key: int, today: date):
    cur.execute("DELETE FROM fact_checklist_occurrence o USING dim_date d WHERE o.due_date_key = d.date_key AND o.checklist_key = %s "
                "AND o.status = 'Pending' AND d.full_date >= %s", (checklist_key, today))


def _tell_assigned(v: dict, by_user: dict) -> None:
    """WhatsApp + the web bell for the person the checklist is for (and the EA / admin when Setup > WhatsApp says so)."""
    notify.fire("chk_assigned", {"name": v["assignee"]["name"], "title": v["title"], "when": describe(v["frequency"], v["due_dow"], v["due_day"], v["due_month"]),
                                 "assigned_by": by_user.get("display_name") or by_user["username"]}, assignee_user_key=v["assignee"]["user_key"],
                                 actor_user_key=by_user["user_key"])


@router.post("/checklists", status_code=201)
def create(body: ChecklistIn, user=Depends(ANY)):
    _need_manager(user)
    today = mc.today_ist()
    try:
        with db.cursor() as cur:
            v = _values(cur, body, today)
            cur.execute("""INSERT INTO dim_checklist (title, description, category_key, assignee_user_key, frequency, due_dow, due_day,
                                                      due_month, start_date_key, end_date_key, created_by_user_key)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING checklist_key""",
                        (v["title"], v["description"], v["category_key"], v["assignee"]["user_key"], v["frequency"], v["due_dow"], v["due_day"],
                         v["due_month"], mc.date_key(v["start"]), mc.date_key(v["end"]), user["user_key"]))
            key = cur.fetchone()["checklist_key"]
            audit(cur, user, "checklist.create", v["title"], {"assignee": v["assignee"]["id"], "frequency": v["frequency"]})
    except Exception as exc:
        raise mc.date_fk_error(exc)
    _tell_assigned(v, user)
    return {"key": key}


@router.put("/checklists/{checklist_key}")
def update(checklist_key: int, body: ChecklistIn, user=Depends(ANY)):
    _need_manager(user)
    today = mc.today_ist()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT sd.full_date AS start_date, c.assignee_user_key FROM dim_checklist c JOIN dim_date sd ON sd.date_key = c.start_date_key "
                        "WHERE c.checklist_key = %s FOR UPDATE OF c", (checklist_key,))
            old = cur.fetchone()
            if not old:
                raise HTTPException(404, "Checklist not found.")
            v = _values(cur, body, today, existing_start=old["start_date"])
            cur.execute("""UPDATE dim_checklist SET title = %s, description = %s, category_key = %s, assignee_user_key = %s, frequency = %s,
                                  due_dow = %s, due_day = %s, due_month = %s, start_date_key = %s, end_date_key = %s, updated_at = now()
                           WHERE checklist_key = %s""",
                        (v["title"], v["description"], v["category_key"], v["assignee"]["user_key"], v["frequency"], v["due_dow"], v["due_day"],
                         v["due_month"], mc.date_key(v["start"]), mc.date_key(v["end"]), checklist_key))
            if old["assignee_user_key"] != v["assignee"]["user_key"]:
                # a new person takes over: what is still open (overdue included) is theirs now; what was already marked stays with who marked it
                cur.execute("UPDATE fact_checklist_occurrence SET assignee_user_key = %s WHERE checklist_key = %s AND status = 'Pending'",
                            (v["assignee"]["user_key"], checklist_key))
            _drop_future_untouched(cur, checklist_key, today)   # the schedule or person may have changed: future rows are rebuilt
            audit(cur, user, "checklist.update", v["title"], {"assignee": v["assignee"]["id"], "frequency": v["frequency"]})
            handed_over = old["assignee_user_key"] != v["assignee"]["user_key"]
    except HTTPException:
        raise
    except Exception as exc:
        raise mc.date_fk_error(exc)
    if handed_over:
        _tell_assigned(v, user)
    return {"ok": True}


class ActiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


@router.post("/checklists/{checklist_key}/active")
def set_active(checklist_key: int, body: ActiveIn, user=Depends(ANY)):
    """Stop (or restart) a checklist. What was already done stays in the history."""
    _need_manager(user)
    with db.cursor() as cur:
        cur.execute("UPDATE dim_checklist SET is_active = %s, updated_at = now() WHERE checklist_key = %s RETURNING title",
                    (body.active, checklist_key))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Checklist not found.")
        if not body.active:
            _drop_future_untouched(cur, checklist_key, mc.today_ist())
        audit(cur, user, "checklist.stop" if not body.active else "checklist.restart", row["title"])
    return {"ok": True}


@router.delete("/checklists/{checklist_key}")
def delete(checklist_key: int, user=Depends(ANY)):
    """Remove a checklist that nobody has acted on yet (typed by mistake). Otherwise stop it - the history stays."""
    _need_manager(user)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM fact_checklist_occurrence WHERE checklist_key = %s AND status <> 'Pending'", (checklist_key,))
        if cur.fetchone()["n"]:
            raise HTTPException(409, "People have already marked items on this checklist, so it cannot be deleted. Stop it instead.")
        try:
            cur.execute("DELETE FROM dim_checklist WHERE checklist_key = %s RETURNING title", (checklist_key,))
        except pgerr.ForeignKeyViolation:
            raise HTTPException(409, "This checklist is in use. Stop it instead.")
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Checklist not found.")
        audit(cur, user, "checklist.delete", row["title"])
    return {"ok": True}
