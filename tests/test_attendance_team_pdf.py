"""The owner dashboard's "All employees (PDF)" for the monthly attendance report and the salary slips."""
import io
import zipfile
from datetime import date, time

from pypdf import PdfReader

from app import attendance
from tests.test_attendance import own, owner_user
from tests.test_modules import user  # noqa: F401
from tests.test_o2d_screens import client, login, seed  # noqa: F401  (fixtures)


def _team(client, admin, *names):
    for n in names:
        r = client.post("/admin/masters/att_employee", json={"name": n, "role_title": "Staff", "is_active": True, "skip_whatsapp": True}, headers=admin)
        assert r.status_code == 201, r.text


def _pdf_text(body: bytes) -> list[str]:
    return [p.extract_text() for p in PdfReader(io.BytesIO(body)).pages]


def test_attendance_pdf_one_page_per_employee_and_a_zip_of_separate_pdfs(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    _team(client, admin, "Pdf Alpha", "Pdf Beta")
    today = date.today()
    q = f"year={today.year}&month={today.month}"
    r = client.get(f"/attendance/owner/pdf?kind=attendance&{q}", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert 'filename="Attendance_All_Employees_' in r.headers["content-disposition"]
    pages = _pdf_text(r.content)
    alpha = [i for i, t in enumerate(pages) if "Pdf Alpha" in t]
    beta = [i for i, t in enumerate(pages) if "Pdf Beta" in t]
    assert alpha and beta and not set(alpha) & set(beta)          # each employee starts on a page of their own
    z = client.get(f"/attendance/owner/pdf?kind=attendance&format=zip&{q}", headers=admin)
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert any(n.startswith("Pdf_Alpha_Attendance_") for n in names) and any(n.startswith("Pdf_Beta_Attendance_") for n in names)
    one = zipfile.ZipFile(io.BytesIO(z.content)).read(next(n for n in names if n.startswith("Pdf_Alpha")))
    assert "Pdf Alpha" in "".join(_pdf_text(one)) and "Pdf Beta" not in "".join(_pdf_text(one))


def test_salary_pdf_shows_net_payable_and_says_when_salary_is_not_set(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    _team(client, admin, "Paid Person", "Unpaid Person")
    r = client.post("/attendance/owner/rpc", json={"action": "setEmployeeSalary", "args": ["p", "Paid Person", 30000]}, headers=admin)
    assert r.json()["status"] in ("ok", "updated"), r.text
    today = date.today()
    body = client.get(f"/attendance/owner/pdf?kind=salary&year={today.year}&month={today.month}", headers=admin)
    assert body.status_code == 200 and body.content.startswith(b"%PDF")
    text = "\n".join(_pdf_text(body.content))
    assert "30,000" in text and "Net Payable Salary" in text
    assert "Salary could not be calculated: Salary not set" in text      # stated on the page, not skipped or guessed


def test_pdf_needs_the_owner_dashboard_page_and_valid_inputs(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    nobody, _ = user(client, db_conn, "godown")
    assert client.get("/attendance/owner/pdf?kind=attendance&year=2026&month=10", headers=nobody).status_code == 403
    holder = owner_user(client, db_conn, view_only=True)       # reading a report is allowed with view-only access
    assert client.get("/attendance/owner/pdf?kind=attendance&year=2026&month=10", headers=holder).status_code == 200
    for bad in ("kind=nope&year=2026&month=10", "kind=salary&year=2026&month=13", "kind=salary&year=2026&month=10&format=exe"):
        assert client.get(f"/attendance/owner/pdf?{bad}", headers=admin).status_code == 422


def test_salary_slip_percentages_sit_under_each_card_in_the_screen_and_the_pdf(client, db_conn, seed):
    # the base is the days that count towards pay: weekly offs and days still to come are left out, sandwich days are already inside absent
    counted, pct = attendance.stat_percentages({"present": 12, "late": 1, "halfDay": 1, "absent": 15, "sandwich": 2, "incomplete": 0}, 2500, 25000)
    assert counted == 29 and pct["present"] == 41.4 and pct["late"] == 3.4 and pct["absent"] == 51.7 and pct["sandwich"] == 6.9 and pct["advance"] == 10.0
    assert attendance.stat_percentages({"present": 0, "late": 0, "halfDay": 0, "absent": 0, "sandwich": 0, "incomplete": 0})[1]["present"] == 0.0   # no days: no division

    admin = login(client, db_conn, "admin")
    boss = owner_user(client, db_conn)
    name = "Percent Person"
    _team(client, admin, name)
    assert own(client, boss, "setEmployeeSalary", name, 30000)["action"] == "set"
    with db_conn.cursor() as cur:
        cur.execute("SELECT employee_key FROM dim_att_employee WHERE employee_name = %s", (name,))
        key = cur.fetchone()[0]
        for d in range(2, 28):                      # June 2026: Mondays are the weekly off; 9th and 10th absent, 11th a half day
            dt = date(2026, 6, d)
            if dt.weekday() == 0 or d in (9, 10):
                continue
            cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, check_out_time, final_status, hours_worked_min) "
                        "VALUES (%s, %s, %s, %s, %s, 480) ON CONFLICT DO NOTHING", (key, dt, time(9, 0), time(18, 0), "Half Day" if d == 11 else "Present"))
    db_conn.commit()
    rep = own(client, boss, "getSalaryReport", name, 2026, 6)
    s, pc = rep["stats"], rep["percent"]
    assert rep["countedDays"] == s["present"] + s["late"] + s["halfDay"] + s["absent"] + s["incomplete"] > 0
    assert pc["present"] == round(s["present"] * 100 / rep["countedDays"], 1) and pc["halfDay"] > 0 and pc["absent"] > 0
    text = "\n".join(_pdf_text(client.get("/attendance/owner/pdf?kind=salary&year=2026&month=6", headers=admin).content))
    page = text[text.index(name):]
    for key_ in ("present", "halfDay", "absent"):
        assert f"{pc[key_]:.1f}%" in page                                    # the % under each card
    assert f"share of the {rep['countedDays']} days that count towards pay" in page
    assert "Attendance Calendar - June 2026" in page and "Net Payable Salary" in page
