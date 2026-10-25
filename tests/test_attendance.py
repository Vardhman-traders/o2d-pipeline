"""Attendance module: GPS spot, check-in / out rules, WhatsApp state, salary, advances, PINs (one targeted flow per area)."""
from datetime import date, time, timedelta

from app import attachments, attendance, modcommon, whatsapp
from tests.test_modules import FakeStore, user
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)

GODOWN = (28.7004323, 77.1288457)
FAR = (28.72, 77.15)


def pub(http, action, *args):
    r = http.post("/attendance/public/rpc", json={"action": action, "args": list(args)})
    assert r.status_code == 200, r.text
    return r.json()


def own(http, headers, action, *args):
    r = http.post("/attendance/owner/rpc", json={"action": action, "args": ["portal", *args]}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def owner_user(http, db_conn, page="att_owner", view_only=False):
    headers, name = user(http, db_conn, "godown")
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, view_only) SELECT user_key, %s, true, %s FROM dim_user WHERE username = %s",
                    (page, view_only, name))
    db_conn.commit()
    return headers


def test_time_rules_follow_the_settings_not_the_code(client):
    r = attendance.Rules()
    m = lambda h, mi=0: h * 60 + mi  # noqa: E731
    assert r.checkin_status(m(10, 29)) == "Present" and r.checkin_status(m(10, 30)) == "Late" and r.checkin_status(m(14)) == "Half Day"
    assert r.final_status(m(9), m(13, 59)) == "Absent" and r.final_status(m(9), m(14)) == "Half Day"
    assert r.final_status(m(9), m(17)) == "Present" and r.final_status(m(11), m(18)) == "Late" and r.final_status(m(14), m(19)) == "Half Day"
    assert r.final_status(m(9), m(21)) == "Present"
    # an open day counts as an auto check-out at 8 PM: status from the check-in time alone
    assert r.resolve("Incomplete", m(9)) == "Present" and r.resolve("", m(11)) == "Late" and r.resolve("Half Day", m(9)) == "Half Day"
    assert r.js_dow(date(2026, 10, 12)) == 1 and r.is_off(date(2026, 10, 12))        # a Monday is the weekly off
    assert attendance._hours(m(9, 5), m(18, 0)) == ("8h 55m", 535) and attendance._hours(m(10), m(9))[1] is None


def test_check_in_out_flow_with_location_photo_and_whatsapp(client, db_conn, seed, monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    store = FakeStore()
    attachments.set_storage(store)
    sent = []
    monkeypatch.setattr(whatsapp, "_http_post", lambda url, body, headers: sent.append(body.decode()) or (200, '{"success": true}'))
    monkeypatch.setenv("WHATSAPP_API_KEY", "k")
    try:
        admin = login(client, db_conn, "admin")
        name = "Test Person"
        assert client.post("/admin/masters/att_employee", json={"name": name, "role_title": "Tester", "is_active": True, "skip_whatsapp": False},
                           headers=admin).status_code == 201
        assert {"name": name, "role": "Tester"} in pub(client, "getEmployees")

        # the spot is a Setup list: inside the radius is ok, outside says how far both places are
        ok = pub(client, "checkLocation", *GODOWN)
        assert ok["status"] == "ok" and ok["site"] == "Godown" and ok["dist"] <= 1 and ok["radius"] == 40
        far = pub(client, "checkLocation", *FAR)
        # nobody is tied to a work place here, so being far from both is fine: they may mark from anywhere (see test_attendance_anywhere.py)
        assert far["status"] == "ok" and far["anywhere"] is True and far["distGodown"] > 1000 and far["distShop"] > 1000
        assert pub(client, "saveAttendance", {"name": "Nobody", "lat": GODOWN[0], "lng": GODOWN[1]})["status"] == "error"

        # check in with a photo; a second tap says "already marked" and does not make a second record
        photo = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        a = pub(client, "saveAttendance", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1], "photo": photo})
        assert a["status"] == "ok" and a["site"] == "Godown" and a["photoSaved"] is True and len(store.objects) == 1
        again = pub(client, "saveAttendance", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1], "photo": photo})
        assert again["status"] == "already_marked" and again["rowIndex"] == a["rowIndex"]
        st = pub(client, "getTodayStatus", name)
        assert st["checkedIn"] and not st["checkedOut"] and st["finalStatus"] == "Incomplete"

        # WhatsApp: not configured -> stays pending; once a group is set it goes out exactly once
        assert pub(client, "notifyAttendance", a["rowIndex"], "someone else")["status"] == "error"
        assert pub(client, "notifyAttendance", a["rowIndex"], name)["status"] == "failed"
        with db_conn.cursor() as cur:
            cur.execute("SELECT wa_status FROM fact_attendance WHERE attendance_key = %s", (a["rowIndex"],))
            assert cur.fetchone()[0] == "IN Pending"
        body = {"group_id": "123@g.us", "enabled": True, "template": client.get("/admin/whatsapp", headers=admin).json()["alerts"][0]["template"]}
        att_alerts = {x["key"]: x for x in client.get("/admin/whatsapp", headers=admin).json()["alerts"]}
        assert {"att_checkin", "att_checkout", "att_auto_summary"} <= set(att_alerts)
        for key in ("att_checkin", "att_checkout"):
            assert client.put(f"/admin/whatsapp/{key}", json={**body, "template": att_alerts[key]["template"]}, headers=admin).status_code == 200
        assert pub(client, "notifyAttendance", a["rowIndex"], name)["status"] == "sent"
        assert pub(client, "notifyAttendance", a["rowIndex"], name)["status"] == "nothing"   # never twice
        assert len(sent) == 1 and "CHECK-IN" in sent[0] and name in sent[0] and "123@g.us" in sent[0]

        # check out: status from the clock, the checkout message follows, photo stored
        out = pub(client, "saveCheckout", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1], "photo": photo})
        assert out["status"] == "ok" and out["photoSaved"] is True and out["finalStatus"] in ("Absent", "Half Day", "Present", "Late")
        assert pub(client, "saveCheckout", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1]})["status"] == "already_checkout"
        assert pub(client, "notifyAttendance", a["rowIndex"], name)["status"] == "sent" and "CHECK-OUT" in sent[1]
        # a person set to "no WhatsApp" is recorded but never messaged
        quiet = "Quiet Person"
        client.post("/admin/masters/att_employee", json={"name": quiet, "is_active": True, "skip_whatsapp": True}, headers=admin)
        q = pub(client, "saveAttendance", {"name": quiet, "lat": GODOWN[0], "lng": GODOWN[1]})
        assert q["status"] == "ok" and q["photoSaved"] is False and pub(client, "notifyAttendance", q["rowIndex"], quiet)["status"] == "nothing"
        assert len(sent) == 2

        # the owner dashboard: today's table (with photo links), manual and automatic check-out
        boss = owner_user(client, db_conn)
        today = own(client, boss, "getTodayAttendance")
        mine = next(m for m in today["marked"] if m["name"] == name)
        assert mine["checkedOut"] and mine["photoIn"].startswith("https://acct.r2.cloudflarestorage.com/bkt/attendance/")
        assert any(m["name"] == quiet and not m["checkedOut"] for m in today["marked"])
        assert own(client, boss, "manualCheckout", quiet, "13:00")["status"] == "error"          # only the admin checks someone out by hand
        assert own(client, admin, "manualCheckout", quiet, "13:00")["status"] == "ok"
        assert own(client, admin, "manualCheckout", quiet, "13:00")["status"] == "already_checkout"
        assert client.post("/attendance/owner/rpc", json={"action": "getTodayAttendance", "args": []}, headers=user(client, db_conn, "godown")[0]).status_code == 403
    finally:
        attachments.set_storage(None)


def test_auto_checkout_closes_open_days_once(client, db_conn, seed, monkeypatch):
    sent = []
    monkeypatch.setattr(whatsapp, "_http_post", lambda url, body, headers: sent.append(body.decode()) or (200, "ok"))
    monkeypatch.setenv("WHATSAPP_API_KEY", "k")
    admin = login(client, db_conn, "admin")
    summary = client.get("/admin/whatsapp", headers=admin).json()["alerts"]
    tpl = next(a for a in summary if a["key"] == "att_auto_summary")["template"]
    client.put("/admin/whatsapp/att_auto_summary", json={"group_id": "9@g.us", "enabled": True, "template": tpl}, headers=admin)
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM fact_attendance")
        cur.execute("SELECT employee_key FROM dim_att_employee WHERE employee_name IN ('Ritesh', 'Rahul') ORDER BY employee_name")
        keys = [r[0] for r in cur.fetchall()]
        for k, t in zip(keys, (time(9, 0), time(11, 0))):
            cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, wa_status) VALUES (%s, %s, %s, 'IN Sent')",
                        (k, modcommon.today_ist(), t))
    db_conn.commit()
    assert attendance.auto_checkout() == 1          # Rahul is on the no-WhatsApp list: closed, but not in the message
    with db_conn.cursor() as cur:
        cur.execute("SELECT e.employee_name, a.final_status, a.check_out_time, a.checkout_source, a.wa_status FROM fact_attendance a "
                    "JOIN dim_att_employee e USING (employee_key) ORDER BY 1")
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    db_conn.rollback()
    assert rows["Ritesh"][0] == "Late" or rows["Ritesh"][0] == "Present"
    assert rows["Rahul"][1] == time(20, 0) and rows["Rahul"][2] == "Auto (8 PM)" and rows["Rahul"][3] == "Skipped (No WA)"
    assert len(sent) == 1 and "AUTO CHECK-OUT" in sent[0] and "Rahul" not in sent[0]
    assert attendance.auto_checkout() == 0          # nothing left open: no second message


def test_salary_sandwich_advances_and_pins(client, db_conn, seed):
    boss = owner_user(client, db_conn)
    name = "Salary Person"
    admin = login(client, db_conn, "admin")
    client.post("/admin/masters/att_employee", json={"name": name, "is_active": True}, headers=admin)
    assert own(client, boss, "getSalaryReport", name, 2026, 6)["status"] == "error"          # no salary yet
    assert own(client, boss, "setEmployeeSalary", name, 30000)["action"] == "set"
    assert own(client, boss, "setEmployeeSalary", name, 0)["status"] == "error"
    # June 2026: 1 is a Monday (weekly off). Absent Sunday 7th and Tuesday 9th -> Monday 8th becomes a sandwich absence.
    with db_conn.cursor() as cur:
        cur.execute("SELECT employee_key FROM dim_att_employee WHERE employee_name = %s", (name,))
        key = cur.fetchone()[0]
        for d in range(1, 31):
            dt = date(2026, 6, d)
            if dt.weekday() == 0 or d in (7, 9) or d == 10:
                continue
            cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                        "VALUES (%s, %s, %s, %s, %s, 480) ON CONFLICT DO NOTHING", (key, dt, time(9, 0), time(18, 0), "Present"))
        cur.execute("UPDATE fact_attendance SET final_status = 'Half Day' WHERE employee_key = %s AND work_date = %s", (key, date(2026, 6, 11)))
    db_conn.commit()
    adv = own(client, boss, "addAdvancePayment", name, 1500, "festival")
    assert adv["status"] == "ok" and own(client, boss, "addAdvancePayment", "Nobody", 10, "")["status"] == "error"
    rep = own(client, boss, "getSalaryReport", name, 2026, 6)
    sat = next(d for d in rep["days"] if d["day"] == 8)
    assert sat["status"] == "Absent (Sandwich)" and rep["stats"]["sandwich"] == 1
    # absent: 7, 9, 10 (+ sandwich 8) = 4, half day 11 = 0.5 -> 4.5 days off at 1000/day; advance only counts in its own month
    assert rep["totalDeductionDays"] == 4.5 and rep["deductionAmount"] == 4500
    assert rep["totalAdvance"] == 0 and rep["netSalary"] == 25500
    adv_list = own(client, boss, "getAllAdvances", name)["advances"]
    assert len(adv_list) == 1 and adv_list[0]["amount"] == 1500
    assert own(client, boss, "deleteAdvance", adv_list[0]["id"])["status"] == "ok" and own(client, boss, "deleteAdvance", adv_list[0]["id"])["status"] == "error"

    # PINs: stored hashed and never readable
    assert own(client, boss, "setEmployeePIN", name, "12")["status"] == "error" and own(client, boss, "setEmployeePIN", name, "12ab")["status"] == "error"
    assert own(client, boss, "setEmployeePIN", name, "4321")["action"] == "set"
    pins = own(client, boss, "getAllPINs")["pins"]
    assert [p["pin"] for p in pins if p["name"] == name] == ["••••"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT pin_hash FROM dim_att_employee WHERE employee_name = %s", (name,))
        assert cur.fetchone()[0] not in (None, "4321")
    # the employee page no longer has a salary section: the PIN endpoints it used are gone
    assert client.post("/attendance/public/rpc", json={"action": "verifyEmployeePIN", "args": [name, "4321"]}).status_code == 404
    assert client.post("/attendance/public/rpc", json={"action": "getEmployeeSalaryView", "args": [name, ""]}).status_code == 404


def test_reports_streaks_and_employee_list(client, db_conn, seed):
    boss = owner_user(client, db_conn)
    admin = login(client, db_conn, "admin")
    name = "Streak Person"
    client.post("/admin/masters/att_employee", json={"name": name, "is_active": True}, headers=admin)
    assert own(client, boss, "addEmployee", name, "x")["status"] == "error"                    # already there
    assert own(client, boss, "deleteEmployee", name)["status"] == "ok"
    assert name not in [e["name"] for e in pub(client, "getEmployees")]                          # removed people leave the check-in page
    assert own(client, boss, "addEmployee", name, "Packer")["status"] == "reactivated"
    month = own(client, boss, "getMonthlyReport", name, 2026, 6)
    assert month["status"] == "ok" and len(month["days"]) == 30 and month["stats"]["weeklyOff"] == 5 and month["stats"]["absent"] == 25
    streak = own(client, boss, "getEmployeeStreak", name)
    assert streak["workingDays"] == 30 and streak["currentStreak"] == 30 and streak["totalAbsent"] == 30
    absent = own(client, boss, "getAbsentStreak", 3)
    assert absent["workingDays"] == 3 and name in [e["name"] for e in absent["employees"]]
    # view-only owners can read but not change
    viewer = owner_user(client, db_conn, view_only=True)
    assert own(client, viewer, "getAllSalaries")["status"] == "ok"
    assert client.post("/attendance/owner/rpc", json={"action": "setEmployeeSalary", "args": ["p", name, 100]}, headers=viewer).status_code == 403


def test_link_switch_and_setup_lists(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    masters = {m["id"] for m in client.get("/admin/masters", headers=admin).json()}
    assert {"att_location", "att_employee"} <= masters
    places = client.get("/admin/masters/att_location", headers=admin).json()["rows"]
    godown = next(p for p in places if p["name"] == "Godown")
    assert godown["radius_m"] == 40 and abs(float(godown["latitude"]) - 28.7004323) < 1e-6
    # moving the radius changes who may check in (no code involved)
    r = client.put(f"/admin/masters/att_location/{godown['key']}", json={"name": "Godown", "latitude": 28.7004323, "longitude": 77.1288457,
                                                                         "radius_m": 5, "icon": "🏭"}, headers=admin)
    assert r.status_code == 200, r.text
    near = pub(client, "checkLocation", GODOWN[0] + 0.0002, GODOWN[1])       # about 22 m away
    assert near["status"] == "ok" and near["anywhere"] is True and near["site"] != "Godown"   # outside the radius, no work place set: anywhere
    client.put(f"/admin/masters/att_location/{godown['key']}", json={"name": "Godown", "latitude": 28.7004323, "longitude": 77.1288457,
                                                                     "radius_m": 40, "icon": "🏭"}, headers=admin)
    back = pub(client, "checkLocation", GODOWN[0] + 0.0002, GODOWN[1])
    assert back["status"] == "ok" and back["site"] == "Godown" and not back.get("anywhere")
    bad = client.post("/admin/masters/att_location", json={"name": "Moon", "latitude": 95, "longitude": 10, "radius_m": 40}, headers=admin)
    assert bad.status_code == 422
    # the open link can be switched off in Setup
    assert client.put("/admin/module-settings", json={"key": "att_employee_link_on", "value": 0}, headers=admin).status_code == 200
    assert client.post("/attendance/public/rpc", json={"action": "getEmployees", "args": []}).status_code == 403
    assert client.get("/attendance/public/status").json() == {"open": False}
    assert client.put("/admin/module-settings", json={"key": "att_late_after", "value": "25:00"}, headers=admin).status_code == 422
    assert client.put("/admin/module-settings", json={"key": "att_employee_link_on", "value": 1}, headers=admin).status_code == 200
    assert pub(client, "getEmployees")


def test_attendance_employees_follow_the_portal_members(client, db_conn):
    import uuid
    from app import auth
    u = uuid.uuid4().hex[:6]
    name = f"Member {u}"
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s, %s, 'shop', %s, false) RETURNING user_key", (f"m_{u}", auth.hash_password("Pw-12345678"), name))
        key = cur.fetchone()[0]
        cur.execute("INSERT INTO dim_att_employee (employee_name, role_title) VALUES (%s, 'Floor') ON CONFLICT DO NOTHING", (f"Floor {u}",))
    db_conn.commit()
    attendance.sync_members(force=True)
    names = [e["name"] for e in pub(client, "getEmployees")]
    assert name in names and f"Floor {u}" in names                     # members and floor staff share one dropdown
    with db_conn.cursor() as cur:                                       # a rename follows, a disabled member drops out
        cur.execute("UPDATE dim_user SET display_name = %s WHERE user_key = %s", (name + " Renamed", key))
    db_conn.commit()
    attendance.sync_members(force=True)
    assert name + " Renamed" in [e["name"] for e in pub(client, "getEmployees")]
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dim_user SET password_hash = %s WHERE user_key = %s", (auth.DISABLED_HASH, key))
    db_conn.commit()
    attendance.sync_members(force=True)
    assert name + " Renamed" not in [e["name"] for e in pub(client, "getEmployees")]
