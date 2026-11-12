# ruff: noqa: F811, E501
"""The salary rules of the November change list: leaves, the one-day bonus, the Monday cut and Monday work, advances with a date, and a
negative net payable that moves to the next month. (June 2026: Mondays - the weekly off - are the 1st, 8th, 15th, 22nd and 29th.)"""
from datetime import date, time

from app import modcommon
from tests.test_attendance import own, owner_user
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)

JUNE = (2026, 6)


def _employee(client, db_conn, name, salary=30000):
    admin = login(client, db_conn, "admin")
    client.post("/admin/masters/att_employee", json={"name": name, "is_active": True}, headers=admin)
    boss = owner_user(client, db_conn)
    assert own(client, boss, "setEmployeeSalary", name, salary)["action"] == "set"
    with db_conn.cursor() as cur:
        cur.execute("SELECT employee_key FROM dim_att_employee WHERE employee_name = %s", (name,))
        return boss, cur.fetchone()[0]


def _month(db_conn, key, year, month, absent=(), half=(), worked_mondays=()):
    """Present on every day of the month except Mondays (off), `absent` (no record), `half` (a half day); `worked_mondays` also come in."""
    import calendar
    with db_conn.cursor() as cur:
        for d in range(1, calendar.monthrange(year, month)[1] + 1):
            dt = date(year, month, d)
            if d in absent or (dt.weekday() == 0 and d not in worked_mondays):
                continue
            cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                        "VALUES (%s, %s, %s, %s, %s, 480) ON CONFLICT DO NOTHING",
                        (key, dt, time(9, 0), time(18, 0), "Half Day" if d in half else "Present"))
    db_conn.commit()


def _report(client, boss, name, year=2026, month=6):
    return own(client, boss, "getSalaryReport", name, year, month)


def test_a_month_without_a_leave_earns_a_bonus_day(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Bonus")
    _month(db_conn, key, *JUNE)
    r = _report(client, boss, "Rule Bonus")
    assert r["bonusEarned"] is True and r["bonus"] == 1000 and r["netSalary"] == 31000 and r["totalPaidDays"] == 31
    assert r["totalDeductionDays"] == 0 and r["leaves"] == 0


def test_one_leave_costs_nothing_two_or_more_are_all_cut(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule One Leave")
    _month(db_conn, key, *JUNE, absent=(10,))
    one = _report(client, boss, "Rule One Leave")
    assert one["leaves"] == 1 and one["totalDeductionDays"] == 0 and one["netSalary"] == 30000 and one["bonusEarned"] is False
    boss, key = _employee(client, db_conn, "Rule Two Leaves")
    _month(db_conn, key, *JUNE, absent=(10, 11))
    two = _report(client, boss, "Rule Two Leaves")
    assert two["leaves"] == 2 and two["totalDeductionDays"] == 2 and two["deductionAmount"] == 2000 and two["netSalary"] == 28000


def test_an_absent_sunday_or_tuesday_also_cuts_monday(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Monday Cut")
    _month(db_conn, key, *JUNE, absent=(7,))                               # Sunday 7th absent: Monday 8th is cut, though the one leave itself is free
    r = _report(client, boss, "Rule Monday Cut")
    assert next(d for d in r["days"] if d["day"] == 8)["status"] == "Absent (Sandwich)"
    assert r["leaves"] == 1 and r["stats"]["sandwich"] == 1 and r["totalDeductionDays"] == 1 and r["netSalary"] == 29000
    boss, key = _employee(client, db_conn, "Rule Tuesday")
    _month(db_conn, key, *JUNE, absent=(9,))                               # Tuesday 9th absent: Monday 8th is cut too
    assert next(d for d in _report(client, boss, "Rule Tuesday")["days"] if d["day"] == 8)["status"] == "Absent (Sandwich)"


def test_working_on_a_monday_cancels_a_leave_and_otherwise_earns_nothing_extra(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Monday Work")
    _month(db_conn, key, *JUNE, absent=(10, 11), worked_mondays=(15,))     # two leaves, one cancelled by Monday the 15th: one leave left = free
    r = _report(client, boss, "Rule Monday Work")
    mon = next(d for d in r["days"] if d["day"] == 15)
    assert mon["workedOnOff"] == "Present" and mon["time"] == "09:00" and r["stats"]["offWorked"] == 1
    assert r["leaves"] == 2 and r["leavesAfterOffset"] == 1 and r["totalDeductionDays"] == 0 and r["netSalary"] == 30000
    boss, key = _employee(client, db_conn, "Rule Monday Plain")
    _month(db_conn, key, *JUNE, worked_mondays=(15,))                      # no leave: Monday work earns no extra, the bonus is all there is
    plain = _report(client, boss, "Rule Monday Plain")
    assert plain["bonusEarned"] is True and plain["netSalary"] == 31000


def test_a_half_day_or_a_late_mark_is_no_leave_but_cancels_the_bonus(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Half Day")
    _month(db_conn, key, *JUNE, half=(10,))
    r = _report(client, boss, "Rule Half Day")
    assert r["leaves"] == 0 and r["totalDeductionDays"] == 0.5 and r["bonusEarned"] is False and r["netSalary"] == 29500


def test_the_bonus_waits_for_the_month_to_end(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Open Month")
    today = modcommon.today_ist()
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                    "VALUES (%s, %s, %s, %s, 'Present', 480) ON CONFLICT DO NOTHING", (key, today, time(9, 0), time(18, 0)))
    db_conn.commit()
    r = _report(client, boss, "Rule Open Month", today.year, today.month)
    assert r["monthOver"] is False and r["bonusEarned"] is False and r["bonus"] == 0


def test_an_advance_has_its_own_date_and_cannot_be_in_the_future(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Advance Date")
    _month(db_conn, key, *JUNE)
    assert own(client, boss, "addAdvancePayment", "Rule Advance Date", 2000, "october", "2026-06-10")["status"] == "ok"
    assert own(client, boss, "addAdvancePayment", "Rule Advance Date", 500, "typed dd-mm-yyyy", "12-06-2026")["status"] == "ok"
    assert own(client, boss, "addAdvancePayment", "Rule Advance Date", 100, "", "not a date")["status"] == "error"
    assert own(client, boss, "addAdvancePayment", "Rule Advance Date", 100, "", "2999-01-01")["status"] == "error"
    r = _report(client, boss, "Rule Advance Date")
    assert r["totalAdvance"] == 2500 and [a["date"] for a in r["advances"]] == ["10-06-2026", "12-06-2026"] and r["netSalary"] == 31000 - 2500
    today = modcommon.today_ist()
    assert own(client, boss, "addAdvancePayment", "Rule Advance Date", 700, "today by default")["status"] == "ok"
    listed = own(client, boss, "getAllAdvances", "Rule Advance Date")["advances"]
    assert listed[0]["date"] == today.strftime("%d-%m-%Y") and {a["date"] for a in listed} >= {"10-06-2026", "12-06-2026"}


def test_more_advance_than_salary_shows_negative_and_moves_to_next_month(client, db_conn, seed):
    boss, key = _employee(client, db_conn, "Rule Carry")
    _month(db_conn, key, *JUNE)                                             # bonus month: 31,000 earned
    _month(db_conn, key, 2026, 7)                                           # July is a bonus month too
    assert own(client, boss, "addAdvancePayment", "Rule Carry", 40000, "big", "2026-06-15")["status"] == "ok"
    june = _report(client, boss, "Rule Carry")
    assert june["netSalary"] == -9000 and june["carryForward"] == 9000 and june["carryIn"] == 0
    july = _report(client, boss, "Rule Carry", 2026, 7)
    assert july["carryIn"] == 9000 and july["netSalary"] == 31000 - 9000 and july["carryForward"] == 0
    sheet = {r["name"]: r for r in own(client, boss, "getSalaryAll", 2026, 7)["rows"]}["Rule Carry"]
    assert sheet["carryIn"] == 9000 and sheet["bonus"] == 1000 and sheet["netSalary"] == july["netSalary"]
