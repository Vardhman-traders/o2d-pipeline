# ruff: noqa: F811, E501
"""Attendance punches are checked for buddy punching: the selfie is compared with the employee's reference photos, a reused photo and one phone
marking two people are noticed. A doubtful punch is flagged and the admins are told - it is never blocked."""
import base64

import pytest

from app import attendance, face_match, notify
from tests.test_attendance import GODOWN, own, owner_user, pub
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


def photo(tag: str) -> str:
    """A fake 'photo': its bytes say who is in it (the face model is replaced by fake_faces below)."""
    return "data:image/jpeg;base64," + base64.b64encode(tag.encode()).decode()


@pytest.fixture()
def fake_faces(monkeypatch):
    def embed(data: bytes):
        text = data.decode(errors="ignore")
        if text.startswith("NOFACE"):
            return "no_face", None
        return "ok", [1.0, 0.0, 0.0] if text.startswith("ALICE") else [0.0, 1.0, 0.0]

    monkeypatch.setattr(face_match, "embed", embed)
    monkeypatch.setattr(face_match, "check_enrolment", lambda data: (embed(data)[1], "") if not data.startswith(b"BAD") else (None, "No face found. Use a clear, front-facing photo in good light."))
    monkeypatch.setattr(face_match, "available", lambda: True)
    told = []
    monkeypatch.setattr(attendance, "_tell_review", lambda name, kind, when, day, reason: told.append((name, kind, reason)))
    return told


def _employee(client, db_conn, name):
    admin = login(client, db_conn, "admin")
    assert client.post("/admin/masters/att_employee", json={"name": name, "role_title": "T", "is_active": True, "skip_whatsapp": True}, headers=admin).status_code == 201
    return admin


def _in(client, name, image, device=None):
    device = f"phone-of-{name}" if device is None else device
    return pub(client, "saveAttendance", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1], "photo": image, "device": device})


def _out(client, name, image, device=None):
    device = f"phone-of-{name}" if device is None else device
    return pub(client, "saveCheckout", {"name": name, "lat": GODOWN[0], "lng": GODOWN[1], "photo": image, "device": device})


def _check(db_conn, name, kind):
    with db_conn.cursor() as cur:
        cur.execute("SELECT c.face_status, c.face_score, c.flags, c.review_result, c.device_hash, c.photo_sha256 FROM att_punch_check c "
                    "JOIN fact_attendance a ON a.attendance_key = c.attendance_key JOIN dim_att_employee e ON e.employee_key = a.employee_key "
                    "WHERE e.employee_name = %s AND c.kind = %s", (name, kind))
        return cur.fetchone()


def test_a_punch_without_a_selfie_is_refused(client, db_conn, seed):
    _employee(client, db_conn, "Face Nophoto")
    r = pub(client, "saveAttendance", {"name": "Face Nophoto", "lat": GODOWN[0], "lng": GODOWN[1]})
    assert r["status"] == "error" and "selfie" in r["msg"]
    assert pub(client, "getTodayStatus", "Face Nophoto")["checkedIn"] is False


def test_a_selfie_that_matches_is_not_flagged_and_one_that_does_not_is_flagged_but_still_counts(client, db_conn, seed, fake_faces):
    admin = _employee(client, db_conn, "Face Alice")
    boss = owner_user(client, db_conn)
    assert own(client, boss, "addEmployeeFace", "Face Alice", photo("ALICE-reference"))["status"] == "ok"
    assert own(client, boss, "addEmployeeFace", "Face Alice", photo("BAD-no-face"))["status"] == "error"
    assert own(client, boss, "getEmployeeFaces", "Face Alice")["faces"][0]["addedOn"]

    assert _in(client, "Face Alice", photo("ALICE-morning"))["status"] == "ok"
    face, score, flags, review, _, _ = _check(db_conn, "Face Alice", "in")
    assert face == "match" and score == pytest.approx(1.0) and flags == "" and review is None and fake_faces == []

    out = _out(client, "Face Alice", photo("OTHER-evening"))             # someone else's face: the punch still counts ...
    assert out["status"] == "ok" and pub(client, "getTodayStatus", "Face Alice")["checkedOut"] is True
    face, score, flags, _, _, _ = _check(db_conn, "Face Alice", "out")
    assert face == "mismatch" and flags == "face"
    assert fake_faces and fake_faces[0][0] == "Face Alice" and fake_faces[0][1] == "out" and "does not look like" in fake_faces[0][2]   # ... and the admins are told

    today = own(client, boss, "getTodayAttendance")
    row = next(m for m in today["marked"] if m["name"] == "Face Alice")
    assert row["checks"]["in"]["face"] == "match" and row["checks"]["out"]["flags"] == ["face"] and row["checks"]["out"]["review"] == ""
    pending = own(client, boss, "getPunchReviews", 14)
    item = next(i for i in pending["items"] if i["name"] == "Face Alice")
    assert pending["pending"] >= 1 and item["kind"] == "out" and "Face does not match" in item["reason"] and item["review"] == ""

    assert own(client, boss, "reviewPunch", item["attendanceKey"], "out", "not_same")["status"] == "ok"
    assert own(client, boss, "reviewPunch", item["attendanceKey"], "in", "ok")["status"] == "error"          # that punch was not flagged
    assert own(client, boss, "reviewPunch", item["attendanceKey"], "out", "maybe")["status"] == "error"
    assert _check(db_conn, "Face Alice", "out")[3] == "not_same"
    after = next(i for i in own(client, boss, "getPunchReviews", 14)["items"] if i["name"] == "Face Alice")
    assert after["review"] == "not_same" and after["reviewedBy"]
    # a view-only owner may look but not decide
    viewer = owner_user(client, db_conn, view_only=True)
    assert client.post("/attendance/owner/rpc", json={"action": "reviewPunch", "args": ["p", item["attendanceKey"], "out", "ok"]}, headers=viewer).status_code == 403
    assert client.post("/attendance/owner/rpc", json={"action": "addEmployeeFace", "args": ["p", "Face Alice", photo("ALICE-x")]}, headers=viewer).status_code == 403
    del admin


def test_no_face_in_the_selfie_is_flagged_and_an_employee_without_reference_photos_is_only_noted(client, db_conn, seed, fake_faces):
    boss = owner_user(client, db_conn)
    _employee(client, db_conn, "Face Noref")
    assert _in(client, "Face Noref", photo("ALICE-any"))["status"] == "ok"
    face, score, flags, _, _, _ = _check(db_conn, "Face Noref", "in")
    assert face == "not_enrolled" and score is None and flags == "" and fake_faces == []        # nothing to compare with: not a flag
    _employee(client, db_conn, "Face Blank")
    assert own(client, boss, "addEmployeeFace", "Face Blank", photo("ALICE-ref2"))["status"] == "ok"
    assert _in(client, "Face Blank", photo("NOFACE-wall"))["status"] == "ok"
    assert _check(db_conn, "Face Blank", "in")[:3] == ("no_face", None, "face") and "no face" in fake_faces[-1][2]


def test_reference_photos_are_limited_and_can_be_deleted(client, db_conn, seed, fake_faces):
    boss = owner_user(client, db_conn)
    _employee(client, db_conn, "Face Many")
    for i in range(attendance.FACE_PHOTOS_MAX):
        assert own(client, boss, "addEmployeeFace", "Face Many", photo(f"ALICE-{i}"))["status"] == "ok"
    over = own(client, boss, "addEmployeeFace", "Face Many", photo("ALICE-over"))
    assert over["status"] == "error" and "At most" in over["msg"]
    faces = own(client, boss, "getEmployeeFaces", "Face Many")["faces"]
    assert len(faces) == attendance.FACE_PHOTOS_MAX and own(client, boss, "deleteEmployeeFace", faces[0]["id"])["status"] == "ok"
    assert own(client, boss, "deleteEmployeeFace", faces[0]["id"])["status"] == "error"
    mine = {e["name"]: e for e in own(client, boss, "getAllEmployees")["employees"]}["Face Many"]
    assert mine["faces"] == attendance.FACE_PHOTOS_MAX - 1


def test_the_same_phone_for_two_people_and_a_reused_photo_are_flagged(client, db_conn, seed, fake_faces, monkeypatch):
    monkeypatch.setenv("VT_PHOTO_DUPLICATE_CHECK", "on")
    _employee(client, db_conn, "Face Phone One")
    _employee(client, db_conn, "Face Phone Two")
    assert _in(client, "Face Phone One", photo("ALICE-shared-phone-1"), device="shared-phone-aaaa")["status"] == "ok"
    assert _check(db_conn, "Face Phone One", "in")[2] == ""
    assert _in(client, "Face Phone Two", photo("ALICE-shared-phone-2"), device="shared-phone-aaaa")["status"] == "ok"
    assert _check(db_conn, "Face Phone Two", "in")[2] == "device" and "Face Phone One" in fake_faces[-1][2]
    assert _in(client, "Face Phone One", photo("x"), device="")["status"] == "already_marked"
    # the same picture sent for someone else
    _employee(client, db_conn, "Face Photo One")
    _employee(client, db_conn, "Face Photo Two")
    assert _in(client, "Face Photo One", photo("ALICE-same-picture"), device="phone-photo-one-1")["status"] == "ok"
    assert _in(client, "Face Photo Two", photo("ALICE-same-picture"), device="phone-photo-two-2")["status"] == "ok"
    assert _check(db_conn, "Face Photo Two", "in")[2] == "photo" and "already used" in fake_faces[-1][2]
    # the phone id is stored hashed, never as typed
    assert _check(db_conn, "Face Photo One", "in")[4] != "phone-photo-one-1" and len(_check(db_conn, "Face Photo One", "in")[4]) == 64


def test_the_checks_can_be_switched_off_in_setup(client, db_conn, seed, fake_faces):
    admin = _employee(client, db_conn, "Face Off")
    boss = owner_user(client, db_conn)
    assert own(client, boss, "addEmployeeFace", "Face Off", photo("ALICE-ref-off"))["status"] == "ok"
    for key in ("att_face_check_on", "att_device_check_on"):
        assert client.put("/admin/module-settings", json={"key": key, "value": 0}, headers=admin).status_code == 200
    try:
        assert _in(client, "Face Off", photo("OTHER-off"), device="phone-off-0000001")["status"] == "ok"
        face, _, flags, _, _, _ = _check(db_conn, "Face Off", "in")
        assert face == "unavailable" and flags == ""
    finally:
        for key in ("att_face_check_on", "att_device_check_on"):
            client.put("/admin/module-settings", json={"key": key, "value": 1}, headers=admin)


def test_a_flagged_punch_reaches_the_admins_bell_and_the_group_message(client, db_conn, seed, monkeypatch):
    from datetime import date
    monkeypatch.setattr(notify, "BACKGROUND", False)
    sent = []
    monkeypatch.setattr("app.whatsapp.send_text", lambda key, text, post=None, ignore_switch=False: sent.append((key, text)) or {"skipped": True, "reason": "test"})
    login(client, db_conn, "admin")
    attendance._tell_review("Bell Person", "in", "09:12 AM", date(2026, 11, 2), "the selfie does not look like Bell Person's reference photos (score 0.12)")
    assert sent and sent[0][0] == "att_review" and "Bell Person" in sent[0][1] and "Check-in" in sent[0][1] and "reference photos" in sent[0][1]
    with db_conn.cursor() as cur:
        cur.execute("SELECT module, title FROM app_notification WHERE title LIKE 'Attendance to review: Bell Person' ORDER BY notification_key DESC LIMIT 1")
        row = cur.fetchone()
    assert row and row[0] == "attendance"


def test_the_caller_address_is_the_one_the_proxy_added():
    from starlette.requests import Request

    def req(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1234), "method": "POST", "path": "/"})

    assert attendance._client_ip(req("203.0.113.7")) == "203.0.113.7"
    assert attendance._client_ip(req("6.6.6.6, 203.0.113.7")) == "203.0.113.7"            # what the caller typed first is ignored
    assert attendance._client_ip(req("")) == "10.0.0.9"


def test_the_face_models_do_not_change_what_the_math_says():
    assert face_match.similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert face_match.similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert face_match.similarity([0, 0, 0], [1, 0, 0]) == 0.0
