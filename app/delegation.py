"""Delegation module: managers hand out dated tasks, staff mark them done (optionally with a photo), the manager approves,
sends back or rejects, and every staff member is scored and tracked week by week (Green / Yellow / Red, planned vs actual).

How it works:
  - staff are the same people as everywhere else (dim_user); "Admin / Manager / Staff" is worked out from the pages a person
    holds (Setup > Access);
  - the scoring numbers and the first day of the week are settings (Setup > Module settings), not constants in code;
  - revisions and deadline extensions are counted separately, so the weekly view never has to guess how many of each there were;
  - proof photos go to private storage with short-lived links.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, db, modcommon as mc, roles

router = APIRouter(prefix="/delegation", tags=["delegation"])
ANY = auth.require_roles(*roles.ALL_ROLES)
PAGES = ("deleg_mine", "deleg_week", "deleg_scoreboard", "deleg_manage")

TASK_SELECT = """
SELECT t.task_key, t.task_no, u.username AS staff_id, u.display_name AS staff_name, u.role AS staff_role,
       t.description, ad.full_date AS assigned_date, dd.full_date AS due_date, t.status, t.extra_work_notes, t.remark,
       t.score, t.revision_count, t.extension_count, t.staff_user_key
FROM fact_task t
JOIN dim_user u ON u.user_key = t.staff_user_key
JOIN dim_date ad ON ad.date_key = t.assigned_date_key
JOIN dim_date dd ON dd.date_key = t.due_date_key
"""


# ------------------------------------------------------------------ who is who
def _who(user) -> str:
    """Admin / Manager / Staff, from the pages the person holds."""
    if user["role"] == "admin":
        return "Admin"
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    held = access.effective_access(user)
    if "deleg_manage" in held:
        return "Manager"
    if set(PAGES) & set(held):
        return "Staff"
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _privileged(who: str) -> bool:
    return who in ("Admin", "Manager")


def _need_manager(user) -> str:
    who = _who(user)
    if not _privileged(who):
        raise HTTPException(403, "Only a manager or admin can do this.")
    access.require_edit(user, "deleg_manage")
    return who


def _need_page(user, page: str):
    held = access.effective_access(user)
    if user["role"] != "admin" and page not in held:
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _settings() -> dict:
    return {"on_time": mc.int_setting("deleg_on_time_score", 10), "late": mc.int_setting("deleg_late_score", -5),
            "revise": mc.int_setting("deleg_revise_penalty", -3), "extend": mc.int_setting("deleg_extend_penalty", -5),
            "week_start": mc.int_setting("deleg_week_start_dow", 6)}


def assignable_staff() -> list[dict]:
    """Everyone who works through Delegation and is not a manager/admin (the people tasks are handed to)."""
    mine, managers = access.users_with_page("deleg_mine"), access.users_with_page("deleg_manage")
    keys = sorted(mine - managers)
    if not keys:
        return []
    with db.cursor() as cur:
        cur.execute("SELECT user_key, username, display_name FROM dim_user WHERE user_key = ANY(%s) ORDER BY lower(display_name)", (keys,))
        return [{"id": r["username"], "name": r["display_name"], "user_key": r["user_key"]} for r in cur.fetchall()]


def _staff_by_username(cur, username: str) -> dict | None:
    cur.execute("SELECT user_key, username, display_name FROM dim_user WHERE lower(username) = lower(%s) AND password_hash <> %s",
                (username, auth.DISABLED_HASH))
    return cur.fetchone()


@router.get("/me")
def me(user=Depends(ANY)):
    """Who the screen is for: id (username), name and Admin / Manager / Staff, plus which sub pages are switched on."""
    who = _who(user)
    held = access.effective_access(user)
    return {"success": True, "id": user["username"], "name": user["display_name"], "role": who,
            "pages": sorted(p for p in PAGES if user["role"] == "admin" or p in held),
            "canWrite": user["role"] == "admin" or not access.is_view_only(user, "deleg_manage"),
            "photosEnabled": attachments.configured(),
            "scoring": {k: v for k, v in _settings().items() if k in ("on_time", "late", "revise", "extend")}}


# ------------------------------------------------------------------ tasks
def _deadline_state(r: dict, today: date) -> str:
    if r["status"] == "Pending":
        if r["due_date"] < today:
            return "overdue"
        return "today" if r["due_date"] == today else "ontime"
    return "late" if (r["score"] is not None and r["score"] < 0) else "done"


def _task(r: dict, today: date, photos: dict) -> dict:
    urls = photos.get(r["task_key"]) or []
    return {"taskId": r["task_no"], "assignedRole": r["staff_role"], "assignedName": r["staff_name"], "assignedId": r["staff_id"],
            "taskDesc": r["description"], "date": r["assigned_date"].isoformat(), "dueDate": r["due_date"].isoformat(),
            "status": r["status"], "extraWork": r["extra_work_notes"] or "", "photoUrl": urls[-1] if urls else "",
            "remark": r["remark"] or "", "score": r["score"], "deadlineState": _deadline_state(r, today)}


@router.get("/tasks")
def tasks(due: str = "", user=Depends(ANY)):
    """A staff member gets their own tasks; a manager or admin gets everyone's (the old privacy filter)."""
    who = _who(user)
    due_d = mc.parse_date(due, "Due date")
    where, params = ["TRUE"], []
    if not _privileged(who):
        where.append("t.staff_user_key = %s"); params.append(user["user_key"])
    if due_d:
        where.append("dd.full_date = %s"); params.append(due_d)
    with db.cursor() as cur:
        cur.execute(TASK_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.task_key DESC LIMIT 3000", params)
        rows = cur.fetchall()
    photos = attachments.file_urls("delegation", [r["task_key"] for r in rows], "proof")
    today = mc.today_ist()
    return [_task(r, today, photos) for r in rows]


@router.get("/staff")
def staff(user=Depends(ANY)):
    _need_manager_or_week(user)
    return [{"id": s["id"], "name": s["name"]} for s in assignable_staff()]


def _need_manager_or_week(user):
    if not _privileged(_who(user)):
        raise HTTPException(403, "Only a manager or admin can do this.")


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    staffId: str = ""
    staffName: str = ""
    desc: str = ""
    dueDate: str = ""


@router.post("/tasks")
def assign(body: AssignIn, user=Depends(ANY)):
    _need_manager(user)
    desc = body.desc.strip()
    if not body.staffId or not desc or not body.dueDate:
        return "All fields including deadline are required!"
    due = mc.parse_date(body.dueDate, "Deadline")
    try:
        with db.cursor() as cur:
            person = _staff_by_username(cur, body.staffId)
            if not person:
                return "Staff not found!"
            cur.execute("INSERT INTO fact_task (staff_user_key, assigned_by_user_key, description, assigned_date_key, due_date_key) "
                        "VALUES (%s, %s, %s, %s, %s)", (person["user_key"], user["user_key"], desc, mc.date_key(mc.today_ist()), mc.date_key(due)))
    except pgerr.ForeignKeyViolation as e:
        raise mc.date_fk_error(e)
    return f"Task assigned to {person['display_name']} — due {due.isoformat()}!"


class PhotoIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    notes: str = ""
    photo: str = ""


def _attach(user, record_key: int, kind: str, data_url: str) -> str:
    """Store the photo if there is one. Returns a sentence to add to the message when it could not be kept."""
    if not data_url:
        return ""
    if not attachments.configured():
        return " (Photo storage is not set up yet, so the photo was not saved.)"
    try:
        attachments.put_data_url("delegation", record_key, kind, user, data_url)
    except HTTPException as e:
        return f" (Photo not saved: {e.detail})"
    return ""


@router.post("/tasks/{task_no}/complete")
def complete(task_no: str, body: PhotoIn, user=Depends(ANY)):
    """Staff submits their own pending task: scored on time / late, plus any earlier revision and extension penalties."""
    _who(user)
    s = _settings()
    with db.cursor() as cur:
        cur.execute(TASK_SELECT + " WHERE t.task_no = %s FOR UPDATE OF t", (task_no,))
        r = cur.fetchone()
        if not r:
            return "Task Not Found"
        if r["staff_user_key"] != user["user_key"] and user["role"] != "admin":
            return "You can only complete your own tasks."
        if r["status"] != "Pending":
            return "This task is not pending any more."
        today = mc.today_ist()
        base = s["on_time"] if today <= r["due_date"] else s["late"]
        score = base + r["revision_count"] * s["revise"] + r["extension_count"] * s["extend"]
        cur.execute("UPDATE fact_task SET status = 'Completed By Staff', completed_date_key = %s, score = %s, "
                    "extra_work_notes = COALESCE(NULLIF(%s, ''), extra_work_notes), updated_at = now() WHERE task_key = %s",
                    (mc.date_key(today), score, body.notes.strip(), r["task_key"]))
    extra = _attach(user, r["task_key"], "proof", body.photo)
    return (f"Task submitted! +{score} points." if score > 0 else f"Task submitted — {score} points.") + extra


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str = ""
    remark: str = ""


@router.post("/tasks/{task_no}/review")
def review(task_no: str, body: ReviewIn, user=Depends(ANY)):
    _need_manager(user)
    if body.status not in ("Approved", "Rejected", "Revise"):
        return "Invalid decision."
    s = _settings()
    with db.cursor() as cur:
        cur.execute("SELECT task_key, status FROM fact_task WHERE task_no = %s FOR UPDATE", (task_no,))
        r = cur.fetchone()
        if not r:
            return "Task Not Found"
        if r["status"] != "Completed By Staff":
            return "This task is not waiting for review."
        if body.status == "Revise":
            cur.execute("UPDATE fact_task SET revision_count = revision_count + 1, status = 'Pending', score = NULL, remark = %s, "
                        "updated_at = now() WHERE task_key = %s", (body.remark.strip() or "Sent back for revision", r["task_key"]))
            return f"Sent back for revision — {s['revise']} points applied."
        cur.execute("UPDATE fact_task SET status = %s, remark = %s, updated_at = now() WHERE task_key = %s",
                    (body.status, body.remark.strip(), r["task_key"]))
    return f"Task {body.status} successfully!"


class ExtendIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    newDueDate: str = ""
    remark: str = ""


@router.post("/tasks/{task_no}/extend")
def extend(task_no: str, body: ExtendIn, user=Depends(ANY)):
    """Follow-up: give a task that missed its deadline a new one. The miss costs the extension penalty when it is finally done."""
    _need_manager(user)
    if not body.newDueDate:
        return "Task and new deadline are required!"
    new_due = mc.parse_date(body.newDueDate, "New deadline")
    s = _settings()
    try:
        with db.cursor() as cur:
            cur.execute(TASK_SELECT + " WHERE t.task_no = %s FOR UPDATE OF t", (task_no,))
            r = cur.fetchone()
            if not r:
                return "Task Not Found"
            if r["status"] != "Pending":
                return "Only a still-pending task can have its deadline revised."
            remark = body.remark.strip() or f"Missed deadline ({r['due_date'].isoformat()}) — revised to {new_due.isoformat()}, {s['extend']} pts"
            cur.execute("UPDATE fact_task SET due_date_key = %s, extension_count = extension_count + 1, remark = %s, updated_at = now() "
                        "WHERE task_key = %s", (mc.date_key(new_due), remark, r["task_key"]))
    except pgerr.ForeignKeyViolation as e:
        raise mc.date_fk_error(e)
    return f"Deadline revised to {new_due.isoformat()} — {s['extend']} points applied."


# ------------------------------------------------------------------ scores
def scoreboard_rows(cur, user_key: int | None = None) -> list[dict]:
    cur.execute("""SELECT u.username AS id, u.display_name AS name, u.role, COALESCE(SUM(t.score), 0) AS total,
                          COUNT(*) FILTER (WHERE t.score > 0) AS on_time, COUNT(*) FILTER (WHERE t.score <= 0) AS late
                   FROM fact_task t JOIN dim_user u ON u.user_key = t.staff_user_key
                   WHERE t.score IS NOT NULL AND (%s::int IS NULL OR t.staff_user_key = %s)
                   GROUP BY u.user_key ORDER BY total DESC, lower(u.display_name)""", (user_key, user_key))
    return [{"id": r["id"], "name": r["name"], "role": r["role"], "totalScore": int(r["total"]), "onTime": r["on_time"], "late": r["late"]}
            for r in cur.fetchall()]


@router.get("/scoreboard")
def scoreboard(user=Depends(ANY)):
    _who(user)
    _need_page(user, "deleg_scoreboard")
    with db.cursor() as cur:
        return scoreboard_rows(cur)


@router.get("/my-score")
def my_score(user=Depends(ANY)):
    _who(user)
    with db.cursor() as cur:
        rows = scoreboard_rows(cur, user["user_key"])
    return rows[0] if rows else {"id": user["username"], "totalScore": 0, "onTime": 0, "late": 0}


# ------------------------------------------------------------------ the week (Green / Yellow / Red)
def week_bounds(anchor: date, week_start_dow: int) -> tuple[date, date]:
    """Weeks run from `week_start_dow` (0 = Sunday .. 6 = Saturday, as the old script) for seven days."""
    py_dow = (anchor.weekday() + 1) % 7              # python: Monday=0 -> our Sunday=0
    start = anchor - timedelta(days=(py_dow - week_start_dow) % 7)
    return start, start + timedelta(days=6)


def classify(r: dict, today: date) -> str:
    """green = done on time, no revisions; yellow = done after one revision; red = late / 2+ revisions / extended /
    rejected / still open past its deadline; pending = not due yet or waiting for the manager."""
    st = r["status"]
    if st == "Pending":
        return "red" if today > r["due_date"] else "pending"
    if st == "Completed By Staff":
        return "pending"
    if st == "Rejected":
        return "red"
    if st == "Approved":
        if r["extension_count"] or r["revision_count"] >= 2:
            return "red"
        if r["revision_count"] == 1:
            return "yellow"
        return "green" if (r["score"] or 0) > 0 else "red"
    return "pending"


def _plan(cur, user_key: int, week_start: date):
    cur.execute("SELECT green_pct, yellow_pct, red_pct FROM fact_weekly_plan WHERE staff_user_key = %s AND week_start_key = %s",
                (user_key, mc.date_key(week_start)))
    p = cur.fetchone()
    return {"green": float(p["green_pct"]), "yellow": float(p["yellow_pct"]), "red": float(p["red_pct"])} if p else None


def _staff_for_week(user, staff_id: str, cur) -> dict:
    who = _who(user)
    _need_page(user, "deleg_week")
    person = _staff_by_username(cur, staff_id or user["username"])
    if not person:
        raise HTTPException(404, "Staff not found.")
    if not _privileged(who) and person["user_key"] != user["user_key"]:
        raise HTTPException(403, "You can only see your own week.")
    return person


@router.get("/weekly")
def weekly(staff_id: str = "", anchor: str = "", user=Depends(ANY)):
    s = _settings()
    a = mc.parse_date(anchor, "Week") or mc.today_ist()
    with db.cursor() as cur:
        person = _staff_for_week(user, staff_id, cur)
        start, end = week_bounds(a, s["week_start"])
        cur.execute(TASK_SELECT + " WHERE t.staff_user_key = %s AND dd.full_date BETWEEN %s AND %s", (person["user_key"], start, end))
        today = mc.today_ist()
        counts = {"green": 0, "yellow": 0, "red": 0, "pending": 0}
        for r in cur.fetchall():
            counts[classify(r, today)] += 1
        nstart, nend = week_bounds(end + timedelta(days=1), s["week_start"])
        plan, nplan = _plan(cur, person["user_key"], start), _plan(cur, person["user_key"], nstart)
    return {"weekStart": start.isoformat(), "weekEnd": end.isoformat(), "counts": counts, "plan": plan,
            "nextWeekStart": nstart.isoformat(), "nextWeekEnd": nend.isoformat(), "nextPlan": nplan}


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    staffId: str = ""
    weekStart: str = ""
    green: float = 0
    yellow: float = 0
    red: float = 0


@router.post("/weekly-plan")
def save_plan(body: PlanIn, user=Depends(ANY)):
    if round(body.green + body.yellow + body.red) != 100:
        return "ERROR: Green + Yellow + Red must add up to 100%."
    ws = mc.parse_date(body.weekStart, "Week")
    if not ws:
        return "ERROR: Week is required."
    try:
        with db.cursor() as cur:
            person = _staff_for_week(user, body.staffId, cur)
            cur.execute("INSERT INTO fact_weekly_plan (staff_user_key, week_start_key, green_pct, yellow_pct, red_pct) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (staff_user_key, week_start_key) DO UPDATE "
                        "SET green_pct = EXCLUDED.green_pct, yellow_pct = EXCLUDED.yellow_pct, red_pct = EXCLUDED.red_pct, saved_at = now()",
                        (person["user_key"], mc.date_key(ws), body.green, body.yellow, body.red))
    except pgerr.ForeignKeyViolation as e:
        raise mc.date_fk_error(e)
    return f"Plan saved for the week of {ws.isoformat()}!"


# ------------------------------------------------------------------ extra work (a staff member's own log)
class ExtraIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    desc: str = ""
    photo: str = ""


@router.post("/extra-work")
def extra_work(body: ExtraIn, user=Depends(ANY)):
    _who(user)
    if not body.desc.strip():
        return "Work details are required!"
    with db.cursor() as cur:
        cur.execute("INSERT INTO fact_extra_work (user_key, work_date_key, description) VALUES (%s, %s, %s) RETURNING extra_key",
                    (user["user_key"], mc.date_key(mc.today_ist()), body.desc.strip()))
        key = cur.fetchone()["extra_key"]
    return "Extra work logged successfully!" + _attach(user, key, "extra_work", body.photo)
