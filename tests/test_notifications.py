"""Delegation / Checklist notifications (WhatsApp to people + the web bell), who is told (Setup > WhatsApp), the EA, and editing tasks."""
from datetime import timedelta

import pytest

from app import attendance, modcommon, notify, whatsapp
from tests.test_checklist_expenses_places import grant
from tests.test_modules import days, user  # noqa: F401  (days: a fixture)
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


@pytest.fixture()
def wa(monkeypatch):
    """A WhatsApp provider that records what would be sent, and notifications sent inline so a test can look at the result."""
    monkeypatch.setenv("WHATSAPP_API_KEY", "key-1")
    monkeypatch.setattr(notify, "BACKGROUND", False)
    sent = []

    def fake_post(url, body, headers):
        import json
        sent.append({"to": json.loads(body)["recipientIds"], "text": json.loads(body)["message"][0], "headers": headers})
        return 200, '{"success": true}'

    monkeypatch.setattr(whatsapp, "_http_post", fake_post)
    return sent


def employee_of(db_conn, username, phone=None):
    """The attendance employee of a member (members are employees), optionally with a WhatsApp number."""
    attendance.sync_members(force=True)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dim_att_employee SET phone_number = %s WHERE user_key = (SELECT user_key FROM dim_user WHERE username = %s) RETURNING employee_key",
                    (phone, username))
        key = cur.fetchone()[0]
    db_conn.commit()
    return key


def setup_alert(client, admin, key, **kw):
    spec = whatsapp.ALERTS[key]
    body = {"group_id": "", "enabled": True, "template": spec["template"], "to_assignee": True, "to_ea": False, "to_admin": False,
            "ea_employee_key": None, "admin_numbers": "", **kw}
    r = client.put(f"/admin/whatsapp/{key}", json=body, headers=admin)
    assert r.status_code == 200, r.text
    return body


def notes(client, headers):
    return client.get("/notifications", headers=headers).json()


def assign(client, mgr, staff_id, desc, due):
    return client.post("/delegation/tasks", json={"staffId": staff_id, "desc": desc, "dueDate": due.isoformat()}, headers=mgr).json()


def phones(sent):
    return sorted(n for s in sent for n in s["to"])


# ---------------------------------------------------------------- phone numbers
def test_phone_numbers_are_normalised_and_typos_rejected():
    assert whatsapp.normalize_phone("98765 43210") == "919876543210"
    assert whatsapp.normalize_phone("+91-98765-43210") == "919876543210"
    assert whatsapp.normalize_phone("09876543210") == "919876543210"
    assert whatsapp.normalize_phone("0091 98765 43210") == "919876543210"
    assert whatsapp.normalize_phone("12345") is None and whatsapp.normalize_phone("abc") is None and whatsapp.normalize_phone("") is None
    assert whatsapp.split_numbers("9876543210, 9811122233;9876543210") == ["919876543210", "919811122233"]
    assert whatsapp.split_numbers("9876543210, nope") is None and whatsapp.split_numbers("") == []


def test_employee_whatsapp_number_is_validated_when_saved(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    made = client.post("/admin/masters/att_employee", json={"name": "Phone Person", "phone_number": "98765 43210"}, headers=admin)
    assert made.status_code == 201, made.text
    rows = client.get("/admin/masters/att_employee", headers=admin).json()["rows"]
    row = next(r for r in rows if r["name"] == "Phone Person")
    assert row["phone_number"] == "919876543210"
    bad = client.put(f"/admin/masters/att_employee/{row['key']}", json={"name": "Phone Person", "phone_number": "12345"}, headers=admin)
    assert bad.status_code == 422 and "country code" in bad.text
    assert client.put(f"/admin/masters/att_employee/{row['key']}", json={"name": "Phone Person", "phone_number": ""}, headers=admin).status_code == 200


# ---------------------------------------------------------------- Setup > WhatsApp
def test_setup_whatsapp_saves_who_is_told_and_checks_the_numbers(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    listed = client.get("/admin/whatsapp", headers=admin).json()
    alerts = {a["key"]: a for a in listed["alerts"]}
    assert {"deleg_assigned", "deleg_done", "chk_assigned", "chk_done"} <= set(alerts)
    assert all(alerts[k]["personal"] for k in ("deleg_assigned", "deleg_done", "chk_assigned", "chk_done"))
    assert alerts["deleg_done"]["recipients"] == ["ea", "admin"] and alerts["chk_done"]["enabled"] is False      # the per-item message starts off
    assert "employees" in listed
    ea = employee_of(db_conn, user(client, db_conn, "staff")[1], "9811122233")
    spec = whatsapp.ALERTS["deleg_done"]
    body = {"group_id": "", "enabled": True, "template": spec["template"], "to_assignee": False, "to_ea": True, "to_admin": True,
            "ea_employee_key": ea, "admin_numbers": "98111 22244, +91 99999 88888"}
    assert client.put("/admin/whatsapp/deleg_done", json=body, headers=mgr).status_code == 403
    assert client.put("/admin/whatsapp/deleg_done", json={**body, "admin_numbers": "not a number"}, headers=admin).status_code == 422
    assert client.put("/admin/whatsapp/deleg_done", json={**body, "ea_employee_key": 99999999}, headers=admin).status_code == 422
    assert client.put("/admin/whatsapp/deleg_done", json=body, headers=admin).status_code == 200
    saved = {a["key"]: a for a in client.get("/admin/whatsapp", headers=admin).json()["alerts"]}["deleg_done"]
    assert saved["ea_employee_key"] == ea and saved["admin_numbers"] == "919811122244, 919999988888" and saved["to_ea"] and not saved["to_assignee"]


def test_setup_test_message_goes_to_the_ea_and_admin_numbers(client, db_conn, seed, wa):
    admin = login(client, db_conn, "admin")
    setup_alert(client, admin, "deleg_done", to_ea=True, to_admin=True)                  # nobody chosen yet
    assert "No number to test" in client.post("/admin/whatsapp/deleg_done/test", headers=admin).json()["message"]
    ea = employee_of(db_conn, user(client, db_conn, "staff")[1], "9811122233")
    setup_alert(client, admin, "deleg_done", to_ea=True, to_admin=True, ea_employee_key=ea, admin_numbers="9811122244")
    r = client.post("/admin/whatsapp/deleg_done/test", headers=admin).json()
    assert r["success"], r
    assert phones(wa) == ["919811122233", "919811122244"] and all("TEST message" in s["text"] for s in wa)
    assert all(s["headers"]["x-api-key"] == "key-1" for s in wa)                 # the same provider login as every other message


# ---------------------------------------------------------------- Delegation: task given
def test_task_given_tells_the_person_on_whatsapp_and_the_web(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    employee_of(db_conn, sname, "98765 43210")
    setup_alert(client, admin, "deleg_assigned")
    assert "Task assigned" in assign(client, mgr, sname, "Count the stock", days + timedelta(days=2))
    assert phones(wa) == ["919876543210"]
    assert "Count the stock" in wa[0]["text"] and (days + timedelta(days=2)).strftime("%d-%m-%Y") in wa[0]["text"] and "T-" in wa[0]["text"]
    inbox = notes(client, staff)
    assert inbox["unread"] == 1 and inbox["items"][0]["title"] == "New task: Count the stock"
    assert notes(client, staff)["items"][0]["module"] == "delegation"
    assert notes(client, mgr)["unread"] == 0                                      # nobody else was told (EA / admin are off)


def test_person_without_a_number_is_told_on_the_web_only_and_it_is_logged(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    employee_of(db_conn, sname, None)
    setup_alert(client, admin, "deleg_assigned")
    assign(client, mgr, sname, "No number task", days + timedelta(days=2))
    assert wa == [] and notes(client, staff)["unread"] == 1
    log = client.get("/notifications/log", headers=admin).json()
    assert any(r["channel"] == "whatsapp" and not r["ok"] and "No WhatsApp number" in r["detail"] for r in log)
    assert client.get("/notifications/log", headers=mgr).status_code == 403


def test_a_provider_failure_never_breaks_giving_the_task(client, db_conn, days, seed, wa, monkeypatch):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    employee_of(db_conn, sname, "9876543210")
    setup_alert(client, admin, "deleg_assigned")

    def boom(url, body, headers):
        raise OSError("provider down")

    monkeypatch.setattr(whatsapp, "_http_post", boom)
    assert "Task assigned" in assign(client, mgr, sname, "Still saved", days + timedelta(days=2))
    assert any(t["taskDesc"] == "Still saved" for t in client.get("/delegation/tasks", headers=staff).json())
    assert notes(client, staff)["unread"] == 1                                    # the web notification still arrived
    assert any(not r["ok"] and "provider down" in r["detail"] for r in client.get("/notifications/log", headers=admin).json())
    # an answer that is HTTP 200 but an error in the body is recorded as a failure too
    monkeypatch.setattr(whatsapp, "_http_post", lambda u, b, h: (200, '{"success": false, "error": "bad number"}'))
    assign(client, mgr, sname, "Rejected by provider", days + timedelta(days=2))
    assert any(not r["ok"] and r["channel"] == "whatsapp" and "bad number" in r["detail"] for r in client.get("/notifications/log", headers=admin).json())


# ---------------------------------------------------------------- Delegation: task completed
def test_completion_goes_to_the_ea_and_admin_and_each_can_be_switched_off(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    ea_headers, ea_name = user(client, db_conn, "staff")
    ea_key = employee_of(db_conn, ea_name, "98111 22233")
    employee_of(db_conn, sname, "9876543210")
    setup_alert(client, admin, "deleg_assigned", to_assignee=False)
    setup_alert(client, admin, "deleg_done", to_assignee=False, to_ea=True, to_admin=True, ea_employee_key=ea_key, admin_numbers="9811122244")

    def finish(desc):
        assign(client, mgr, sname, desc, days + timedelta(days=3))
        t = next(x for x in client.get("/delegation/tasks", headers=mgr).json() if x["taskDesc"] == desc)
        wa.clear()
        assert "+10" in client.post(f"/delegation/tasks/{t['taskId']}/complete", json={"notes": "all counted"}, headers=staff).json()
        return t

    finish("Both told")
    assert phones(wa) == ["919811122233", "919811122244"]                          # the EA and the admin number (not the person who did it)
    assert "Both told" in wa[0]["text"] and "on time" in wa[0]["text"] and "all counted" in wa[0]["text"]
    assert notes(client, ea_headers)["unread"] == 1 and notes(client, staff)["unread"] == 0     # the EA's own web screen too
    assert notes(client, admin)["unread"] == 1 and "Task completed by" in notes(client, admin)["items"][0]["title"]

    setup_alert(client, admin, "deleg_done", to_assignee=False, to_ea=False, to_admin=True, ea_employee_key=ea_key, admin_numbers="9811122244")
    finish("Admin only")
    assert phones(wa) == ["919811122244"]
    setup_alert(client, admin, "deleg_done", to_assignee=False, to_ea=True, to_admin=False, ea_employee_key=ea_key, admin_numbers="9811122244")
    finish("EA only")
    assert phones(wa) == ["919811122233"]
    setup_alert(client, admin, "deleg_done", enabled=False, to_ea=True, to_admin=True, ea_employee_key=ea_key, admin_numbers="9811122244")
    before = notes(client, admin)["unread"]
    finish("Switched off")
    assert wa == [] and notes(client, admin)["unread"] == before                    # switched off: no WhatsApp and no bell


def test_the_ea_who_is_also_the_person_hears_once(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    key = employee_of(db_conn, sname, "9876543210")
    setup_alert(client, admin, "deleg_assigned", to_assignee=True, to_ea=True, ea_employee_key=key)
    assign(client, mgr, sname, "Once only", days + timedelta(days=2))
    assert phones(wa) == ["919876543210"] and notes(client, staff)["unread"] == 1


def test_bell_read_state_belongs_to_each_person(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    one, n1 = user(client, db_conn, "staff")
    two, n2 = user(client, db_conn, "staff")
    setup_alert(client, admin, "deleg_assigned")
    for n, d in ((n1, "A"), (n1, "B"), (n2, "C")):
        assign(client, mgr, n, d, days + timedelta(days=2))
    inbox = notes(client, one)
    assert inbox["unread"] == 2 and len(inbox["items"]) == 2
    first = inbox["items"][0]["id"]
    assert client.post(f"/notifications/{first}/read", headers=two).status_code == 404      # someone else's
    assert client.post(f"/notifications/{first}/read", headers=one).status_code == 200
    assert notes(client, one)["unread"] == 1
    assert client.post("/notifications/read-all", headers=one).status_code == 200
    assert notes(client, one)["unread"] == 0 and notes(client, two)["unread"] == 1
    assert client.get("/notifications").status_code in (401, 403)


# ---------------------------------------------------------------- Checklist
def test_checklist_given_and_item_marked_are_separately_configurable(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "cashier")
    ea_headers, ea_name = user(client, db_conn, "staff")
    grant(db_conn, mname, "chk_manage")
    grant(db_conn, sname, "chk_mine")
    employee_of(db_conn, sname, "9876543210")
    ea_key = employee_of(db_conn, ea_name, "9811122233")
    setup_alert(client, admin, "chk_assigned")
    setup_alert(client, admin, "deleg_done", to_assignee=False, to_ea=True, to_admin=True, ea_employee_key=ea_key)     # the task alert: not for checklists
    body = {"title": "Open the shop", "assignee": sname, "frequency": "Once", "startDate": days.isoformat(), "category": "Shop / Godown routine"}
    assert client.post("/checklist/checklists", json=body, headers=mgr).status_code == 201
    assert phones(wa) == ["919876543210"] and "Open the shop" in wa[0]["text"] and "One time" in wa[0]["text"]
    assert notes(client, staff)["items"][0]["title"] == "New checklist: Open the shop" and notes(client, staff)["items"][0]["module"] == "checklist"

    # marking an item tells nobody until Setup switches the "item marked" alert on (it starts off)
    item = client.get("/checklist/mine", headers=staff).json()["dueToday"][0]
    wa.clear()
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=staff).status_code == 200
    assert wa == [] and notes(client, ea_headers)["unread"] == 0
    client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=mgr)
    setup_alert(client, admin, "chk_done", to_assignee=False, to_ea=True, to_admin=True, ea_employee_key=ea_key, admin_numbers="9811122244")
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=mgr).status_code == 200
    assert phones(wa) == ["919811122233", "919811122244"] and "Done" in wa[0]["text"] and "Open the shop" in wa[0]["text"]
    assert notes(client, ea_headers)["unread"] == 1
    wa.clear()
    client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=mgr)
    assert wa == []                                                               # re-opening an item is not an event

    # handing the checklist to someone else tells the new person
    other, oname = user(client, db_conn, "cashier")
    grant(db_conn, oname, "chk_mine")
    employee_of(db_conn, oname, "9822233344")
    wa.clear()
    key = client.get("/checklist/checklists", headers=mgr).json()[0]["key"]
    assert client.put(f"/checklist/checklists/{key}", json={"title": "Open the shop", "assignee": oname, "frequency": "Once"}, headers=mgr).status_code == 200
    assert phones(wa) == ["919822233344"] and notes(client, other)["unread"] == 1
    wa.clear()
    assert client.put(f"/checklist/checklists/{key}", json={"title": "Open the shop (renamed)", "assignee": oname, "frequency": "Once"}, headers=mgr).status_code == 200
    assert wa == []                                                               # same person: no new "given" message


# ---------------------------------------------------------------- editing tasks
def test_only_the_admin_and_people_given_the_page_can_edit_a_task(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    assign(client, mgr, sname, "Edit me", days + timedelta(days=3))
    t = next(x for x in client.get("/delegation/tasks", headers=mgr).json() if x["taskDesc"] == "Edit me")
    edit = lambda who, **kw: client.put(f"/delegation/tasks/{t['taskId']}", json={"staffId": sname, "desc": "Edit me", "dueDate": (days + timedelta(days=3)).isoformat(), **kw}, headers=who)  # noqa: E731

    assert client.get("/delegation/me", headers=admin).json()["canEditTasks"] is True
    assert client.get("/delegation/me", headers=mgr).json()["canEditTasks"] is False        # a manager is not an editor by default
    assert edit(mgr, desc="Changed").status_code == 403 and edit(staff, desc="Changed").status_code == 403
    assert "Task updated" in edit(admin, desc="Edited by admin").json()
    assert next(x for x in client.get("/delegation/tasks", headers=mgr).json() if x["taskId"] == t["taskId"])["taskDesc"] == "Edited by admin"

    grant(db_conn, mname, "deleg_edit", view_only=True)
    assert client.get("/delegation/me", headers=mgr).json()["canEditTasks"] is False        # View only is not enough
    assert edit(mgr, desc="Still no").status_code == 403
    grant(db_conn, mname, "deleg_edit")
    assert client.get("/delegation/me", headers=mgr).json()["canEditTasks"] is True
    assert "Task updated" in edit(mgr, desc="Edited by the manager").json()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM admin_audit_log WHERE action = 'delegation.edit' AND target = %s", (t["taskId"],))
        assert cur.fetchone()[0] == 2                                                     # every edit is in the audit log


def test_edit_rules_person_deadline_and_finished_tasks(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    one, n1 = user(client, db_conn, "staff")
    two, n2 = user(client, db_conn, "staff")
    employee_of(db_conn, n2, "9822233344")
    setup_alert(client, admin, "deleg_assigned")
    assign(client, mgr, n1, "Move me", days + timedelta(days=4))
    t = next(x for x in client.get("/delegation/tasks", headers=mgr).json() if x["taskDesc"] == "Move me")
    put = lambda **kw: client.put(f"/delegation/tasks/{t['taskId']}", json={"staffId": n1, "desc": "Move me", "dueDate": (days + timedelta(days=4)).isoformat(), **kw}, headers=admin).json()  # noqa: E731

    assert "Nothing was changed" in put()
    assert "required" in put(desc="  ")
    assert "cannot be moved into the past" in put(dueDate=(days - timedelta(days=2)).isoformat())
    assert "Task updated" in put(dueDate=(days + timedelta(days=9)).isoformat())
    assert next(x for x in client.get("/delegation/tasks", headers=admin).json() if x["taskId"] == t["taskId"])["dueDate"] == (days + timedelta(days=9)).isoformat()
    assert "cannot be given tasks" in put(staffId=user(client, db_conn, "manager")[1])               # not to a manager
    assert "Staff not found" in put(staffId="nobody")

    wa.clear()
    msg = put(staffId=n2)                                                                            # a pending task can be handed to someone else
    assert "given to" in msg and phones(wa) == ["919822233344"] and "Move me" in wa[0]["text"]
    assert notes(client, two)["unread"] == 1
    assert [x["taskId"] for x in client.get("/delegation/tasks", headers=two).json()] == [t["taskId"]] and client.get("/delegation/tasks", headers=one).json() == []

    # a finished task keeps its owner and its points
    assert "+10" in client.post(f"/delegation/tasks/{t['taskId']}/complete", json={}, headers=two).json()
    assert "still pending" in put(staffId=n1)
    assert "Task updated" in put(staffId=n2, desc="Reworded after completion")
    done = next(x for x in client.get("/delegation/tasks", headers=admin).json() if x["taskId"] == t["taskId"])
    assert done["score"] == 10 and done["taskDesc"] == "Reworded after completion" and done["assignedId"] == n2
    assert "Task Not Found" in client.put("/delegation/tasks/T-99999999", json={"staffId": n1, "desc": "x", "dueDate": days.isoformat()}, headers=admin).json()


def test_deleg_edit_is_a_page_the_admin_can_give_in_setup_access(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    pages = client.get("/admin/access/matrix", headers=admin).json()
    text = str(pages)
    assert "deleg_edit" in text and "Edit tasks" in text


def test_whoever_did_the_thing_is_not_told_about_it(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    mgr_key = employee_of(db_conn, mname, "9811100011")
    employee_of(db_conn, sname, "9876543210")
    # the manager is also the EA, and the admin is told: the manager gives the task and so is not told about their own action
    setup_alert(client, admin, "deleg_assigned", to_assignee=True, to_ea=True, to_admin=True, ea_employee_key=mgr_key)
    before = notes(client, admin)["unread"]
    assign(client, mgr, sname, "Own action", days + timedelta(days=2))
    assert phones(wa) == ["919876543210"]                                         # the person it is for; not the EA who gave it
    assert notes(client, mgr)["unread"] == 0 and notes(client, staff)["unread"] == 1
    assert notes(client, admin)["unread"] == before + 1                            # the admin did not do it, so is told
