# ruff: noqa: F811, E501
"""The O2D changes list of November: the 9:30 PM closing reports, VT Decor merge companies, VT Decor photo on edit, and the wrong-photo check at upload."""
from datetime import datetime

from app import attachments, daily_extra_reports, daily_reports, modcommon
from tests.test_decor import rpc, staff
from tests.test_modules import FakeStore
from tests.test_o2d_cashier import _dc
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)
from tests.test_order_journey import _dispatch, _status, people  # noqa: F401  (fixture + helpers)


def _storage(monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    attachments.set_storage(FakeStore())


# ------------------------------------------------------------------ the 9:30 PM closing batch
def test_closing_batch_builds_every_report_and_lists_them_in_the_message(client, people, db_conn, monkeypatch):
    h, today = people
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://vt.example.com")
    now = datetime.now(daily_reports.IST)
    _, stored = daily_reports.generate_batch(today, now, "closing")
    assert {r["kind"] for r in stored} == set(daily_extra_reports.KINDS)
    with db_conn.cursor() as cur:
        cur.execute("SELECT report_kind, pdf FROM daily_delivery_report WHERE report_date = %s AND report_kind = ANY(%s)", (today, list(daily_extra_reports.KINDS)))
        rows = cur.fetchall()
    assert len(rows) == len(daily_extra_reports.KINDS) and all(bytes(r[1]).startswith(b"%PDF") for r in rows)
    blocks = daily_reports.report_blocks(stored, "closing")
    assert len(blocks) == len(daily_extra_reports.KINDS) and all("https://vt.example.com/o2d/daily-reports/shared/" in b for b in blocks)
    assert "entr" in blocks[0] and "order" not in blocks[0]
    # they also show up on the Reports page for the day
    listed = client.get("/o2d/daily-reports", params={"date": today.isoformat()}, headers=h["admin"]).json()
    assert {r["kind"] for r in listed["reports"] if r["kind"]} >= set(daily_extra_reports.KINDS)


def test_attendance_reports_split_present_and_absent(client, db_conn, seed):
    from datetime import time as t
    day = modcommon.today_ist()
    admin = login(client, db_conn, "admin")
    for name in ("Closing Here", "Closing Away"):
        client.post("/admin/masters/att_employee", json={"name": name, "is_active": True}, headers=admin)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                    "SELECT employee_key, %s, %s, %s, 'Present', 480 FROM dim_att_employee WHERE employee_name = 'Closing Here' ON CONFLICT DO NOTHING",
                    (day, t(9, 0), t(18, 0)))
    db_conn.commit()
    present, absent, note = daily_extra_reports._attendance_rows(day)
    assert "Closing Here" in [r["name"] for r in present]
    if not note:                              # on the weekly off or a holiday nobody is absent
        assert "Closing Away" in [r["name"] for r in absent] and "Closing Here" not in [r["name"] for r in absent]
        away = next(r for r in absent if r["name"] == "Closing Away")
        assert away["in"] == "—" and away["out"] == "—" and away["status"] == "Absent"
    here = next(r for r in present if r["name"] == "Closing Here")
    assert here["in"] == "09:00 AM" and here["out"] == "06:00 PM" and here["status"] == "Present"


# ------------------------------------------------------------------ VT Decor money: only Oikos / Wallpaper / Texture
def test_decor_rows_can_only_be_merged_to_the_decor_companies(client, db_conn, seed):
    ensure_date(db_conn, modcommon.today_ist())
    owner = login(client, db_conn, "admin")
    asha, _ = staff(client, db_conn)
    code = rpc(client, asha, "addClient", name="Merge Company", mobile="9811122222", amount=50000, productType="Wallpaper")["clientId"]
    assert rpc(client, asha, "addPayment", clientId=code, stage="", amount=1000, mode="Cash")["ok"]
    q = client.get("/payments/merge", headers=owner).json()
    assert q["decorCompanies"] == ["Oikos", "Wallpaper", "Texture"]
    row = next(r for r in q["rows"] if r["kind"] == "decor_in" and r["dcNo"] == code)
    body = {"date": row["date"], "company": "IRIS", "party": "Merge Company", "cashAmount": 1000, "bankAmount": 0, "remarks": ""}
    refused = client.post(f"/payments/merge/{row['key']}/merge", json=body, headers=owner).json()
    assert refused["success"] is False and "Oikos, Wallpaper, Texture" in refused["message"]
    assert client.post(f"/payments/merge/{row['key']}/merge", json={**body, "company": "TEXTURE"}, headers=owner).json()["success"]


# ------------------------------------------------------------------ VT Decor: a photo added while editing an entry stays with it
def test_decor_photo_can_be_added_when_editing_a_payment_and_an_expense(client, db_conn, seed, monkeypatch):
    _storage(monkeypatch)
    try:
        asha, _ = staff(client, db_conn)
        code = rpc(client, asha, "addClient", name="Edit Photo", mobile="9811133333", amount=50000, productType="Wallpaper")["clientId"]
        assert rpc(client, asha, "addPayment", clientId=code, stage="", amount=500, mode="Cash")["ok"]
        assert rpc(client, asha, "addExpense", clientId=code, type="Transport", amount=200)["ok"]
        d = rpc(client, asha, "getClientDetail", clientId=code)
        pay, exp = d["payments"][0], d["expenses"][0]
        assert pay["photos"] == [] and exp["photos"] == []
        edit_pay = rpc(client, asha, "editPayment", clientId=code, rowIndex=pay["rowIndex"], amount=500, date=modcommon.today_ist().isoformat(), stage="", mode="Cash",
                       base64="/9j/4AAQSkZJRgEA", mimeType="image/jpeg")
        edit_exp = rpc(client, asha, "editExpense", clientId=code, rowIndex=exp["rowIndex"], amount=200, date=modcommon.today_ist().isoformat(), type="Transport",
                       base64="/9j/4AAQSkZJRgEB", mimeType="image/jpeg")
        assert edit_pay["ok"] and edit_exp["ok"]
        d = rpc(client, asha, "getClientDetail", clientId=code)
        assert len(d["payments"][0]["photos"]) == 1 and len(d["expenses"][0]["photos"]) == 1
    finally:
        attachments.set_storage(None)


# ------------------------------------------------------------------ the wrong-photo check at upload
def test_the_same_photo_cannot_be_attached_to_two_orders(client, people, db_conn, monkeypatch):
    monkeypatch.setenv("VT_PHOTO_DUPLICATE_CHECK", "on")
    h, today = people
    _storage(monkeypatch)
    try:
        ensure_date(db_conn, today)
        a, b = (add(client, h["shop"], today, _dc())["slNo"] for _ in range(2))
        for sl in (a, b):
            _status(client, h, sl, "Sent to the Company")
            _dispatch(client, h, "godown_dispatch", sl)
        hdr = {**h["godown_dispatch"], "Content-Type": "image/jpeg"}
        same = b"\xff\xd8closing-same-picture"
        assert client.post(f"/o2d/orders/{a}/photos?kind=dispatch", content=same, headers=hdr).status_code == 200
        twice = client.post(f"/o2d/orders/{b}/photos?kind=dispatch", content=same, headers=hdr)
        assert twice.status_code == 409 and "wrong photo" in twice.json()["detail"] and f"Sl {a}" in twice.json()["detail"]
        assert client.post(f"/o2d/orders/{b}/photos?kind=dispatch", content=b"\xff\xd8another-picture", headers=hdr).status_code == 200
    finally:
        attachments.set_storage(None)


def test_the_same_photo_cannot_be_used_for_two_decor_entries(client, db_conn, seed, monkeypatch):
    monkeypatch.setenv("VT_PHOTO_DUPLICATE_CHECK", "on")
    _storage(monkeypatch)
    try:
        asha, _ = staff(client, db_conn)
        code = rpc(client, asha, "addClient", name="Twice Photo", mobile="9811144444", amount=50000, productType="Wallpaper")["clientId"]
        first = rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Labour", amount=100, base64="/9j/4AAQSkZJRgEC", mimeType="image/jpeg")
        assert first["ok"] and not first.get("photoSkipped")
        second = rpc(client, asha, "addExpenseWithPhoto", clientId=code, type="Labour", amount=110, base64="/9j/4AAQSkZJRgEC", mimeType="image/jpeg")
        assert second["ok"] and second["photoSkipped"] and "wrong photo" in second["msg"]       # the entry is saved; the photo is refused with the reason
    finally:
        attachments.set_storage(None)
