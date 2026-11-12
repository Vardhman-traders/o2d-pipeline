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
    "Holiday": "#eff6ff",
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
        15 * mm, 8 * mm, "Code Blue - generated " + datetime.now(mc.IST).strftime("%d-%m-%Y %H:%M")
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


# The salary slip is drawn like the one on the owner dashboard: header band, employee row, one card per count with its % underneath, the
# earnings / deductions table, the net-payable band, then the month's calendar. Rs (not the rupee sign) because the PDF fonts have no
# rupee glyph.
SLIP_W = 180          # mm, the printable width of an A4 page with 15 mm margins
INK = colors.HexColor("#1a1f36")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#e2e8f0")
BAND = colors.HexColor("#f8faff")
CARD_COLOURS = {          # card key -> (label, number colour, top border)
    "present": ("PRESENT", "#16a34a", "#22c55e"),
    "late": ("LATE", "#d97706", "#f59e0b"),
    "halfDay": ("HALF DAY", "#7c3aed", "#8b5cf6"),
    "absent": ("ABSENT", "#dc2626", "#ef4444"),
    "sandwich": ("SANDWICH", "#ea580c", "#f97316"),
    "advance": ("ADVANCE", "#c2410c", "#c2410c"),
    "bonus": ("BONUS", "#15803d", "#16a34a"),
}
# calendar cell: status -> (fill, border, label colour)
DAY_LOOK = {
    "Present": ("#f0fdf4", "#86efac", "#166534"),
    "Late": ("#fffbeb", "#fde68a", "#92400e"),
    "Half Day": ("#faf5ff", "#d8b4fe", "#5b21b6"),
    "Absent": ("#fef2f2", "#fecaca", "#991b1b"),
    "Absent (Sandwich)": ("#fef2f2", "#fecaca", "#991b1b"),
    "Incomplete": ("#fff7ed", "#fed7aa", "#c2410c"),
    "Weekly Off": ("#f8fafc", "#e2e8f0", "#64748b"),
    "Holiday": ("#eff6ff", "#bfdbfe", "#1e40af"),
    "Future": ("#fafbfc", "#e5e7eb", "#9ca3af"),
}


def _ps(name: str, size: float, colour=INK, bold: bool = False, align: int = 0, leading: float | None = None) -> ParagraphStyle:
    return ParagraphStyle(name, parent=_styles["Normal"], fontSize=size, textColor=colour, alignment=align, leading=leading or size * 1.25,
                          fontName="Helvetica-Bold" if bold else "Helvetica")


def _rs(v) -> str:
    return f"Rs {float(v):,.0f}"


def _initials(name: str) -> str:
    words = name.split()
    return ((words[0][0] + (words[1][0] if len(words) > 1 else "")) if words else "?").upper()


def _slip_header(month_name: str, year: int) -> Table:
    logo = Paragraph("VT", _ps("logo", 15, colors.white, True, 1))
    text = [Paragraph("VT", _ps("co", 15, colors.white, True)),
            Paragraph(escape(f"Salary Slip - {month_name} {year}"), _ps("tag", 8, colors.HexColor("#c7d2fe")))]
    t = Table([[logo, text]], colWidths=[16 * mm, (SLIP_W - 16) * mm], rowHeights=[17 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY), ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#3949ab")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (1, 0), (1, 0), 8), ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    return t


def _slip_employee(name: str, role: str, monthly: float) -> Table:
    badge = Paragraph(escape(_initials(name)), _ps("ini", 10, colors.HexColor("#3949ab"), True, 1))
    who = [Paragraph(escape(name), _ps("nm", 10, INK, True)), Paragraph(escape(role or "-"), _ps("rl", 7.5, MUTED))]
    pay = [Paragraph(escape(_rs(monthly)), _ps("gs", 11, NAVY, True, 2)), Paragraph("gross/month", _ps("gl", 6.5, MUTED, False, 2))]
    t = Table([[badge, who, pay]], colWidths=[12 * mm, (SLIP_W - 12 - 45) * mm, 45 * mm], rowHeights=[13 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BAND), ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#e0e7ff")), ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    return t


def _slip_cards(r: dict) -> list:
    """One card per count with its % of the counted days underneath (Advance: % of the gross salary)."""
    s, pc = r["stats"], r["percent"]
    keys = (["present", "late", "halfDay", "absent"] + (["sandwich"] if s["sandwich"] > 0 else []) + (["advance"] if r["totalAdvance"] > 0 else [])
            + (["bonus"] if r.get("bonusEarned") else []))      # the bonus card is there only for the months it is earned
    cells, tops = [], []
    for k in keys:
        label, num_colour, top = CARD_COLOURS[k]
        value = _rs(r["totalAdvance"]) if k == "advance" else _rs(r["bonus"]) if k == "bonus" else str(s[k])
        under = "1 day" if k == "bonus" else f"{pc[k]:.1f}%"
        cells.append([Paragraph(escape(value), _ps("cv" + k, 14 if k not in ("advance", "bonus") else 11, colors.HexColor(num_colour), True, 1)),
                      Paragraph(label, _ps("cl" + k, 6.5, MUTED, True, 1)),
                      Paragraph(under, _ps("cp" + k, 8.5, colors.HexColor(num_colour), True, 1))])
        tops.append(colors.HexColor(top))
    gap, n = 2, len(keys)
    row: list = []                                     # a thin empty column between the cards
    for i, c in enumerate(cells):
        row += [c] + ([""] if i < n - 1 else [])
    card_w = (SLIP_W - gap * (n - 1)) / n
    col_widths = [w * mm for i in range(n) for w in ([card_w] + ([gap] if i < n - 1 else []))]
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    for i, colour in enumerate(tops):
        j = i * 2
        style += [("BOX", (j, 0), (j, 0), 0.5, LINE), ("LINEABOVE", (j, 0), (j, 0), 2.2, colour)]
    t = Table([row], colWidths=col_widths)
    t.setStyle(TableStyle(style))
    return [t, Spacer(1, 2),
            Paragraph(escape(f"% = share of the {r['countedDays']} days that count towards pay (weekly offs and days still to come are left out; "
                             f"Sandwich days are already part of Absent" + ("; Advance = share of the gross salary)" if r["totalAdvance"] > 0 else ")")),
                      _ps("pcnote", 6.5, MUTED))]


def _slip_table(r: dict) -> Table:
    s, per = r["stats"], r["perDaySalary"]
    rows: list[list] = [["Earnings", ""], ["Monthly gross salary", _rs(r["monthlySalary"])], [f"Per day rate (/ {r['baseDays']} days)", _rs(per)]]
    seps, reds, ambers, greens = [0], [], [], []
    if r.get("bonusEarned"):
        rows.append([f"Bonus - no leave this month (1 day x {_rs(per)})", "+ " + _rs(r["bonus"])])
        greens.append(len(rows) - 1)
    rows.append(["Deductions", ""])
    seps.append(len(rows) - 1)
    leave_cut, leaves = r.get("leaveDeductionDays", 0), r.get("leaves", 0)
    if leave_cut:
        rows.append([f"Leaves ({leave_cut:g} days x {_rs(per)})", "- " + _rs(round(leave_cut * per))])
        reds.append(len(rows) - 1)
    elif leaves:
        rows.append([f"Leaves ({leaves:g}) - covered, nothing cut", "Rs 0"])
    if r.get("offsetDays"):
        rows.append([f"Monday worked ({s.get('offWorked', 0)}) - cancels {r['offsetDays']:g} leave", "Counted above"])
    rows.append([f"Half Day ({s['halfDay']} x 0.5 x {_rs(per)})", "- " + _rs(round(s["halfDay"] * 0.5 * per))])
    reds.append(len(rows) - 1)
    if s["sandwich"]:
        rows.append([f"Monday cut ({s['sandwich']} day x {_rs(per)})", "- " + _rs(round(s["sandwich"] * per))])
        reds.append(len(rows) - 1)
    for a in r["advances"]:
        rows.append([f"Advance - {str(a.get('date', ''))[:10]}" + (f" ({a['note']})" if a.get("note") else ""), "- " + _rs(a["amount"])])
        ambers.append(len(rows) - 1)
    if r.get("carryIn"):
        rows.append(["Advance balance from earlier months", "- " + _rs(r["carryIn"])])
        ambers.append(len(rows) - 1)
    rows.append(["Summary", ""])
    seps.append(len(rows) - 1)
    rows.append([f"Total deduction ({r['totalDeductionDays']:g} days)", "- " + _rs(r["deductionAmount"])])
    reds.append(len(rows) - 1)
    if r["totalAdvance"] > 0 or r.get("carryIn"):
        rows.append(["Total advance", "- " + _rs(r["totalAdvance"] + r.get("carryIn", 0))])
        ambers.append(len(rows) - 1)
    rows.append(["Paid days", f"{r['totalPaidDays']:g} / {r['baseDays']} days"])
    green = len(rows) - 1
    body = [[Paragraph(escape(a), _ps("tl", 8, MUTED, False)), Paragraph(escape(b), _ps("tv", 8, INK, True, 2))] for a, b in rows]
    t = Table(body, colWidths=[(SLIP_W - 55) * mm, 55 * mm])
    style = [("BOX", (0, 0), (-1, -1), 0.5, LINE), ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#f1f5f9")),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for i in seps:
        style += [("BACKGROUND", (0, i), (-1, i), BAND), ("SPAN", (0, i), (-1, i))]
        body[i] = [Paragraph(escape(rows[i][0].upper()), _ps("sp", 6.8, colors.HexColor("#374151"), True)), ""]
    for i in reds:
        body[i][1] = Paragraph(escape(rows[i][1]), _ps("tvr", 8, colors.HexColor("#dc2626"), True, 2))
    for i in ambers:
        body[i][1] = Paragraph(escape(rows[i][1]), _ps("tva", 8, colors.HexColor("#c2410c"), True, 2))
    for i in greens:
        body[i][1] = Paragraph(escape(rows[i][1]), _ps("tvg2", 8, colors.HexColor("#16a34a"), True, 2))
    body[green][1] = Paragraph(escape(rows[green][1]), _ps("tvg", 8, colors.HexColor("#16a34a"), True, 2))
    t = Table(body, colWidths=[(SLIP_W - 55) * mm, 55 * mm])
    t.setStyle(TableStyle(style))
    return t


def _slip_net(net: float) -> Table:
    """A net below nothing = more advance than salary: it is shown as a negative amount, to be cut from next month's salary."""
    label = "Net Payable Salary" if net >= 0 else "Net Payable Salary (advance balance - cut from next month)"
    value = _rs(net) if net >= 0 else "- " + _rs(-net)
    t = Table([[Paragraph(label, _ps("nl", 9, colors.HexColor("#c7d2fe"), True)),
                Paragraph(escape(value), _ps("nv", 15, colors.HexColor("#fecaca") if net < 0 else colors.white, True, 2))]],
              colWidths=[(SLIP_W - 70) * mm, 70 * mm], rowHeights=[14 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 10),
                           ("RIGHTPADDING", (1, 0), (1, 0), 10), ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    return t


def _day_cell(d: dict) -> tuple[list, str, str]:
    st = d["status"]
    fill, border, ink = DAY_LOOK.get(st, DAY_LOOK["Weekly Off"])
    label = {"Absent (Sandwich)": "Absent*", "Weekly Off": "OFF", "Future": "-"}.get(st, st)
    sub = ""
    if st in ("Present", "Late", "Half Day") and d.get("time"):
        sub = d["time"] + (" - " + d["checkoutTime"] if d.get("checkoutTime") and d["checkoutTime"] != "—" else "")
    hrs = d.get("hoursWorked") if d.get("hoursWorked") not in (None, "", "—") else ""
    flow = [Paragraph(str(d["day"]), _ps("dn", 8.5, INK, True, 1)), Paragraph(escape(label.upper()), _ps("ds" + st, 5.6, colors.HexColor(ink), True, 1))]
    if sub:
        flow.append(Paragraph(escape(sub), _ps("dt", 5.4, MUTED, False, 1)))
    if hrs:
        flow.append(Paragraph(escape(str(hrs)), _ps("dh", 5.4, MUTED, False, 1)))
    return flow, fill, border


def _slip_calendar(r: dict, month_name: str, year: int) -> list:
    out: list = [Paragraph(escape(f"Attendance Calendar - {month_name} {year}"), _ps("ct", 9, colors.HexColor("#374151"), True)), Spacer(1, 3)]
    head = [Paragraph(x.upper(), _ps("dh2", 6, colors.HexColor("#64748b"), True, 1)) for x in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]
    first = (datetime(year, month_number(month_name), 1).weekday() + 1) % 7      # Sunday = 0, as on the screen
    cells: list = [""] * first
    looks: list = [None] * first
    for d in r["days"]:
        flow, fill, border = _day_cell(d)
        cells.append(flow)
        looks.append((fill, border))
    while len(cells) % 7:
        cells.append("")
        looks.append(None)
    rows = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    w = SLIP_W / 7
    t = Table([head] + rows, colWidths=[w * mm] * 7, rowHeights=[6 * mm] + [13.5 * mm] * len(rows))
    style = [("VALIGN", (0, 1), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 1), (-1, -1), 1), ("BOTTOMPADDING", (0, 1), (-1, -1), 1),
             ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]
    for i, look in enumerate(looks):
        if look:
            row, col = i // 7 + 1, i % 7
            style += [("BACKGROUND", (col, row), (col, row), colors.HexColor(look[0])), ("BOX", (col, row), (col, row), 0.8, colors.HexColor(look[1]))]
    t.setStyle(TableStyle(style))
    legend = Paragraph(
        '<font color="#16a34a">&#9632;</font> Present &nbsp; <font color="#d97706">&#9632;</font> Late &nbsp; <font color="#7c3aed">&#9632;</font> Half Day &nbsp; '
        '<font color="#dc2626">&#9632;</font> Absent (* sandwich) &nbsp; <font color="#94a3b8">&#9632;</font> Weekly Off &nbsp; <font color="#3b82f6">&#9632;</font> Holiday', _ps("lg", 6.8, MUTED))
    return out + [t, Spacer(1, 4), legend]


def month_number(month_name: str) -> int:
    return attendance.MONTHS.index(month_name) + 1


def salary_page(name: str, role: str, year: int, month: int, r: dict) -> list:
    """r = attendance.get_salary_report() for the employee (or its error answer). Drawn like the salary slip on the owner dashboard."""
    mname = attendance.MONTHS[month - 1]
    out: list = [_slip_header(mname, year), Spacer(1, 5)]
    if r.get("status") != "ok":
        out += [_slip_employee(name, role, 0), Paragraph(escape("Salary could not be calculated: " + str(r.get("msg") or "unknown reason")), NOTE)]
        return out
    out += [_slip_employee(name, role, r["monthlySalary"]), Spacer(1, 5)]
    out += _slip_cards(r) + [Spacer(1, 6), _slip_table(r), Spacer(1, 6), _slip_net(r["netSalary"]), Spacer(1, 8)]
    out += _slip_calendar(r, mname, year)
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
        title="Code Blue - attendance",
        author="Code Blue",
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
