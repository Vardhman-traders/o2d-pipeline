"""Printable PDF reports for the O2D dashboard: what is still pending at each stage, and the receiving / payment
position. Each report is one A4-landscape table with the important columns, and a "Created by / Approved by"
block at the bottom (creator's name filled in, approver's line left to sign by hand).

Same rule as the order screens decides which orders a person's report contains (roles.visibility); who may open
the reports at all is the admin-set page access "o2d_reports".
"""
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import access, config, db, roles

router = APIRouter(prefix="/o2d/reports", tags=["o2d-reports"])
IST = ZoneInfo("Asia/Kolkata")
MAX_ROWS = 5000

# Where an order stands, as the O2D screens define it (cancelled orders need no action, so they are left out).
_LIVE = "NOT o.is_cancelled AND lower(btrim(COALESCE(st.type_name, ''))) <> 'cancelled'"
_DELIVERED = "o.material_delivery_datetime IS NOT NULL"
_RECEIVED = "o.date_of_receiving_key IS NOT NULL AND o.payment_status_key IS NOT NULL"
_CASH = "lower(btrim(COALESCE(ps.status_name, ''))) = 'cash'"   # payment mode "Cash"; everything else is "other modes"
# Highest doc (DC / Inv) number first; numbers are compared as numbers (so 100 comes before 99), blanks last.
_BY_DOC_NO = "NULLIF(regexp_replace(o.dc_inv_no, '[^0-9]', '', 'g'), '')::numeric DESC NULLS LAST, o.dc_inv_no DESC, o.sl_no DESC"
_NEWEST_ORDERED = _NEWEST_DELIVERED = _NEWEST_RECEIVED = _BY_DOC_NO


# column = (header, field, width in mm, align)
ORDER_COLS = [("Order date", "order_received_date", 22, "L"), ("DC / Inv No", "dc_inv_no", 22, "L"),
              ("Type", "submission_type", 22, "L"), ("Address", "shipping_location", 52, "L"),
              ("Remarks", "detailed_remarks", 62, "L")]
_STATUS = "lower(btrim(COALESCE(ds.status_name, '')))"
_SHOP = f"{_STATUS} = 'shop'"
_GODOWN_SIDE = f"{_STATUS} NOT IN ('', 'shop', 'cancelled')"   # same split the two dispatch screens use
_AWAITING_COLS = [("Order date", "order_received_date", 21, "L"), ("DC / Inv No", "dc_inv_no", 21, "L"), ("Address", "shipping_location", 58, "L"),
                  ("Delivered on", "material_delivery_datetime", 30, "L"), ("Delivered by", "delivered_by_full", 50, "L"),
                  ("Payment status", "payment_status", 28, "L"), ("Cartage (Rs)", "cartage", 24, "R")]
_RECEIVED_COLS = [("Order date", "order_received_date", 21, "L"), ("DC / Inv No", "dc_inv_no", 21, "L"), ("Address", "shipping_location", 52, "L"),
                  ("Delivered by", "delivered_by_full", 42, "L"), ("Date received", "date_of_receiving", 24, "L"),
                  ("Payment mode / status", "payment_status", 32, "L"), ("Cartage (Rs)", "cartage", 22, "R"),
                  ("Amount received (Rs)", "amount_received", 30, "R")]
_DISPATCH_COLS = ORDER_COLS + [("Ready by", "ready_by", 24, "L"), ("Delivery status", "delivery_status", 24, "L")]
def _received_report(side: str, label: str, mode: str, color: str) -> dict:
    side_sql, cash_sql = (_SHOP if side == "shop" else _GODOWN_SIDE), (_CASH if mode == "cash" else f"NOT ({_CASH})")
    what = "paid in cash" if mode == "cash" else "paid by other modes (not cash)"
    return {"title": f"{label} Receiving - Received & Payments ({'Cash' if mode == 'cash' else 'Other modes'})",
            "file": f"{side}_received_{mode}", "color": color, "order": _NEWEST_RECEIVED,
            "sections": [(f"{label} orders received, {what}", f"{_LIVE} AND {side_sql} AND {_DELIVERED} AND {_RECEIVED} AND {cash_sql}",
                          _RECEIVED_COLS, True)]}


# One report per part. color = the column-heading band, so printed reports are easy to tell apart.
REPORTS = {
    "godown-pending": {
        "title": "Godown - Pending Orders", "file": "godown_pending", "color": "#b7791f", "order": _NEWEST_ORDERED,
        "sections": [("Orders waiting on the godown (no delivery status yet)", f"{_LIVE} AND o.delivery_status_key IS NULL",
                      ORDER_COLS + [("Order via", "order_via", 22, "L"), ("Taken by", "created_by", 24, "L")], False)]},
    "shop-dispatch-pending": {
        "title": "Shop Dispatch - Pending Orders", "file": "shop_dispatch_pending", "color": "#1f8a5f", "order": _NEWEST_ORDERED,
        "sections": [("Shop orders not yet delivered", f"{_LIVE} AND {_SHOP} AND NOT ({_DELIVERED})", _DISPATCH_COLS, False)]},
    "godown-dispatch-pending": {
        "title": "Godown Dispatch - Pending Orders", "file": "godown_dispatch_pending", "color": "#1b7f8c", "order": _NEWEST_ORDERED,
        "sections": [("Godown orders not yet delivered", f"{_LIVE} AND {_GODOWN_SIDE} AND NOT ({_DELIVERED})", _DISPATCH_COLS, False)]},
    # shop and godown orders together
    "receiving-pending": {
        "title": "Awaiting Receiving", "file": "receiving_pending", "color": "#7c3aed", "order": _NEWEST_DELIVERED,
        "sections": [("Delivered, awaiting receiving or payment (shop and godown)", f"{_LIVE} AND {_DELIVERED} AND NOT ({_RECEIVED})", _AWAITING_COLS, False)]},
    # Cartage reports are built from the cartage data (period = delivery date), see app/o2d_cartage.py
    "cartage-detail": {
        "title": "Cartage - Deliveries & Cartage Paid", "file": "cartage_detail", "color": "#c2410c", "cartage": "detail",
        # same columns as the Archive portal's "Cartage History" PDF: S.No., Date, DC/Inv No., Order Received Date,
        # Receiving, Delivered, Cartage, Address, Sign
        "sections": [("Cartage history", None,
                      [("S.No.", "sno", 16, "L"), ("Date", "cartage_on", 24, "L"), ("DC / Inv No.", "dc_inv_no", 24, "L"),
                       ("Order received date", "order_received_date", 26, "L"), ("Receiving", "payment_status", 30, "L"),
                       ("Delivered", "delivered_by_full", 44, "L"), ("Cartage (Rs)", "cartage", 24, "R"),
                       ("Address", "shipping_location", 50, "L"), ("Sign", "sign", 30, "L")], True)]},
    "cartage-by-employee": {
        "title": "Cartage - Paid to Each Delivery Person", "file": "cartage_by_employee", "color": "#9a3412", "cartage": "employee",
        "sections": [("Cartage by delivery person", None,
                      [("Delivery person", "employee", 90, "L"), ("Deliveries", "deliveries", 34, "R"),
                       ("With cartage", "with_cartage", 34, "R"), ("Total cartage (Rs)", "total", 46, "R"),
                       ("Average per paid delivery (Rs)", "average", 56, "R", "nosum")], True)]},
    "godown-received-cash": _received_report("godown", "Godown", "cash", "#2563eb"),
    "godown-received-other": _received_report("godown", "Godown", "other", "#1d4ed8"),
    "shop-received-cash": _received_report("shop", "Shop", "cash", "#0f766e"),
    "shop-received-other": _received_report("shop", "Shop", "other", "#115e59"),
}


def _fmt(field: str, v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.astimezone(IST).strftime("%d-%m-%Y %H:%M") if v.tzinfo else v.strftime("%d-%m-%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)   # counts stay whole numbers
    if isinstance(v, (Decimal, float, int)) and not isinstance(v, bool):
        return f"{float(v):,.2f}"
    return str(v)


def _prepare(row: dict) -> dict:
    r = dict(row)
    r["amount_received"] = config.shown_amount(row.get("amount_received"))   # the "entered / 100" display rule
    r["delivered_by_full"] = " - ".join(x for x in (row.get("delivered_by"), row.get("delivered_by_detail")) if x)
    return r


def build_pdf(title: str, sections: list[dict], created_by: str, generated_at: datetime, note: str = "",
              color: str = "#1f3a5f") -> bytes:
    """sections: [{heading, columns: [(header, field, mm, align)], rows: [dict], totals: bool}] -> PDF bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm, title=title, author=created_by)
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=18, leading=22, spaceAfter=3)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=5)
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=base["Normal"], fontSize=9, leading=11)
    cell_r = ParagraphStyle("cell_r", parent=cell, alignment=2)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    head_r = ParagraphStyle("head_r", parent=head, alignment=2)

    company = ParagraphStyle("company", parent=base["Title"], fontSize=24, leading=28, alignment=0, textColor=colors.HexColor(color), spaceAfter=0)
    story = [Table([[Paragraph("Vardhman Traders", company),
                     Paragraph(f"<b>Date:</b> {generated_at.astimezone(IST):%d-%m-%Y}<br/><b>Time:</b> {generated_at.astimezone(IST):%H:%M} IST",
                               ParagraphStyle("dt", parent=cell_r, fontSize=11, leading=14))]],
                   colWidths=[(sum(c[2] for c in sections[0]["columns"]) if sections else 260) * mm - 50 * mm, 50 * mm], hAlign="LEFT",
                   style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor(color)), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                     ("LEFTPADDING", (0, 0), (0, 0), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)])),
             Spacer(1, 6), Paragraph(escape(title), h1),
             Paragraph(escape(f"Prepared by {created_by}" + (f" · {note}" if note else "")), small)]
    for sec in sections:
        cols, rows = sec["columns"], sec["rows"]
        story.append(CondPageBreak(35 * mm))   # no orphan heading at the foot of a page, but a long table still starts on page 1
        story.append(Paragraph(escape(f"{sec['heading']} ({len(rows)})"), h2))
        data = [[Paragraph(escape(c[0]), head_r if c[3] == "R" else head) for c in cols]]
        for r in rows:
            data.append([Paragraph(escape(_fmt(c[1], r.get(c[1]))), cell_r if c[3] == "R" else cell) for c in cols])
        if not rows:
            data.append([Paragraph("Nothing here.", cell)] + [""] * (len(cols) - 1))
        if sec.get("totals") and rows:
            tot = []
            for c in cols:
                if c[3] == "R" and len(c) < 5:   # a 5th item ("nosum") marks a column that must not be added up
                    whole = all(isinstance(r.get(c[1]), int) for r in rows)   # counts stay whole numbers, money keeps paise
                    n = sum(float(r.get(c[1]) or 0) for r in rows)
                    tot.append(Paragraph(f"<b>{int(n):,}</b>" if whole else f"<b>{n:,.2f}</b>", cell_r))
                else:
                    tot.append("")
            tot[0] = Paragraph("<b>Total</b>", cell)
            data.append(tot)
        t = Table(data, colWidths=[c[2] * mm for c in cols], repeatRows=1, hAlign="LEFT")
        style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(color)),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c0cc")),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f8")]),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if sec.get("totals") and rows:
            style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e4e9f0")))
        t.setStyle(TableStyle(style))
        story.append(t)

    width = sum(c[2] for c in sections[0]["columns"]) * mm if sections else 260 * mm
    sign = Table([[Paragraph(f"<b>Created by:</b> {escape(created_by)}", cell), Paragraph("<b>Approved by:</b> ______________________", cell_r)]],
                 colWidths=[width / 2, width / 2], hAlign="LEFT")
    sign.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.HexColor("#1f3a5f")),
                              ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [Spacer(1, 60), KeepTogether([sign])]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawRightString(landscape(A4)[0] - 10 * mm, 7 * mm, f"Page {d.page}")
        canvas.drawString(10 * mm, 7 * mm, "Vardhman Traders - O2D")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def fetch_rows(user, where_sql: str, date_from: date | None, date_to: date | None, order_sql: str = _NEWEST_ORDERED) -> list[dict]:
    from . import main
    vis_sql, vis_params = roles.visibility(user)
    where, params = [vis_sql, f"({where_sql})"], list(vis_params)
    if date_from:
        where.append("d.full_date >= %s"); params.append(date_from)
    if date_to:
        where.append("d.full_date <= %s"); params.append(date_to)
    sql = (main.ORDER_SELECT + " WHERE " + " AND ".join(where) +
           f" ORDER BY {order_sql} LIMIT %s")
    with db.cursor() as cur:
        cur.execute(sql, params + [MAX_ROWS])
        return [_prepare(r) for r in cur.fetchall()]


def _doc_no_key(r: dict):
    digits = "".join(ch for ch in str(r.get("dc_inv_no") or "") if ch.isdigit())
    return (digits != "", int(digits) if digits else 0, str(r.get("dc_inv_no") or ""), r["sl_no"])


def _cartage_sections(spec: dict, date_from: date | None, date_to: date | None, delivered_by: str | None):
    from . import o2d_cartage
    rows = o2d_cartage.fetch_cartage(date_from, date_to, delivered_by)   # no dates = everything on record, like the Archive report
    heading, _, cols, totals = spec["sections"][0]
    if spec["cartage"] == "employee":
        rows = o2d_cartage.by_employee(rows)
    else:
        rows.sort(key=_doc_no_key, reverse=True)   # highest doc number first
        for i, r in enumerate(rows, 1):
            r["sno"] = i
            r["sign"] = ""
    tag = f" - {delivered_by}" if delivered_by else ""
    period = (f"{date_from:%d-%m-%Y}" if date_from else "start") + " to " + (f"{date_to:%d-%m-%Y}" if date_to else "today")
    note = f"Date of receiving {period}" if (date_from or date_to) else "All dates"
    return [{"heading": heading + tag, "columns": cols, "totals": totals, "rows": rows}], note, len(rows) >= o2d_cartage.MAX_ROWS


@router.get("/{kind}.pdf")
def report_pdf(kind: str, date_from: date | None = Query(default=None), date_to: date | None = Query(default=None),
               delivered_by: str | None = Query(default=None, max_length=150),
               user=Depends(access.require_page("o2d_reports", "o2d_cartage"))):
    spec = REPORTS.get(kind)
    if not spec:
        raise HTTPException(404, "Unknown report.")
    held = access.effective_pages(user)
    # the cartage reports open for the Cartage page as well as the Reports page; the others need the Reports page
    if not (held & ({"o2d_reports", "o2d_cartage"} if spec.get("cartage") else {"o2d_reports"})):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "From date is after the To date.")
    if spec.get("cartage"):
        if spec["cartage"] == "detail" and not delivered_by:
            raise HTTPException(422, "Choose a delivery person first.")   # the Archive report was always for one person
        sections, note, cut = _cartage_sections(spec, date_from, date_to, delivered_by or None)
        title = f"Cartage History - {delivered_by}" if spec["cartage"] == "detail" else spec["title"]
        pdf = build_pdf(title, sections, user.get("display_name") or user["username"], datetime.now(IST),
                        note + (f" · first {MAX_ROWS} deliveries only, narrow the dates" if cut else ""), spec["color"])
        name = f"{spec['file']}_{datetime.now(IST):%Y%m%d_%H%M}.pdf"
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                                     "Cache-Control": "no-store"})
    sections = [{"heading": h, "columns": cols, "totals": totals, "rows": fetch_rows(user, where, date_from, date_to, spec.get("order", _NEWEST_ORDERED))}
                for h, where, cols, totals in spec["sections"]]
    note = "Order dates " + (f"{date_from:%d-%m-%Y}" if date_from else "start") + " to " + (f"{date_to:%d-%m-%Y}" if date_to else "today") \
        if (date_from or date_to) else "All orders still open"
    if any(len(s["rows"]) >= MAX_ROWS for s in sections):
        note += f" · first {MAX_ROWS} rows only, narrow the dates"
    pdf = build_pdf(spec["title"], sections, user.get("display_name") or user["username"], datetime.now(IST), note, spec["color"])
    name = f"{spec['file']}_{datetime.now(IST):%Y%m%d_%H%M}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                                 "Cache-Control": "no-store"})
