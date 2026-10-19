"""Delegation and Checklist rules found by validating the demo data: a rejected task earns nothing, an extension only costs points when the
deadline was missed, no deadline in the past, plan shares between 0 and 100, a stopped checklist keeps its history, reassigning moves the
open items, staff cannot rewrite what they already marked, and tasks only go to staff."""
from datetime import timedelta

from app import modcommon
from tests.test_checklist_expenses_places import grant
from tests.test_modules import days, user  # noqa: F401  (days: a fixture)
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


def tasks(client, headers):
    return client.get("/delegation/tasks", headers=headers).json()


def assign(client, mgr, staff_id, desc, due):
    return client.post("/delegation/tasks", json={"staffId": staff_id, "desc": desc, "dueDate": due.isoformat()}, headers=mgr).json()


def find(client, headers, desc):
    return next(t for t in tasks(client, headers) if t["taskDesc"] == desc)


def total(client, staff):
    return client.get("/delegation/my-score", headers=staff).json()["totalScore"]


# ---------------------------------------------------------------- delegation
def test_rejected_task_earns_no_points(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    assert "Task assigned" in assign(client, mgr, sname, "Rejected job", days + timedelta(days=3))
    t = find(client, mgr, "Rejected job")
    assert "+10" in client.post(f"/delegation/tasks/{t['taskId']}/complete", json={}, headers=staff).json()
    assert total(client, staff) == 10                                  # counted while it waits for the manager
    assert client.post(f"/delegation/tasks/{t['taskId']}/review", json={"status": "Rejected", "remark": "no"}, headers=mgr).json() \
        == "Task Rejected successfully!"
    rejected = find(client, mgr, "Rejected job")
    assert rejected["status"] == "Rejected" and rejected["score"] is None
    assert total(client, staff) == 0                                   # nothing earned, nothing on the scoreboard


def test_extension_costs_points_only_when_the_deadline_was_missed(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    assign(client, mgr, sname, "Early move", days + timedelta(days=5))
    early = find(client, mgr, "Early move")
    msg = client.post(f"/delegation/tasks/{early['taskId']}/extend", json={"newDueDate": (days + timedelta(days=8)).isoformat()}, headers=mgr).json()
    assert "no penalty" in msg and "-5" not in msg
    client.post(f"/delegation/tasks/{early['taskId']}/complete", json={}, headers=staff)
    assert total(client, staff) == 10                                  # a deadline moved before it was missed costs nothing

    # a missed deadline still costs the extension penalty (existing rule)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO fact_task (staff_user_key, assigned_by_user_key, description, assigned_date_key, due_date_key) "
                    "SELECT s.user_key, s.user_key, 'Missed one', %s, %s FROM dim_user s WHERE s.username = %s",
                    (modcommon.date_key(days - timedelta(days=3)), modcommon.date_key(days - timedelta(days=2)), sname))
    db_conn.commit()
    missed = find(client, mgr, "Missed one")
    assert "-5 points" in client.post(f"/delegation/tasks/{missed['taskId']}/extend", json={"newDueDate": (days + timedelta(days=2)).isoformat()}, headers=mgr).json()
    client.post(f"/delegation/tasks/{missed['taskId']}/complete", json={}, headers=staff)
    assert total(client, staff) == 10 + 5                              # 10 on time - 5 for the extension


def test_deadlines_cannot_be_in_the_past(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    assert "cannot be in the past" in assign(client, mgr, sname, "Old", days - timedelta(days=1))
    assert not [t for t in tasks(client, mgr) if t["taskDesc"] == "Old"]
    assert "Task assigned" in assign(client, mgr, sname, "Today is fine", days)
    t = find(client, mgr, "Today is fine")
    msg = client.post(f"/delegation/tasks/{t['taskId']}/extend", json={"newDueDate": (days - timedelta(days=4)).isoformat()}, headers=mgr).json()
    assert "cannot be in the past" in msg
    assert find(client, mgr, "Today is fine")["dueDate"] == days.isoformat()          # unchanged


def test_weekly_plan_shares_must_be_between_0_and_100(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    week = client.get(f"/delegation/weekly?staff_id={sname}", headers=staff).json()["nextWeekStart"]

    def plan(g, y, r):
        return client.post("/delegation/weekly-plan", json={"staffId": sname, "weekStart": week, "green": g, "yellow": y, "red": r}, headers=staff).json()

    assert "between 0% and 100%" in plan(150, -30, -20)                # adds up to 100, but nonsense
    assert "between 0% and 100%" in plan(120, 0, -20)
    assert "between 0% and 100%" in plan(101, 0, -1)
    assert client.get(f"/delegation/weekly?staff_id={sname}&anchor={week}", headers=staff).json()["plan"] is None
    assert "Plan saved" in plan(100, 0, 0) and "Plan saved" in plan(0, 0, 100) and "Plan saved" in plan(60, 30, 10)


def test_tasks_go_to_staff_not_to_managers_or_non_users(client, db_conn, days, seed):
    mgr, _ = user(client, db_conn, "manager")
    other_mgr, other_name = user(client, db_conn, "manager")
    nobody, nobody_name = user(client, db_conn, "cashier")             # holds no Delegation page
    staff, sname = user(client, db_conn, "staff")
    for who in (other_name, nobody_name, "admin"):
        msg = assign(client, mgr, who, "Not for you", days + timedelta(days=2))
        assert "cannot be given tasks" in msg or "Staff not found" in msg, msg
    assert not [t for t in tasks(client, mgr) if t["taskDesc"] == "Not for you"]
    assert "Task assigned" in assign(client, mgr, sname, "For staff", days + timedelta(days=2))


# ---------------------------------------------------------------- checklist
def _checklist(client, mgr, staff_name, title="Daily round", **kw):
    body = {"title": title, "assignee": staff_name, "frequency": "Daily", "startDate": modcommon.today_ist().isoformat(), **kw}
    r = client.post("/checklist/checklists", json=body, headers=mgr)
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _back_date(db_conn, key, days_back):
    """Checklists cannot start in the past through the API; move the start back so there is history to look at."""
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dim_checklist SET start_date_key = %s WHERE checklist_key = %s",
                    (modcommon.date_key(modcommon.today_ist() - timedelta(days=days_back)), key))
    db_conn.commit()


def test_stopped_checklist_keeps_its_history_but_not_its_open_items(client, db_conn, days, seed):
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "cashier")
    grant(db_conn, mname, "chk_manage", "chk_calendar")
    grant(db_conn, sname, "chk_mine", "chk_calendar")
    key = _checklist(client, mgr, sname, "History list")
    _back_date(db_conn, key, 6)
    mine = client.get("/checklist/mine", headers=staff).json()
    overdue = [i for i in mine["overdue"] if i["title"] == "History list"]
    assert len(overdue) >= 3
    done = overdue[0]
    assert client.post(f"/checklist/occurrences/{done['key']}/mark", json={"status": "Done"}, headers=staff).status_code == 200
    today = modcommon.today_ist()
    cal_url = f"/checklist/calendar?year={today.year}&month={today.month}"

    assert client.post(f"/checklist/checklists/{key}/active", json={"active": False}, headers=mgr).status_code == 200
    after = client.get("/checklist/mine", headers=staff).json()
    assert any(i["key"] == done["key"] and i["status"] == "Done" for i in after["finished"])         # the history is still there
    assert not [i for i in after["overdue"] + after["dueToday"] + after["upcoming"] if i["title"] == "History list"]   # open items are gone
    cal_items = [i for v in client.get(cal_url, headers=mgr).json()["days"].values() for i in v if i["title"] == "History list"]
    assert cal_items and all(i["status"] != "Pending" for i in cal_items)

    assert client.post(f"/checklist/checklists/{key}/active", json={"active": True}, headers=mgr).status_code == 200
    assert [i for i in client.get("/checklist/mine", headers=staff).json()["overdue"] if i["title"] == "History list"]   # back on the list


def test_reassigning_a_checklist_moves_the_open_items(client, db_conn, days, seed):
    mgr, mname = user(client, db_conn, "manager")
    old, oname = user(client, db_conn, "cashier")
    new, nname = user(client, db_conn, "cashier")
    grant(db_conn, mname, "chk_manage")
    grant(db_conn, oname, "chk_mine")
    grant(db_conn, nname, "chk_mine")
    key = _checklist(client, mgr, oname, "Handover list")
    _back_date(db_conn, key, 5)
    first = client.get("/checklist/mine", headers=old).json()["overdue"]
    first = [i for i in first if i["title"] == "Handover list"]
    assert len(first) >= 3
    kept = first[0]
    assert client.post(f"/checklist/occurrences/{kept['key']}/mark", json={"status": "Done"}, headers=old).status_code == 200

    body = {"title": "Handover list", "assignee": nname, "frequency": "Daily"}
    assert client.put(f"/checklist/checklists/{key}", json=body, headers=mgr).status_code == 200
    old_mine = client.get("/checklist/mine", headers=old).json()
    new_mine = client.get("/checklist/mine", headers=new).json()
    assert not [i for i in old_mine["overdue"] + old_mine["dueToday"] + old_mine["upcoming"] if i["title"] == "Handover list"]   # nothing stranded
    assert len([i for i in new_mine["overdue"] if i["title"] == "Handover list"]) == len(first) - 1        # the open ones moved over
    assert any(i["key"] == kept["key"] for i in old_mine["finished"])                                      # what was marked stays with who marked it


def test_staff_cannot_rewrite_what_they_already_marked(client, db_conn, days, seed):
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "cashier")
    grant(db_conn, mname, "chk_manage")
    grant(db_conn, sname, "chk_mine")
    key = _checklist(client, mgr, sname, "Once marked")
    _back_date(db_conn, key, 3)
    item = next(i for i in client.get("/checklist/mine", headers=staff).json()["overdue"] if i["title"] == "Once marked")
    mark = lambda who, status, remark="": client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": status, "remark": remark}, headers=who)  # noqa: E731
    assert mark(staff, "Done").status_code == 200
    for status, remark in (("Not Done", "changed my mind"), ("Done", "again"), ("Pending", "")):
        r = mark(staff, status, remark)
        assert r.status_code == 403, (status, r.text)                  # no second go, no re-opening
    assert any(i["key"] == item["key"] and i["status"] == "Done" and i["markedBy"] for i in client.get("/checklist/mine", headers=staff).json()["finished"])
    assert mark(mgr, "Pending").status_code == 200                      # a manager can re-open ...
    assert mark(staff, "Not Done", "really not done").status_code == 200   # ... and then it is open again for the person
