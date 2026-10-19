"""Delegation / Checklist notifications (WhatsApp GROUPS + the web bell), the Setup > WhatsApp screen (formats, sample, test), editing tasks."""
import json
from datetime import timedelta

import pytest

from app import modcommon, notify, whatsapp
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
        j = json.loads(body)
        sent.append({"to": j["recipientIds"][0], "text": j["message"][0], "headers": headers})
        return 200, '{"success": true}'

    monkeypatch.setattr(whatsapp, "_http_post", fake_post)
    return sent


def alert_row(client, admin, key, group="", enabled=True, template=None):
    body = {"group_id": group, "enabled": enabled, "template": template or whatsapp.ALERTS[key]["template"]}
    r = client.put(f"/admin/whatsapp/{key}", json=body, headers=admin)
    assert r.status_code == 200, r.text
    return body


def notes(client, headers):
    return client.get("/notifications", headers=headers).json()


def assign(client, mgr, staff_id, desc, due):
    return client.post("/delegation/tasks", json={"staffId": staff_id, "desc": desc, "dueDate": due.isoformat()}, headers=mgr).json()


def groups(sent):
    return [s["to"] for s in sent]


def clear_groups(client, admin):
    for k in ("deleg_assigned", "deleg_done", "chk_assigned", "chk_done"):
        alert_row(client, admin, k, group="", enabled=(k != "chk_done"))


# ---------------------------------------------------------------- Setup > WhatsApp: formats, groups, sample
def test_every_alert_has_a_format_a_group_label_and_sample_values():
    for key, spec in whatsapp.ALERTS.items():
        assert spec["label"] and spec["group_label"] and spec["when"], key
        used = set(whatsapp.PLACEHOLDER.findall(spec["template"]))
        assert used <= set(spec["placeholders"]), (key, used - set(spec["placeholders"]))          # the standard wording only uses known placeholders
        assert set(spec["placeholders"]) <= set(spec["sample"]), (key, set(spec["placeholders"]) - set(spec["sample"]))   # every placeholder has a sample
        assert "{" not in whatsapp.sample_message(key), key                                          # nothing is left unfilled in the preview
        if spec.get("group_from"):
            assert spec["group_from"] in whatsapp.ALERTS and whatsapp.ALERTS[spec["group_from"]].get("group_from") != key


def test_setup_lists_formats_and_says_which_alerts_have_no_group(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    assert client.get("/admin/whatsapp", headers=mgr).status_code == 403
    for k in whatsapp.ALERTS:
        alert_row(client, admin, k, group="")
    alerts = {a["key"]: a for a in client.get("/admin/whatsapp", headers=admin).json()["alerts"]}
    assert set(alerts) == set(whatsapp.ALERTS)
    assert {"dispatch_godown", "dispatch_porter", "dispatch_receiving", "reports_evening", "reports_night", "deleg_assigned", "deleg_done", "chk_assigned", "chk_done"} <= set(alerts)
    assert all(not a["group_effective"] for a in alerts.values())
    assert alerts["dispatch_godown"]["group_label"] == "VT Dispatch group" and alerts["dispatch_porter"]["group_label"] == "Porter group"
    assert alerts["deleg_done"]["group_from"] == "deleg_assigned" and alerts["reports_night"]["group_from"] == "reports_evening"
    assert alerts["deleg_assigned"]["web_bell"] and not alerts["dispatch_godown"]["web_bell"]
    assert "Photo: {photo}" in alerts["dispatch_godown"]["template"] and "{cartage_line}" in alerts["dispatch_godown"]["template"]
    # a group typed on one alert is used by the alerts that fall back to it
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us")
    alerts = {a["key"]: a for a in client.get("/admin/whatsapp", headers=admin).json()["alerts"]}
    assert alerts["deleg_assigned"]["group_effective"] and alerts["deleg_assigned"]["group_source"] == "own"
    assert alerts["deleg_done"]["group_effective"] and alerts["deleg_done"]["group_source"] == "fallback" and alerts["deleg_done"]["group_id"] == ""
    assert not alerts["chk_assigned"]["group_effective"]
    assert whatsapp.get_alert("deleg_done")["group_id"] == "DELEG@g.us"
    alert_row(client, admin, "deleg_done", group="OTHER@g.us")                                        # its own group wins
    assert whatsapp.get_alert("deleg_done")["group_id"] == "OTHER@g.us" and whatsapp.get_alert("deleg_assigned")["group_id"] == "DELEG@g.us"
    # the saved wording is checked
    bad = {"group_id": "", "enabled": True, "template": "Hi {nope}"}
    assert client.put("/admin/whatsapp/deleg_done", json=bad, headers=admin).status_code == 422
    assert client.put("/admin/whatsapp/deleg_done", json={**bad, "template": "ok {name}", "group_id": "a b"}, headers=admin).status_code == 422


def test_sample_message_goes_to_the_group_typed_on_the_screen_in_the_format_typed(client, db_conn, seed, wa):
    admin = login(client, db_conn, "admin")
    for k in whatsapp.ALERTS:
        alert_row(client, admin, k, group="")
    assert "group not set" in client.post("/admin/whatsapp/dispatch_godown/test", headers=admin).json()["message"].lower()
    # nothing saved: the group and the wording typed on the screen are used as they are
    r = client.post("/admin/whatsapp/dispatch_godown/test", json={"group_id": "TRY@g.us", "template": "DC {dc_no} to {address}\n{cartage_line}By {delivered_by}"}, headers=admin).json()
    assert r["success"], r
    assert groups(wa) == ["TRY@g.us"] and "SAMPLE" in wa[0]["text"] and "DC 94 to Home: H-2/84,85 Sec-16 Rohini" in wa[0]["text"]
    assert "Cartage: 50" in wa[0]["text"] and "By Ranjeet(E-R) 8796637988" in wa[0]["text"] and wa[0]["headers"]["x-api-key"] == "key-1"
    assert whatsapp.get_alert("dispatch_godown")["group_id"] == ""                                       # and it was not saved
    # every alert's standard format can be sample-sent
    wa.clear()
    for k in whatsapp.ALERTS:
        assert client.post(f"/admin/whatsapp/{k}/test", json={"group_id": "G@g.us"}, headers=admin).json()["success"], k
    assert len(wa) == len(whatsapp.ALERTS) and all("SAMPLE" in s["text"] and "{" not in s["text"] for s in wa)
    # a blank group box tests the group already in use, including a fallback
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us")
    wa.clear()
    assert client.post("/admin/whatsapp/deleg_done/test", json={"group_id": ""}, headers=admin).json()["success"]
    assert groups(wa) == ["DELEG@g.us"]
    assert client.post("/admin/whatsapp/deleg_done/test", json={"template": "Hi {nope}"}, headers=admin).status_code == 422
    assert client.post("/admin/whatsapp/dispatch_godown/test", headers=user(client, db_conn, "manager")[0]).status_code == 403


# ---------------------------------------------------------------- Delegation / Checklist go to their groups
def test_delegation_and_checklist_messages_go_to_their_own_group(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, mname = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    cstaff, cname = user(client, db_conn, "cashier")
    grant(db_conn, mname, "chk_manage")
    grant(db_conn, cname, "chk_mine")
    clear_groups(client, admin)
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us")                 # one group for Delegation: "completed" falls back to it
    alert_row(client, admin, "chk_assigned", group="CHK@g.us")                      # one group for Checklist

    assert "Task assigned" in assign(client, mgr, sname, "Count the stock", days + timedelta(days=2))
    assert groups(wa) == ["DELEG@g.us"] and "Count the stock" in wa[0]["text"] and "New task" in wa[0]["text"] and "T-" in wa[0]["text"]
    assert (days + timedelta(days=2)).strftime("%d-%m-%Y") in wa[0]["text"] and wa[0]["headers"]["x-api-key"] == "key-1"
    wa.clear()
    t = next(x for x in client.get("/delegation/tasks", headers=mgr).json() if x["taskDesc"] == "Count the stock")
    assert "+10" in client.post(f"/delegation/tasks/{t['taskId']}/complete", json={"notes": "all counted"}, headers=staff).json()
    assert groups(wa) == ["DELEG@g.us"] and "Task completed" in wa[0]["text"] and "on time" in wa[0]["text"] and "all counted" in wa[0]["text"]

    wa.clear()
    body = {"title": "Open the shop", "assignee": cname, "frequency": "Once", "startDate": days.isoformat(), "category": "Shop / Godown routine"}
    assert client.post("/checklist/checklists", json=body, headers=mgr).status_code == 201
    assert groups(wa) == ["CHK@g.us"] and "Open the shop" in wa[0]["text"] and "One time" in wa[0]["text"]
    wa.clear()
    item = client.get("/checklist/mine", headers=cstaff).json()["dueToday"][0]
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=cstaff).status_code == 200
    assert wa == []                                                                # "item marked" starts off: a daily list would flood the group
    client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=mgr)
    alert_row(client, admin, "chk_done", group="", enabled=True)                    # switched on, no group of its own: the Checklist group
    assert client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Done"}, headers=cstaff).status_code == 200
    assert groups(wa) == ["CHK@g.us"] and "Checklist item Done" in wa[0]["text"] and "Open the shop" in wa[0]["text"]
    wa.clear()
    client.post(f"/checklist/occurrences/{item['key']}/mark", json={"status": "Pending"}, headers=mgr)
    assert wa == []                                                                # re-opening an item is not an event

    # handing the checklist to someone else tells the group again; a rename for the same person does not
    other, oname = user(client, db_conn, "cashier")
    grant(db_conn, oname, "chk_mine")
    key = client.get("/checklist/checklists", headers=mgr).json()[0]["key"]
    assert client.put(f"/checklist/checklists/{key}", json={"title": "Open the shop", "assignee": oname, "frequency": "Once"}, headers=mgr).status_code == 200
    assert groups(wa) == ["CHK@g.us"] and oname.title() in wa[0]["text"] and notes(client, other)["unread"] == 1
    wa.clear()
    assert client.put(f"/checklist/checklists/{key}", json={"title": "Open the shop (renamed)", "assignee": oname, "frequency": "Once"}, headers=mgr).status_code == 200
    assert wa == []


def test_switched_off_or_group_less_alerts_send_no_whatsapp_but_the_bell_follows_the_switch(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    clear_groups(client, admin)
    assign(client, mgr, sname, "No group yet", days + timedelta(days=2))
    assert wa == [] and notes(client, staff)["unread"] == 1                          # the bell does not need a group
    log = client.get("/notifications/log", headers=admin).json()
    assert any(r["channel"] == "whatsapp" and not r["ok"] and "group not set" in r["detail"].lower() for r in log)
    assert client.get("/notifications/log", headers=mgr).status_code == 403
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us", enabled=False)
    before = notes(client, staff)["unread"]
    assign(client, mgr, sname, "Switched off", days + timedelta(days=2))
    assert wa == [] and notes(client, staff)["unread"] == before                     # off = no WhatsApp and no bell


def test_a_provider_failure_never_breaks_giving_the_task(client, db_conn, days, seed, wa, monkeypatch):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    staff, sname = user(client, db_conn, "staff")
    clear_groups(client, admin)
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us")

    def boom(url, body, headers):
        raise OSError("provider down")

    monkeypatch.setattr(whatsapp, "_http_post", boom)
    assert "Task assigned" in assign(client, mgr, sname, "Still saved", days + timedelta(days=2))
    assert any(t["taskDesc"] == "Still saved" for t in client.get("/delegation/tasks", headers=staff).json())
    assert notes(client, staff)["unread"] == 1
    assert any(not r["ok"] and "provider down" in r["detail"] for r in client.get("/notifications/log", headers=admin).json())
    monkeypatch.setattr(whatsapp, "_http_post", lambda u, b, h: (200, '{"success": false, "error": "bad group"}'))     # HTTP 200 but refused
    assign(client, mgr, sname, "Refused by provider", days + timedelta(days=2))
    assert any(not r["ok"] and r["channel"] == "whatsapp" and "bad group" in r["detail"] for r in client.get("/notifications/log", headers=admin).json())


def test_bell_goes_to_the_person_and_every_admin_but_not_the_one_who_acted(client, db_conn, days, seed, wa):
    admin = login(client, db_conn, "admin")
    mgr, _ = user(client, db_conn, "manager")
    one, n1 = user(client, db_conn, "staff")
    two, n2 = user(client, db_conn, "staff")
    clear_groups(client, admin)
    before = notes(client, admin)["unread"]
    for n, d in ((n1, "A"), (n1, "B"), (n2, "C")):
        assign(client, mgr, n, d, days + timedelta(days=2))
    inbox = notes(client, one)
    assert inbox["unread"] == 2 and len(inbox["items"]) == 2 and inbox["items"][0]["module"] == "delegation"
    assert notes(client, admin)["unread"] == before + 3 and notes(client, mgr)["unread"] == 0     # every admin; not the manager who gave them
    first = inbox["items"][0]["id"]
    assert client.post(f"/notifications/{first}/read", headers=two).status_code == 404          # someone else's
    assert client.post(f"/notifications/{first}/read", headers=one).status_code == 200
    assert notes(client, one)["unread"] == 1
    assert client.post("/notifications/read-all", headers=one).status_code == 200
    assert notes(client, one)["unread"] == 0 and notes(client, two)["unread"] == 1
    assert client.get("/notifications").status_code in (401, 403)
    # the admin giving a task is not told about their own action
    mark = notes(client, admin)["unread"]
    assign(client, admin, n2, "By the admin", days + timedelta(days=2))
    assert notes(client, admin)["unread"] == mark and notes(client, two)["unread"] == 2


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
    clear_groups(client, admin)
    alert_row(client, admin, "deleg_assigned", group="DELEG@g.us")
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
    assert "given to" in msg and groups(wa) == ["DELEG@g.us"] and "Move me" in wa[0]["text"]
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
    text = str(client.get("/admin/access/matrix", headers=admin).json())
    assert "deleg_edit" in text and "Edit tasks" in text
