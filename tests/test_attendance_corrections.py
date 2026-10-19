"""Owner dashboard: the admin corrects / adds / removes a wrongly punched day (only the admin), and the whole-team month data the Excel exports use."""
from datetime import timedelta

from app import modcommon
from tests.test_attendance import own, owner_user
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


def employee(client, admin, name):
    r = client.post("/admin/masters/att_employee", json={"name": name, "role_title": "Staff", "is_active": True, "skip_whatsapp": True}, headers=admin)
    assert r.status_code == 201, r.text
    return name


def correct(client, admin, name, day, **kw):
    body = {"name": name, "date": day.isoformat(), "checkIn": "09:00", "checkOut": "19:00", "status": "", "site": "", "reason": "Wrong punch", **kw}
    return own(client, admin, "correctAttendance", body)


def month_day(client, admin, name, day):
    rep = own(client, admin, "getMonthlyReport", name, day.year, day.month)
    return next(d for d in rep["days"] if d["date"] == day.isoformat())


def audit(db_conn, action, target):
    with db_conn.cursor() as cur:
        cur.execute("SELECT details FROM admin_audit_log WHERE action = %s AND target LIKE %s ORDER BY audit_key", (action, target + "%"))
        return [r[0] for r in cur.fetchall()]


def test_admin_adds_corrects_and_removes_a_punch(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    name = employee(client, admin, "Fix Person")
    day = modcommon.today_ist() - timedelta(days=2)

    assert month_day(client, admin, name, day)["hasRecord"] is False
    added = correct(client, admin, name, day, checkIn="10:50")                     # no punch that day: the admin adds it
    assert added["status"] == "ok" and added["created"] is True and added["finalStatus"] == "Late" and added["hoursWorked"] == "8h 10m"
    d = month_day(client, admin, name, day)
    assert d["hasRecord"] and d["status"] == "Late" and d["time"] == "10:50" and d["checkoutTime"] == "19:00" and d["corrected"] and d["note"] == "Wrong punch"

    fixed = correct(client, admin, name, day, checkIn="09:00", reason="Punched late by mistake")      # the time was wrong: corrected
    assert fixed["created"] is False and fixed["finalStatus"] == "Present"
    d = month_day(client, admin, name, day)
    assert d["status"] == "Present" and d["time"] == "09:00" and d["note"] == "Punched late by mistake"

    assert correct(client, admin, name, day, status="Half Day")["finalStatus"] == "Half Day"            # the admin may force the status
    assert month_day(client, admin, name, day)["status"] == "Half Day"
    open_day = correct(client, admin, name, day, checkOut="")                                       # no check-out: an open day
    assert open_day["finalStatus"] == "Incomplete" and month_day(client, admin, name, day)["checkoutTime"] == "—"

    # a correction never sends a WhatsApp message, and is logged with what it replaced
    with db_conn.cursor() as cur:
        cur.execute("SELECT wa_status, corrected_by_user_key FROM fact_attendance a JOIN dim_att_employee e ON e.employee_key = a.employee_key "
                    "WHERE e.employee_name = %s AND a.work_date = %s", (name, day))
        wa, by = cur.fetchone()
    assert wa == "Skipped (No WA)" and by is not None
    log = audit(db_conn, "attendance.correct", f"{name} {day.isoformat()}")
    assert len(log) == 4 and log[0]["before"] is None and log[0]["after"]["status"] == "Late" and log[1]["before"]["in"] == "10:50" and log[1]["reason"] == "Punched late by mistake"

    gone = own(client, admin, "deleteAttendance", {"name": name, "date": day.isoformat(), "reason": "Punched by mistake"})
    assert gone["status"] == "ok" and month_day(client, admin, name, day)["hasRecord"] is False
    assert own(client, admin, "deleteAttendance", {"name": name, "date": day.isoformat(), "reason": "again"})["status"] == "error"     # nothing left
    assert audit(db_conn, "attendance.delete", f"{name} {day.isoformat()}")[0]["before"]["status"] == "Incomplete"


def test_corrections_are_checked(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    name = employee(client, admin, "Check Person")
    day = modcommon.today_ist() - timedelta(days=1)
    msg = lambda **kw: correct(client, admin, name, day, **kw)["msg"]  # noqa: E731
    assert "reason" in msg(reason="  ")
    assert "Check-in time is required" in msg(checkIn="")
    assert "not a valid time" in msg(checkIn="9am") and "not a valid time" in msg(checkOut="25:99")
    assert "before check-in" in msg(checkIn="12:00", checkOut="11:00")
    assert "Status must be" in msg(status="Holiday")
    assert "from the list" in msg(site="Nowhere")
    assert "future" in correct(client, admin, name, modcommon.today_ist() + timedelta(days=1))["msg"]
    assert "date is not valid" in correct(client, admin, name, day, date="31-12-2026")["msg"]
    assert own(client, admin, "correctAttendance", {"name": "Nobody", "date": day.isoformat(), "checkIn": "09:00", "reason": "x"})["status"] == "error"
    assert month_day(client, admin, name, day)["hasRecord"] is False               # none of the refused corrections saved anything


def test_only_the_admin_can_correct_attendance(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    name = employee(client, admin, "Guarded Person")
    day = modcommon.today_ist() - timedelta(days=1)
    owner = owner_user(client, db_conn)                       # may open the Owner dashboard, but is not the admin
    assert own(client, owner, "getTodayAttendance")["status"] == "ok"
    refused = correct(client, owner, name, day)
    assert refused["status"] == "error" and "Only the admin" in refused["msg"]
    assert "Only the admin" in own(client, owner, "deleteAttendance", {"name": name, "date": day.isoformat(), "reason": "x"})["msg"]
    viewer = owner_user(client, db_conn, view_only=True)
    r = client.post("/attendance/owner/rpc", json={"action": "correctAttendance", "args": ["portal", {}]}, headers=viewer)
    assert r.status_code == 403                                # a View only person never reaches it
    assert month_day(client, admin, name, day)["hasRecord"] is False
    assert client.get("/attendance/owner/me", headers=admin).json()["canCorrect"] is True
    me = client.get("/attendance/owner/me", headers=owner).json()
    assert me["canCorrect"] is False and me["role"] != "admin"


def test_whole_team_month_data_for_the_exports(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    a, b = employee(client, admin, "Team Alpha"), employee(client, admin, "Team Beta")
    day = modcommon.today_ist() - timedelta(days=1)
    correct(client, admin, a, day, checkIn="09:00")
    own(client, admin, "setEmployeeSalary", a, 30000)
    rep = own(client, admin, "getMonthlyAll", day.year, day.month)
    assert rep["status"] == "ok" and rep["year"] == day.year
    team = {e["name"]: e for e in rep["employees"]}
    assert {"Team Alpha", "Team Beta"} <= set(team)
    one = own(client, admin, "getMonthlyReport", a, day.year, day.month)
    assert team[a]["stats"] == one["stats"] and team[a]["days"] == one["days"]       # the same numbers as the single-employee report
    import calendar
    assert len(team[a]["days"]) == calendar.monthrange(day.year, day.month)[1]
    assert next(d for d in team[a]["days"] if d["date"] == day.isoformat())["status"] == "Present"

    sal = own(client, admin, "getSalaryAll", day.year, day.month)
    assert sal["status"] == "ok" and sal["baseDays"] == 30
    rows = {r["name"]: r for r in sal["rows"]}
    assert rows[b]["salarySet"] is False                                              # listed, but no salary to calculate
    single = own(client, admin, "getSalaryReport", a, day.year, day.month)
    assert rows[a]["salarySet"] and rows[a]["netSalary"] == single["netSalary"] and rows[a]["monthlySalary"] == 30000
    assert rows[a]["absent"] == single["stats"]["absent"] and rows[a]["deductionAmount"] == single["deductionAmount"]
    assert own(client, admin, "getMonthlyAll", "x", 99)["status"] == "error" and own(client, admin, "getSalaryAll", day.year, 13)["status"] == "error"
