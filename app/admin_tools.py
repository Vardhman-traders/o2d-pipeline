"""Admin portal tools for the order tracker: custom order filters (with saved filters and Excel export) and bulk upload
(orders and members) with template, validation, inline fixes, one-transaction import and undo. Admin only."""
import io
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import access, bulk, bulk_entities, db, timeline
from .admin import ADMIN, EXPORT_COLUMNS, IST_COLS, audit

router = APIRouter(prefix="/admin", tags=["admin-tools"])
IST = ZoneInfo("Asia/Kolkata")
NONE_TOKEN = "__none__"      # "no value" in a multi-select filter, e.g. orders with no payment status yet

# ------------------------------------------------------------------ filters
MULTI = {"stage": "v.stage", "channel": "v.channel", "submission_type": "v.submission_type",
         "delivery_status": "v.delivery_status", "payment_status": "v.payment_status", "ready_by": "v.ready_by",
         "colour_making_by": "v.colour_making_by", "delivered_by": "v.delivered_by", "created_by": "v.created_by"}
NUMBERS = {"min_amount": ("v.amount_received", ">="), "max_amount": ("v.amount_received", "<="),
           "min_cartage": ("v.cartage", ">="), "max_cartage": ("v.cartage", "<="),
           "min_hours": ("v.hours_to_deliver", ">="), "max_hours": ("v.hours_to_deliver", "<=")}
SORTS = {"sl_no": "v.sl_no", "order_date": "v.order_date", "dc_inv_no": "v.dc_inv_no", "stage": "v.stage",
         "delivery_status": "v.delivery_status", "payment_status": "v.payment_status", "channel": "v.channel",
         "amount_received": "v.amount_received", "cartage": "v.cartage", "hours_to_deliver": "v.hours_to_deliver"}
FILTER_KEYS = ({"date_from", "date_to", "q", "sl_no", "cancelled", "include_archived", "batch_id"}
               | set(MULTI) | set(NUMBERS))


def normalise_filter(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and clean a filter (from the query string or a saved definition). Unknown keys are dropped."""
    out: dict[str, Any] = {}
    try:
        for k in ("date_from", "date_to"):
            if raw.get(k):
                out[k] = date.fromisoformat(str(raw[k])).isoformat()
        if raw.get("q"):
            out["q"] = str(raw["q"]).strip()[:100]
        if raw.get("sl_no"):
            out["sl_no"] = str(raw["sl_no"]).strip()[:50]
        for k in MULTI:
            vals = raw.get(k)
            if isinstance(vals, str):
                vals = [vals]
            vals = [str(v)[:100] for v in (vals or []) if str(v).strip()][:50]
            if vals:
                out[k] = vals
        for k in NUMBERS:
            if raw.get(k) not in (None, ""):
                out[k] = float(raw[k])
        if raw.get("cancelled") in ("exclude", "only"):
            out["cancelled"] = raw["cancelled"]
        if str(raw.get("include_archived", "")).lower() in ("1", "true", "yes"):
            out["include_archived"] = True
        if raw.get("batch_id") not in (None, ""):
            out["batch_id"] = int(raw["batch_id"])
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"Invalid filter value: {e}")
    return out


def filter_from_query(request: Request) -> dict[str, Any]:
    qp = request.query_params
    raw: dict[str, Any] = {k: qp.get(k) for k in FILTER_KEYS if k not in MULTI}
    for k in MULTI:
        raw[k] = qp.getlist(k)
    return normalise_filter(raw)


def build_where(f: dict[str, Any]) -> tuple[str, list]:
    where, params = [], []
    if not f.get("include_archived"):
        where.append("v.archived_at IS NULL")
    if f.get("date_from"):
        where.append("v.order_date >= %s")
        params.append(f["date_from"])
    if f.get("date_to"):
        where.append("v.order_date <= %s")
        params.append(f["date_to"])
    if f.get("sl_no"):  # one order by its Sl number, or by DC/Inv number text
        if f["sl_no"].isdigit():  # a number is an exact Sl no. (or an exact DC/Inv no.), never a partial match
            where.append("(v.sl_no::text = %s OR v.dc_inv_no = %s)")
            params += [f["sl_no"], f["sl_no"]]
        else:
            where.append("v.dc_inv_no ILIKE %s")
            params.append(f"%{f['sl_no']}%")
    if f.get("q"):
        where.append("(v.dc_inv_no ILIKE %s OR v.shipping_location ILIKE %s OR v.detailed_remarks ILIKE %s)")
        params += [f"%{f['q']}%"] * 3
    for key, col in MULTI.items():
        vals = f.get(key)
        if not vals:
            continue
        real = [x for x in vals if x != NONE_TOKEN]
        parts, sub = [], []
        if real:
            parts.append(f"{col} = ANY(%s)")
            sub.append(real)
        if NONE_TOKEN in vals:
            parts.append(f"{col} IS NULL")
        where.append("(" + " OR ".join(parts) + ")")
        params += sub
    for key, (col, op) in NUMBERS.items():
        if key in f:
            where.append(f"{col} {op} %s")
            params.append(f[key])
    if f.get("cancelled") == "exclude":
        where.append("NOT v.is_cancelled")
    elif f.get("cancelled") == "only":
        where.append("v.is_cancelled")
    if "batch_id" in f:
        where.append("f.import_batch_id = %s")
        params.append(f["batch_id"])
    return (" AND ".join(where) or "TRUE"), params


ORDERS_FROM = "FROM v_orders_archive v LEFT JOIN fact_orders f ON f.order_key = v.order_key"
RESULT_COLUMNS = ("v.sl_no, v.dc_inv_no, v.order_date, v.stage, v.channel, v.submission_type, v.delivery_status, "
                  "v.payment_status, v.ready_by, v.colour_making_by, v.delivered_by, v.shipping_location, "
                  "v.detailed_remarks, v.material_delivery_datetime, v.date_of_receiving, v.amount_received, "
                  "v.cartage, v.hours_to_deliver, v.created_by, v.is_cancelled, "
                  "(v.archived_at IS NOT NULL) AS archived, f.import_batch_id")


@router.get("/orders/filter-options")
def filter_options(viewer=Depends(access.require_page(*access.DASHBOARD_PAGES))):
    with db.cursor() as cur:
        def col(sql):
            cur.execute(sql)
            return [next(iter(r.values())) for r in cur.fetchall()]
        return {
            "stage": ["Awaiting godown", "Awaiting dispatch", "Awaiting receiving", "Closed", "Cancelled"],
            "channel": col("SELECT channel_name FROM dim_order_channel ORDER BY lower(channel_name)"),
            "submission_type": col("SELECT type_name FROM dim_submission_type ORDER BY lower(type_name)"),
            "delivery_status": col("SELECT status_name FROM dim_delivery_status ORDER BY lower(status_name)"),
            "payment_status": col("SELECT status_name FROM dim_payment_status ORDER BY lower(status_name)"),
            "ready_by": col("SELECT full_name FROM dim_person WHERE person_role = 'ready_by' "
                            "ORDER BY lower(full_name)"),
            "colour_making_by": col("SELECT full_name FROM dim_person WHERE person_role = 'colour_making' "
                                    "ORDER BY lower(full_name)"),
            "delivered_by": col("SELECT full_name FROM dim_person WHERE person_role = 'delivery' "
                                "ORDER BY lower(full_name)"),
            "created_by": col("SELECT display_name FROM dim_user ORDER BY lower(display_name)"),
        }


@router.get("/orders/search")
def search_orders(request: Request, sort: str = "order_date", direction: str = Query(default="desc", alias="dir"),
                  limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                  viewer=Depends(access.require_page(*access.DASHBOARD_PAGES))):
    f = filter_from_query(request)
    where, params = build_where(f)
    order_col = SORTS.get(sort, "v.order_date")
    order_dir = "ASC" if direction.lower() == "asc" else "DESC"
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n {ORDERS_FROM} WHERE {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT {RESULT_COLUMNS} {ORDERS_FROM} WHERE {where} "
                    f"ORDER BY {order_col} {order_dir} NULLS LAST, v.sl_no DESC LIMIT %s OFFSET %s",
                    params + [limit, offset])
        rows = cur.fetchall()
    return {"total": total, "rows": rows, "filter": f}


@router.get("/orders/summary")
def orders_summary(viewer=Depends(access.require_page("dashboard_orders"))):
    """High-level counts for the top of the All orders page: the whole database, archived orders included."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE archived_at IS NULL) AS active, "
                    "count(*) FILTER (WHERE archived_at IS NOT NULL) AS archived, "
                    "count(*) FILTER (WHERE archived_at IS NULL AND NOT is_cancelled "
                    "AND stage <> 'Closed') AS in_progress, "
                    "count(*) FILTER (WHERE stage = 'Closed') AS closed, "
                    "count(*) FILTER (WHERE is_cancelled) AS cancelled, "
                    "min(order_date) AS first_order, max(order_date) AS last_order "
                    "FROM v_orders_archive")
        return cur.fetchone()


@router.get("/orders/{sl_no}/detail")
def order_detail(sl_no: int, viewer=Depends(access.require_page(*access.DASHBOARD_PAGES))):
    """Everything on one order (active or archived) plus its timeline, for the order-details popup."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM v_orders_archive WHERE sl_no = %s", (sl_no,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        tl = timeline.timeline_for(cur, row["order_key"], row)
    order = {k: v for k, v in row.items() if k != "order_key"}
    order["archived"] = order.pop("archived_at") is not None
    return {"order": order, "timeline": tl["events"], "timeline_note": tl["note"]}


EXPORT_CAP = 20000


@router.get("/orders/search.xlsx")
def export_search(request: Request, viewer=Depends(access.require_page("dashboard_orders"))):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    f = filter_from_query(request)
    where, params = build_where(f)
    cols = ", ".join("v." + c for _, c, _ in EXPORT_COLUMNS)
    with db.cursor() as cur:
        cur.execute(f"SELECT {cols} {ORDERS_FROM} WHERE {where} ORDER BY v.order_date, v.sl_no LIMIT %s",
                    params + [EXPORT_CAP + 1])
        rows = cur.fetchall()
        if len(rows) > EXPORT_CAP:
            raise HTTPException(400, f"More than {EXPORT_CAP} orders match - narrow the filter (for example by date).")
        audit(cur, viewer, "export.filtered", None, {"rows": len(rows), "filter": f})

    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append([h for h, _, _ in EXPORT_COLUMNS])
    fill = PatternFill("solid", fgColor="10263D")
    for i, (_, _, w) in enumerate(EXPORT_COLUMNS, 1):
        c = ws.cell(row=1, column=i)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    for r in rows:
        line = []
        for _, col, _ in EXPORT_COLUMNS:
            v = r[col]
            if col in IST_COLS and isinstance(v, datetime):
                v = v.astimezone(IST).replace(tzinfo=None)
            elif col == "hours_to_deliver" and v is not None:
                v = round(float(v), 1)
            elif col in ("amount_received", "cartage") and v is not None:
                v = float(v)
            elif col == "is_cancelled":
                v = "Yes" if v else "No"
            line.append(v)
        ws.append(line)
        for c in ws[ws.max_row]:
            if isinstance(c.value, str) and c.value.startswith("="):
                c.data_type = "s"           # text that starts with "=" must stay text, never a formula
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Filter")
    info.append(["Rows", len(rows)])
    info.append(["Generated (IST)", datetime.now(IST).strftime("%d-%b-%Y %H:%M")])
    for k, v in f.items():
        info.append([k, ", ".join(v) if isinstance(v, list) else str(v)])
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 22, 60
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="vardhman_orders_filtered.xlsx"'})


# ------------------------------------------------------------------ bulk upload
def _entity(name: str) -> dict:
    ent = bulk_entities.ENTITIES.get(name)
    if not ent:
        raise HTTPException(404, "Unknown upload type")
    return ent


def _public(report: dict, ent: dict) -> dict:
    return {**{k: v for k, v in report.items() if not k.startswith("_")},
            "columns": ent["columns"], "help": ent["help"], "label": ent["label"]}


class RowsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, Any]]
    filename: str | None = Field(default=None, max_length=255)


def _clean_rows(rows: list[dict], ent: dict) -> list[dict]:
    bulk.check_row_cap(len(rows))
    return bulk.canonical_rows([{k: ("" if v is None else str(v).strip()) if k != "_row" else v
                                 for k, v in r.items()} for r in rows], ent["columns"])


@router.get("/bulk/batches")
def list_batches(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT b.batch_id, b.entity, b.filename, b.row_count, b.created_at, b.undone_at, "
                    "u.display_name AS created_by FROM import_batch b LEFT JOIN dim_user u "
                    "ON u.user_key = b.created_by_user_key ORDER BY b.batch_id DESC LIMIT 30")
        return cur.fetchall()


@router.get("/bulk/{entity}/template.csv")
def template(entity: str, admin=Depends(ADMIN)):
    ent = _entity(entity)
    body = bulk.template_csv(ent["columns"], ent["examples"])
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{entity}_upload_template.csv"'})


# Template columns that must hold one of the values set up in the system -> where those values come from.
ORDER_LISTS = {"Order Via": "channel", "Type of Submission": "type", "Ready By": "person_ready_by",
               "Colour Making By": "person_colour_making", "Delivery Status": "delivery",
               "Delivered By": "person_delivery", "Payment Status": "payment"}


@router.get("/bulk/{entity}/template.xlsx")
def template_xlsx(entity: str, admin=Depends(ADMIN)):
    """Excel template: Template sheet (examples + dropdowns), Instructions sheet, Valid values sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    ent = _entity(entity)
    if entity == "orders":
        maps = bulk_entities._lookup_maps()
        lists = {col: [n for _, n in maps.get(kind, {}).values()] for col, kind in ORDER_LISTS.items()}
    else:
        lists = {"Role": list(bulk_entities.BULK_ROLES)}
    wb = Workbook()
    ws = wb.active
    ws.title = "Template"
    ws.append(ent["columns"])
    for ex in ent["examples"]:
        ws.append(ex)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="10263D")
        ws.column_dimensions[c.column_letter].width = max(16, len(str(c.value)) + 4)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, str):
                c.data_type = "s"                    # dates stay as typed text, formatted dd-mm-yyyy
    ws.freeze_panes = "A2"
    vals = wb.create_sheet("Valid values")
    for i, (col, items) in enumerate(lists.items(), start=1):
        vals.cell(row=1, column=i, value=col).font = Font(bold=True)
        vals.column_dimensions[get_column_letter(i)].width = 24
        for j, item in enumerate(items, start=2):
            vals.cell(row=j, column=i, value=item)
        if items:
            letter = get_column_letter(i)
            dv = DataValidation(type="list", formula1=f"='Valid values'!${letter}$2:${letter}${len(items) + 1}",
                                allow_blank=True, showErrorMessage=False)
            ws.add_data_validation(dv)
            col_letter = get_column_letter(ent["columns"].index(col) + 1)
            dv.add(f"{col_letter}2:{col_letter}{bulk.MAX_ROWS + 1}")
    info = wb.create_sheet("Instructions", 0)
    info.column_dimensions["A"].width = 110
    info.append([f"How to upload {ent['label']}"])
    info["A1"].font = Font(bold=True, size=14)
    for line in ("1. Fill the Template sheet, one row per line. Rows marked EXAMPLE are ignored - delete or keep them.",
                 "2. Columns with a dropdown must use one of the values on the Valid values sheet.",
                 "3. Save as .xlsx (or export the Template sheet as CSV) and upload it on Setup > Import.",
                 "4. The portal checks every row, tells you how many passed and failed, and what to fix in each failed "
                 "row. Nothing is saved until you press Import.", "", ent["help"]):
        info.append([line])
    wb.active = wb.sheetnames.index("Template")
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{entity}_upload_template.xlsx"'})


@router.post("/bulk/{entity}/validate")
async def validate_upload(entity: str, request: Request, filename: str = "", admin=Depends(ADMIN)):
    ent = _entity(entity)
    raw = await request.body()
    rows, names = bulk.read_table(raw)
    problem = bulk.header_problem(names, ent["required"], ent["columns"])
    if problem:
        return {"format_error": problem, "columns": ent["columns"], "help": ent["help"], "label": ent["label"]}
    if not rows:
        return {"format_error": "No data rows found. Fill in the template below its header row (rows marked EXAMPLE "
                                "are ignored).", "columns": ent["columns"], "help": ent["help"], "label": ent["label"]}
    bulk.check_row_cap(len(rows))
    return _public(ent["validate"](bulk.canonical_rows(rows, ent["columns"])), ent)


@router.post("/bulk/{entity}/revalidate")
def revalidate(entity: str, body: RowsIn, admin=Depends(ADMIN)):
    ent = _entity(entity)
    return _public(ent["validate"](_clean_rows(body.rows, ent)), ent)


@router.post("/bulk/{entity}/confirm")
def confirm(entity: str, body: RowsIn, admin=Depends(ADMIN)):
    ent = _entity(entity)
    rows = _clean_rows(body.rows, ent)
    if not rows:
        raise HTTPException(400, "Nothing to import.")
    report = ent["validate"](rows)           # never trust the client: check everything again right now
    if report["errors"]:
        return {"created": 0, "errors": report["errors"],
                "message": "Some rows no longer pass validation (for example someone else added the same order). "
                           "Nothing was imported."}
    with db.cursor() as cur:
        cur.execute("INSERT INTO import_batch (entity, filename, row_count, created_by_user_key) "
                    "VALUES (%s, %s, %s, %s) RETURNING batch_id", (entity, body.filename, len(rows), admin["user_key"]))
        batch_id = cur.fetchone()["batch_id"]
        result: dict[str, Any] = {"created": len(rows), "batch_id": batch_id}
        if entity == "orders":
            bulk_entities.insert_orders(cur, report["_normalised"], batch_id, admin)
        else:
            result["credentials"] = bulk_entities.insert_users(cur, report["_normalised"], batch_id, admin)
        audit(cur, admin, f"bulk.import.{entity}", f"batch {batch_id}", {"rows": len(rows), "file": body.filename})
    return result


@router.post("/bulk/batches/{batch_id}/undo")
def undo_batch(batch_id: int, admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT entity, created_at, undone_at FROM import_batch WHERE batch_id = %s FOR UPDATE",
                    (batch_id,))
        b = cur.fetchone()
        if not b:
            raise HTTPException(404, "Import not found")
        if b["undone_at"]:
            raise HTTPException(409, "This import was already undone")
        if b["entity"] == "orders":
            cur.execute("SELECT count(*) AS n FROM fact_orders WHERE import_batch_id = %s "
                        "AND (last_updated_at > %s OR archived_at IS NOT NULL)", (batch_id, b["created_at"]))
            edited = cur.fetchone()["n"]
            if edited:
                raise HTTPException(409, f"{edited} imported order(s) have been edited or archived since the import, "
                                         "so undoing would lose that work. Nothing was changed.")
            cur.execute("DELETE FROM fact_orders WHERE import_batch_id = %s", (batch_id,))
        else:
            cur.execute("SELECT count(*) AS n FROM dim_user u WHERE u.import_batch_id = %s "
                        "AND (NOT u.must_change_password OR EXISTS (SELECT 1 FROM fact_orders o "
                        "WHERE o.created_by_user_key = u.user_key OR o.last_updated_by_user_key = u.user_key))",
                        (batch_id,))
            if cur.fetchone()["n"]:
                raise HTTPException(409, "Some of these members have already signed in or logged orders, so the import "
                                         "can't be undone. Disable them under Members instead. Nothing was changed.")
            cur.execute("DELETE FROM dim_user WHERE import_batch_id = %s", (batch_id,))
        removed = cur.rowcount
        cur.execute("UPDATE import_batch SET undone_at = now() WHERE batch_id = %s", (batch_id,))
        audit(cur, admin, f"bulk.undo.{b['entity']}", f"batch {batch_id}", {"removed": removed})
    return {"removed": removed}
