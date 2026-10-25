"""An employee with no work place set may mark attendance from anywhere; one who is tied to a work place still may not."""
from tests.test_checklist_expenses_places import GODOWN, SHOP, pub
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)

FAR = (19.0760, 72.8777)   # Mumbai: nowhere near either work place


def _employee(client, admin, name):
    r = client.post("/admin/masters/att_employee", json={"name": name, "role_title": "T", "is_active": True, "skip_whatsapp": True}, headers=admin)
    assert r.status_code == 201, r.text


def test_untagged_employee_marks_from_anywhere_and_the_row_has_no_work_place(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    _employee(client, admin, "Roaming Person")
    check = pub(client, "checkLocation", *FAR, "Roaming Person")
    assert check["status"] == "ok" and check["anywhere"] is True and "Anywhere" in check["site"]
    saved = pub(client, "saveAttendance", {"name": "Roaming Person", "lat": FAR[0], "lng": FAR[1]})
    assert saved["status"] == "ok" and "Anywhere" in saved["site"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT a.location_key FROM fact_attendance a JOIN dim_att_employee e ON e.employee_key = a.employee_key "
                    "WHERE e.employee_name = 'Roaming Person'")
        assert [r[0] for r in cur.fetchall()] == [None]
    out = pub(client, "saveCheckout", {"name": "Roaming Person", "lat": FAR[0], "lng": FAR[1]})
    assert out["status"] == "ok"


def test_a_phone_position_is_still_needed_even_when_anywhere(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    _employee(client, admin, "No Gps Person")
    assert pub(client, "checkLocation", None, None, "No Gps Person")["status"] == "error"
    assert pub(client, "saveAttendance", {"name": "No Gps Person"})["status"] == "error"


def test_tagged_employee_is_still_held_to_their_work_place(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    _employee(client, admin, "Fixed Person")
    r = client.post("/attendance/owner/rpc", json={"action": "setEmployeePlaces", "args": ["portal", "Fixed Person", ["Godown"]]}, headers=admin)
    assert r.json()["status"] == "ok"
    assert pub(client, "checkLocation", *FAR, "Fixed Person")["status"] == "wrong_location"
    assert pub(client, "saveAttendance", {"name": "Fixed Person", "lat": FAR[0], "lng": FAR[1]})["status"] == "wrong_location"
    assert pub(client, "saveAttendance", {"name": "Fixed Person", "lat": SHOP[0], "lng": SHOP[1]})["status"] == "wrong_location"
    assert pub(client, "saveAttendance", {"name": "Fixed Person", "lat": GODOWN[0], "lng": GODOWN[1]})["status"] == "ok"
