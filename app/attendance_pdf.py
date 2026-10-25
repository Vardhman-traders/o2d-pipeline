"""Attendance PDFs for the whole team: the monthly attendance report and the salary slips, one employee per page.

  GET /attendance/owner/pdf?kind=attendance|salary&year=&month=&format=pdf|zip

  - format=pdf  one PDF, every active employee starting on a page of their own (print it, or split it by page);
  - format=zip  a ZIP holding a separate PDF for each employee (to send each person only their own sheet).

The numbers come from the same functions as the screen and the Excel export (attendance.get_monthly_all /
get_salary_report), so a PDF
can never disagree with what the owner dashboard shows. Who may open it is the Owner dashboard page access, like
every other call there.
An employee whose salary cannot be calculated is printed with the reason ("Salary not set", ...) on their page and
the failure is logged;
a salary is never guessed.
"""

import io
import logging
import re
import zipfile
from collections.abc import Sequence
from datetime import datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import attendance, db
from . import modcommon as mc

log = logging.getLogger("attendance.pdf")
router = APIRouter(prefix="/attendance/owner", tags=["attendance-pdf"])

NAVY = colors.HexColor("#1a237e")
STATUS_FILL = {
    "Present": "#f0fdf4",
    "Late": "#fffbeb",
    "Half Day": "#faf5ff",
    "Absent": "#fef2f2",
    "Absent (Sandwich)": "#fef2f2",
    "Incomplete": "#fff7ed",
    "Weekly Off": "#f1f5f9",
}
_styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_styles["Heading1"], fontSize=15, textColor=NAVY, spaceAfter=2)
SUB = ParagraphStyle("sub", parent=_styles["Normal"], fontSize=9, textColor=colors.HexColor("#475569"), spaceAfter=6)
CELL = ParagraphStyle("cell", parent=_styles["Normal"], fontSize=8, leading=10)
NOTE = ParagraphStyle("note", parent=_styles["Normal"], fontSize=9, textColor=colors.HexColor("#b91c1c"), spaceBefore=8)


def _p(text, style=CELL) -> Paragraph:
    return Paragraph(escape(str(text if text is not None else "")), style)


def _money(v) -> str:
    return f"{float(v):,.2f}"


def _table(
    rows: list[list], widths: list[float], fills: Sequence[str | None] | None = None, right_cols: tuple[int, ...] = ()
) -> Table:
    t = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    style += [("ALIGN", (c, 0), (c, -1), "RIGHT") for c in right_cols]
    for i, f in enumerate(fills or [], start=1):
        if f:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(f)))
    t.setStyle(TableStyle(style))
    return t


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawString(
        15 * mm, 8 * mm, "Vardhman Traders - generated " + datetime.now(mc.IST).strftime("%d-%m-%Y %H:%M")
    )
    canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


# ------------------------------------------------------------------ one employee's page
def attendance_page(emp: dict, year: int, month: int) -> list:
    """emp = one entry of attendance.get_monthly_all()["employees"]."""
    s = emp["stats"]
    out = [
        Paragraph(escape(emp["name"]), H1),
        Paragraph(escape(f"{emp['role'] or '-'}  |  Attendance report  |  {attendance.MONTHS[month - 1]} {year}"), SUB),
    ]
    summary = [
        ["Present", "Late", "Half day", "Absent", "Weekly off", "Incomplete", "Paid days"],
        [s["present"], s["late"], s["halfDay"], s["absent"], s["weeklyOff"], s.get("incomplete", 0), s["paidDays"]],
    ]
    out += [_table(summary, [25] * 7), Spacer(1, 6)]
    rows: list[list] = [["Date", "Day", "Status", "Check-in", "Check-out", "Hours", "Place / note"]]
    fills: list[str | None] = [None]
    for d in emp["days"]:
        note = ("Corrected: " + d["note"]) if d.get("corrected") else d.get("site", "")
        rows.append(
            [
                d["date"][8:] + "-" + d["date"][5:7] + "-" + d["date"][:4],
                d["dayName"],
                d["status"],
                d["time"] or "",
                d["checkoutTime"] or "",
                d["hoursWorked"] or "",
                _p(note),
            ]
        )
        fills.append(STATUS_FILL.get(d["status"]))
    out.append(_table(rows, [24, 14, 26, 20, 20, 20, 52], fills))
    return out


def salary_page(name: str, role: str, year: int, month: int, r: dict) -> list:
    """r = attendance.get_salary_report() for the employee (or its error answer)."""
    out = [
        Paragraph(escape(name), H1),
        Paragraph(escape(f"{role or '-'}  |  Salary slip  |  {attendance.MONTHS[month - 1]} {year}"), SUB),
    ]
    if r.get("status") != "ok":
        out.append(Paragraph(escape("Salary could not be calculated: " + str(r.get("msg") or "unknown reason")), NOTE))
        return out
    s = r["stats"]
    figures = [
        ["Item", "Value"],
        ["Monthly salary (Rs)", _money(r["monthlySalary"])],
        [f"Per day (salary / {r['baseDays']})", _money(r["perDaySalary"])],
        ["Deduction days", f"{r['totalDeductionDays']:g}"],
        ["Deduction amount (Rs)", _money(r["deductionAmount"])],
        ["Advance deducted (Rs)", _money(r["totalAdvance"])],
        ["Paid days", f"{r['totalPaidDays']:g}"],
        ["NET PAYABLE (Rs)", _money(r["netSalary"])],
    ]
    t = _table(figures, [70, 45], right_cols=(1,))
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e0e7ff")),
            ]
        )
    )
    out += [t, Spacer(1, 6)]
    counts = [
        ["Present", "Late", "Half day", "Absent", "Sandwich", "Weekly off", "Incomplete"],
        [s["present"], s["late"], s["halfDay"], s["absent"], s["sandwich"], s["weeklyOff"], s["incomplete"]],
    ]
    out += [_table(counts, [25] * 7), Spacer(1, 6)]
    if r["advances"]:
        adv = [["Advance date", "Amount (Rs)", "Note"]] + [
            [a.get("date", ""), _money(a["amount"]), _p(a.get("note", ""))] for a in r["advances"]
        ]
        out += [_table(adv, [35, 35, 100], right_cols=(1,)), Spacer(1, 6)]
    rows = [["Day", "Status", "Check-in", "Check-out", "Hours"]]
    fills: list[str | None] = [None]
    for d in r["days"]:
        rows.append([d["day"], d["status"], d["time"] or "", d["checkoutTime"] or "", d["hoursWorked"] or ""])
        fills.append(STATUS_FILL.get(d["status"]))
    out.append(_table(rows, [15, 45, 30, 30, 30], fills))
    return out


# ------------------------------------------------------------------ building the files
def _pdf(pages: list[list]) -> bytes:
    """pages: one flowables list per employee; each starts a new page."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title="Vardhman Traders - attendance",
        author="Vardhman Traders",
    )
    story: list = []
    for i, flow in enumerate(pages):
        if i:
            story.append(PageBreak())
        story += flow
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "employee"


def build(kind: str, year: int, month: int, fmt: str, user) -> tuple[bytes, str, str]:
    """-> (file bytes, media type, file name)."""
    mname = attendance.MONTHS[month - 1]
    if kind == "attendance":
        data = attendance.get_monthly_all(user, year, month)
        if data.get("status") != "ok":
            raise HTTPException(422, data.get("msg") or "Could not build the attendance report.")
        items = [(e["name"], attendance_page(e, year, month)) for e in data["employees"]]
    else:
        with db.cursor() as cur:
            cur.execute(
                "SELECT employee_name, role_title FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)"
            )
            emps = cur.fetchall()
        rules = attendance.Rules()
        items = []
        for e in emps:
            r = attendance.get_salary_report(user, e["employee_name"], year, month, None, rules)
            if r.get("status") != "ok":
                log.warning(
                    "salary slip pdf: no slip for %s (%s-%02d) by user %s (%s): %s",
                    e["employee_name"],
                    year,
                    month,
                    user["user_key"],
                    user["username"],
                    r.get("msg"),
                )
            items.append((e["employee_name"], salary_page(e["employee_name"], e["role_title"] or "", year, month, r)))
    if not items:
        raise HTTPException(404, "There are no active employees to report on.")
    label = "Attendance" if kind == "attendance" else "Salary"
    if fmt == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            used: set[str] = set()
            for name, flow in items:
                fname = _safe(name)
                while fname in used:  # two people with the same name still get a file each
                    fname += "_"
                used.add(fname)
                z.writestr(f"{fname}_{label}_{mname}_{year}.pdf", _pdf([flow]))
        return buf.getvalue(), "application/zip", f"{label}_All_Employees_{mname}_{year}.zip"
    return _pdf([flow for _, flow in items]), "application/pdf", f"{label}_All_Employees_{mname}_{year}.pdf"


@router.get("/pdf")
def team_pdf(
    kind: str = Query(default="attendance"),
    year: int = Query(...),
    month: int = Query(...),
    format: str = Query(default="pdf"),
    user=Depends(attendance.OWNER_DEP),
):
    if kind not in ("attendance", "salary"):
        raise HTTPException(422, "kind must be attendance or salary.")
    if format not in ("pdf", "zip"):
        raise HTTPException(422, "format must be pdf or zip.")
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        raise HTTPException(422, "Month or year is not valid.")
    try:
        body, media, name = build(kind, year, month, format, user)
    except HTTPException:
        raise
    except Exception:
        log.exception(
            "team pdf failed: kind=%s year=%s month=%s format=%s user=%s (%s)",
            kind,
            year,
            month,
            format,
            user["user_key"],
            user["username"],
        )
        raise HTTPException(
            500, f"Could not build the {kind} PDF for {attendance.MONTHS[month - 1]} {year}. The error has been logged."
        )
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )
