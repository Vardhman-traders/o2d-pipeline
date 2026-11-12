"""The "closing" batch of the daily reports (default 9:30 PM IST, after the 9:00 PM auto check-out): one PDF each for

  att-present      Attendance - everyone present today: check-in, check-out, hours, status
  att-absent       Attendance - everyone absent today: check-in, check-out, status
  decor-payments   VT Decor - payments received today
  decor-expenses   VT Decor - expenses today
  purchase-godown  Purchase - Godown entries of the day
  purchase-shop    Purchase - Shop entries of the day
  checklist        Checklist - every employee's Done / Not done / Pending
  pay-cards        Payments dashboard - every card (today and overall) and the day's transactions
  regular-expenses Regular expenses - the whole page: balance, period cards, by category, by person, the statement

They are built on the server (the browser screens cannot run by themselves at night), stored with the other daily reports (Reports page, and
a download link in the WhatsApp message "Daily reports - closing"), and each one reads the same data and rules as the screen it mirrors.
A report that fails is logged and skipped; the others are still kept.
"""
import logging
from datetime import date, datetime

from . import attendance, checklist, db, expenses, o2d_reports, payments

log = logging.getLogger("daily_reports")

SYSTEM_USER = {"role": "admin", "username": "automatic daily report", "display_name": "Automatic daily report", "user_key": None,
               "must_change_password": False}
KINDS = ("att-present", "att-absent", "decor-payments", "decor-expenses", "purchase-godown", "purchase-shop", "checklist", "pay-cards",
         "regular-expenses")
COLOR = "#1b4f72"


def _clock(t) -> str:
    return attendance._to12h(attendance._hhmm(t)) if t else "—"


def _section(heading: str, columns: list, rows: list, totals: bool = False) -> dict:
    return {"heading": heading, "columns": columns, "rows": rows, "totals": totals}


# ------------------------------------------------------------------ attendance
_ATT_COLS = [("Employee", "name", 62, "L"), ("Role", "role", 44, "L"), ("Site", "site", 36, "L"), ("Check-in", "in", 28, "L"),
             ("Check-out", "out", 28, "L"), ("Hours", "hours", 24, "L"), ("Status", "status", 32, "L"), ("Note", "note", 22, "L")]


def _attendance_rows(day: date) -> tuple[list, list, str]:
    """(present rows, absent rows, a note when nobody can be absent that day)."""
    rules = attendance.Rules()
    with db.cursor() as cur:
        cur.execute(attendance.ROW_SQL + " WHERE a.work_date = %s ORDER BY a.check_in_time", (day,))
        recs = cur.fetchall()
        cur.execute("SELECT employee_key, employee_name, role_title FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)")
        emps = cur.fetchall()
        holidays = attendance.holidays_in(cur, day.year, day.month)
    roles = {e["employee_key"]: e["role_title"] or "" for e in emps}
    present, absent, seen = [], [], set()
    for r in recs:
        seen.add(r["employee_key"])
        status = rules.resolve(r["final_status"], attendance._mins(r["check_in_time"]))
        out = r["check_out_time"]
        row = {"name": r["employee_name"], "role": roles.get(r["employee_key"], ""), "site": r["location_name"] or "", "in": _clock(r["check_in_time"]),
               "out": _clock(out) if out else "—", "hours": attendance._hours_text(r["hours_worked_min"]) if out else "—", "status": status,
               "note": "Corrected" if r.get("corrected_at") else ""}
        (absent if status == "Absent" else present).append(row)
    off, holiday = rules.is_off(day), holidays.get(day.day)
    note = "Weekly off - nobody is marked absent." if off else f"Holiday ({holiday}) - nobody is marked absent." if holiday else ""
    if not note:
        absent += [{"name": e["employee_name"], "role": e["role_title"] or "", "site": "", "in": "—", "out": "—", "hours": "—", "status": "Absent", "note": ""}
                   for e in emps if e["employee_key"] not in seen]
    absent.sort(key=lambda x: x["name"].lower())
    return present, absent, note


def attendance_reports(day: date) -> list[dict]:
    present, absent, note = _attendance_rows(day)
    tag = f"{day:%d-%m-%Y}"
    return [{"kind": "att-present", "title": "Attendance - Present", "note": f"Employees present on {tag}",
             "sections": [_section(f"Present on {tag}", _ATT_COLS, present)]},
            {"kind": "att-absent", "title": "Attendance - Absent", "note": note or f"Employees absent on {tag}",
             "sections": [_section(f"Absent on {tag}", _ATT_COLS, absent)]}]


# ------------------------------------------------------------------ VT Decor
def decor_reports(day: date) -> list[dict]:
    tag = f"{day:%d-%m-%Y}"
    with db.cursor() as cur:
        cur.execute("""SELECT c.client_code, c.client_name, COALESCE(s.stage_name, p.stage_text, '') AS stage, COALESCE(m.mode_name, '') AS mode,
                              p.ref_no, p.amount, p.remarks, COALESCE(u.display_name, '') AS entered_by
                       FROM fact_decor_payment p JOIN dim_decor_client c ON c.client_key = p.client_key
                       LEFT JOIN dim_decor_stage s ON s.stage_key = p.stage_key LEFT JOIN dim_payment_mode m ON m.mode_key = p.mode_key
                       LEFT JOIN dim_user u ON u.user_key = p.created_by_user_key
                       WHERE p.pay_date = %s ORDER BY p.payment_key""", (day,))
        pays = cur.fetchall()
        cur.execute("""SELECT c.client_code, c.client_name, e.expense_type, e.amount, e.notes, COALESCE(u.display_name, '') AS entered_by
                       FROM fact_decor_expense e JOIN dim_decor_client c ON c.client_key = e.client_key
                       LEFT JOIN dim_user u ON u.user_key = e.created_by_user_key
                       WHERE e.exp_date = %s ORDER BY e.expense_key""", (day,))
        exps = cur.fetchall()
    pay_cols = [("Client code", "client_code", 26, "L"), ("Client", "client_name", 52, "L"), ("Stage", "stage", 44, "L"), ("Mode", "mode", 24, "L"),
                ("Ref no", "ref_no", 26, "L"), ("Amount (Rs)", "amount", 30, "R"), ("Remarks", "remarks", 48, "L"), ("Entered by", "entered_by", 26, "L")]
    exp_cols = [("Client code", "client_code", 28, "L"), ("Client", "client_name", 62, "L"), ("Expense type", "expense_type", 54, "L"),
                ("Amount (Rs)", "amount", 32, "R"), ("Notes", "notes", 62, "L"), ("Entered by", "entered_by", 30, "L")]
    return [{"kind": "decor-payments", "title": "VT Decor - Payments", "note": f"Payments received on {tag}",
             "sections": [_section(f"VT Decor payments received on {tag}", pay_cols, pays, True)]},
            {"kind": "decor-expenses", "title": "VT Decor - Expenses", "note": f"Expenses on {tag}",
             "sections": [_section(f"VT Decor expenses on {tag}", exp_cols, exps, True)]}]


# ------------------------------------------------------------------ Purchase
def purchase_reports(day: date) -> list[dict]:
    tag = f"{day:%d-%m-%Y}"
    cols = [("Vendor", "vendor", 70, "L"), ("Material received", "material_date", 32, "L"), ("Invoice date", "invoice_date", 30, "L"),
            ("Invoice no", "invoice_no", 38, "L"), ("Amount (Rs)", "invoice_amount", 32, "R"), ("Remarks", "remarks", 62, "L")]
    out = []
    for slug, label in (("godown", "Godown"), ("shop", "Shop")):
        with db.cursor() as cur:
            cur.execute("""SELECT p.party_name AS vendor, md.full_date AS material_date, idt.full_date AS invoice_date, e.invoice_no,
                                  e.invoice_amount, e.remarks
                           FROM fact_purchase_entry e JOIN dim_purchase_site s ON s.site_key = e.site_key
                           JOIN dim_party p ON p.party_key = e.vendor_party_key
                           LEFT JOIN dim_date md ON md.date_key = e.material_received_date_key
                           LEFT JOIN dim_date idt ON idt.date_key = e.invoice_date_key
                           WHERE s.site_slug = %s AND ((e.created_at AT TIME ZONE 'Asia/Kolkata')::date = %s OR md.full_date = %s)
                           ORDER BY e.purchase_key""", (slug, day, day))
            rows = cur.fetchall()
        out.append({"kind": f"purchase-{slug}", "title": f"Purchase - {label}", "note": f"{label} purchase entries made or received on {tag}",
                    "sections": [_section(f"{label} purchases on {tag}", cols, rows, True)]})
    return out


# ------------------------------------------------------------------ Checklist
def checklist_report(day: date) -> dict:
    tag = f"{day:%d-%m-%Y}"
    with db.cursor() as cur:
        checklist._materialize(cur, day, day)
        cur.execute(checklist.ITEM_SQL + " WHERE dd.full_date = %s AND (c.is_active OR o.status <> 'Pending') "
                    "ORDER BY lower(u.display_name), lower(c.title)", (day,))
        today_rows = cur.fetchall()
        cur.execute(checklist.ITEM_SQL + " WHERE dd.full_date < %s AND dd.full_date >= %s AND o.status = 'Pending' AND c.is_active "
                    "ORDER BY lower(u.display_name), dd.full_date, lower(c.title)", (day, day.fromordinal(day.toordinal() - 30)))
        overdue = cur.fetchall()
    people: dict[str, dict] = {}
    for r in today_rows:
        p = people.setdefault(r["assignee_name"], {"employee": r["assignee_name"], "total": 0, "done": 0, "not_done": 0, "pending": 0})
        p["total"] += 1
        p[{"Done": "done", "Not Done": "not_done"}.get(r["status"], "pending")] += 1
    for r in overdue:
        people.setdefault(r["assignee_name"], {"employee": r["assignee_name"], "total": 0, "done": 0, "not_done": 0, "pending": 0})
    summary = sorted(people.values(), key=lambda x: x["employee"].lower())
    for p in summary:
        p["overdue"] = sum(1 for r in overdue if r["assignee_name"] == p["employee"])
    sum_cols = [("Employee", "employee", 86, "L"), ("Due today", "total", 34, "R"), ("Done", "done", 34, "R"), ("Not done", "not_done", 34, "R"),
                ("Pending today", "pending", 36, "R"), ("Still pending from earlier days", "overdue", 50, "R")]
    det_cols = [("Employee", "employee", 52, "L"), ("Task", "title", 90, "L"), ("Category", "category", 36, "L"), ("Repeats", "frequency", 22, "L"),
                ("Status", "status", 24, "L"), ("Remark", "remark", 52, "L")]

    def detail(rows):
        return [{"employee": r["assignee_name"], "title": r["title"], "category": r["category_name"] or "", "frequency": r["frequency"],
                 "status": r["status"] if r["due_date"] == day else f"Pending since {r['due_date']:%d-%m-%Y}", "remark": r["remark"] or ""} for r in rows]
    return {"kind": "checklist", "title": "Checklist - Done and Pending", "note": f"Checklist items due on {tag}",
            "sections": [_section(f"Each employee's checklist on {tag}", sum_cols, summary, True),
                         _section(f"Tasks due on {tag}", det_cols, detail(today_rows)),
                         _section("Still pending from the last 30 days", det_cols, detail(overdue))]}


# ------------------------------------------------------------------ Payments dashboard
def payments_report(day: date) -> dict:
    tag, iso = f"{day:%d-%m-%Y}", day.isoformat()
    one = payments.dashboard(company="", date_from=iso, date_to=iso, user=SYSTEM_USER)
    overall = payments.dashboard(company="", date_from="", date_to="", user=SYSTEM_USER)
    cards = [("Cash in hand", "cashInHand"), ("Bank balance (entered on Payments)", "bankBalance"), ("Total received (cash)", "totalReceived"),
             ("Total paid (cash)", "totalPaid"), ("Cash sales (from O2D)", "cashSales"), ("Bank sales (from O2D)", "bankSales")]
    rows = [{"card": label, "today": one[key], "overall": overall[key]} for label, key in cards]
    rows.append({"card": "Entries waiting for approval", "today": int(overall["pendingEntries"]), "overall": int(overall["pendingEntries"])})
    for t, n in sorted(overall["pendingByType"].items()):
        if t != "all":
            rows.append({"card": f"   waiting - {t}", "today": int(n), "overall": int(n)})
    rows.append({"card": "Merged entries changed after the merge", "today": int(overall["changedAfterMerge"]), "overall": int(overall["changedAfterMerge"])})
    card_cols = [("Card", "card", 110, "L"), (f"Today ({tag})", "today", 60, "R", "nosum"), ("Overall", "overall", 60, "R", "nosum")]
    txns = payments.transactions(company="", date_from=iso, date_to=iso, user=SYSTEM_USER)["rows"]
    txn_cols = [("Txn", "TxnID", 22, "L"), ("Type", "Type", 36, "L"), ("Company", "Company", 34, "L"), ("Party", "Party", 44, "L"), ("Mode", "Mode", 22, "L"),
                ("Amount (Rs)", "Amount", 28, "R", "nosum"), ("Status", "Status", 20, "L"), ("Source", "Source", 18, "L"), ("Remarks", "Remarks", 48, "L")]
    return {"kind": "pay-cards", "title": "Payments - All Cards and Transactions", "note": f"Payments dashboard for {tag}",
            "sections": [_section("Dashboard cards", card_cols, rows), _section(f"Transactions on {tag}", txn_cols, txns)]}


# ------------------------------------------------------------------ Regular expenses (the whole page)
def regular_expenses_report(day: date) -> dict:
    tag, iso = f"{day:%d-%m-%Y}", day.isoformat()
    with db.cursor() as cur:
        names = [a["name"] for a in expenses.balances(cur).values()]
    sections = []
    for name in names:
        s = expenses.summary(account=name, date_from=iso, date_to=iso, user=SYSTEM_USER)
        p = s["period"]
        cards = [{"card": "Balance right now" + (" (overdrawn)" if s["overdrawn"] else ""), "amount": s["balance"]},
                 {"card": f"Opening balance on {tag}", "amount": p["opening"]}, {"card": "Money added", "amount": p["topups"]},
                 {"card": f"Spent ({p['spendCount']} entries)", "amount": p["spent"]}, {"card": "Closing balance", "amount": p["closing"]}]
        sections.append(_section(f"Regular expenses - {name}: balance and day cards", [("Card", "card", 150, "L"), ("Amount (Rs)", "amount", 60, "R", "nosum")], cards))
        sections.append(_section("Spent by category", [("Category", "category", 120, "L"), ("Entries", "entries", 40, "R"), ("Amount (Rs)", "amount", 50, "R")],
                                 s["byCategory"], True))
        sections.append(_section("Spent by person", [("Person", "name", 150, "L"), ("Amount (Rs)", "amount", 60, "R")], s["byPerson"], True))
        ents = expenses.entries(account=name, date_from=iso, date_to=iso, category="", kind="", include_voided=True, user=SYSTEM_USER)["entries"]
        rows = [{"no": e["no"], "kind": e["kind"], "category": e["category"], "spentBy": e["spentBy"], "paidTo": e["paidTo"], "description": e["description"],
                 "amount": e["amount"], "balanceAfter": e["balanceAfter"], "status": ("Cancelled: " + e["voidReason"]) if e["voided"] else ""}
                for e in ents]
        sections.append(_section(f"Statement for {tag}", [("No", "no", 22, "L"), ("Kind", "kind", 18, "L"), ("Category", "category", 34, "L"),
                                                         ("Spent by", "spentBy", 32, "L"), ("Paid to", "paidTo", 34, "L"), ("Description", "description", 56, "L"),
                                                         ("Amount (Rs)", "amount", 24, "R", "nosum"), ("Balance after", "balanceAfter", 24, "R", "nosum"),
                                                         ("Note", "status", 30, "L")], rows))
    return {"kind": "regular-expenses", "title": "Regular Expenses - Whole Page", "note": f"Regular expenses on {tag}", "sections": sections}


# ------------------------------------------------------------------ build and store
def build_all(day: date) -> list[dict]:
    """Every report of the batch that could be built (a failing one is logged and left out)."""
    out: list[dict] = []
    for build in (attendance_reports, decor_reports, purchase_reports, lambda d: [checklist_report(d)], lambda d: [payments_report(d)],
                  lambda d: [regular_expenses_report(d)]):
        try:
            out += build(day)
        except Exception:
            log.exception("Could not build a closing daily report for %s", day)
    return out


def generate(day: date, now: datetime) -> list[dict]:
    """(Re)build and store the closing reports. Returns [{key, title, orders, kind}] for each one stored (orders = rows in its first table)."""
    stored = []
    for rep in build_all(day):
        try:
            pdf = o2d_reports.build_pdf(rep["title"] + f" - {day:%d-%m-%Y}", rep["sections"], "Automatic daily report", now, rep["note"], COLOR)
        except Exception:
            log.exception("Could not draw the closing daily report %s for %s", rep["kind"], day)
            continue
        count = (sum(len(sec["rows"]) for sec in rep["sections"] if sec["heading"].startswith("Statement")) if rep["kind"] == "regular-expenses"
                 else len(rep["sections"][0]["rows"]))
        with db.cursor() as cur:
            cur.execute("INSERT INTO daily_delivery_report (report_date, person_name, order_count, pdf, generated_at, report_kind) "
                        "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (report_date, person_name) DO UPDATE SET "
                        "order_count = EXCLUDED.order_count, pdf = EXCLUDED.pdf, generated_at = now(), report_kind = EXCLUDED.report_kind "
                        "RETURNING report_key", (day, rep["title"], count, pdf, rep["kind"]))
            stored.append({"key": cur.fetchone()["report_key"], "title": rep["title"], "orders": count, "kind": rep["kind"]})
    return stored
