# ruff: noqa: E501
"""Setup > "Export & Delete": two admin-only buttons.

Export  - every dashboard's data in its own Excel file (orders, payments, delegation, purchase, VT Decor, attendance), zipped.
Delete  - removes every completed order (Closed or Cancelled, archived ones too) from the database, and the working data of the other
          dashboards: payments and expenses, delegation tasks / checklist, purchase entries, VT Decor clients, attendance records,
          reports, uploaded files. Orders that are NOT completed stay, as do the lists that drive the dropdowns, the members, the access
          settings, the WhatsApp settings and the audit log.
          A preview with the counts always comes first, and nothing is deleted without typing the confirmation word.
"""
import io
import logging
import zipfile
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from . import attachments, db
from .admin import ADMIN, audit

log = logging.getLogger("data_tools")
router = APIRouter(prefix="/admin/data", tags=["data"])

CONFIRM_WORD = "DELETE"
EXPORT_ROW_CAP = 200000

# ------------------------------------------------------------------ export: file -> [(sheet, SQL)]
_D = "JOIN dim_date {a} ON {a}.date_key = {col}"
EXPORT_FILES: dict[str, list[tuple[str, str]]] = {
    "O2D_Orders": [("Orders",
        "SELECT sl_no AS \"Sl no\", dc_inv_no AS \"DC / Inv no\", order_date AS \"Order date\", channel AS \"Order via\", submission_type AS \"Submission type\", "
        "delivery_status AS \"Delivery status\", payment_status AS \"Payment status\", ready_by AS \"Ready by\", colour_making_by AS \"Colour making by\", "
        "delivered_by AS \"Delivered by\", created_by AS \"Logged by\", last_updated_by AS \"Last updated by\", shipping_location AS \"Address\", "
        "detailed_remarks AS \"Remarks\", timestamp_created AS \"Logged at\", material_delivery_datetime AS \"Delivered at\", date_of_receiving AS \"Date received\", "
        "amount_received AS \"Amount received\", cartage AS \"Cartage\", stage AS \"Stage\", archived_at AS \"Archived at\" FROM v_orders_archive ORDER BY sl_no")],
    "Payments": [
        ("Entries",
         "SELECT t.txn_no AS \"Txn no\", d.full_date AS \"Date\", tt.type_name AS \"Type\", c.company_name AS \"Company\", p.party_name AS \"Party\", m.mode_name AS \"Mode\", "
         "t.amount AS \"Amount\", t.account_name AS \"Account\", t.invoice_no AS \"Invoice no\", t.remarks AS \"Remarks\", t.status AS \"Status\", "
         "cu.display_name AS \"Entered by\", au.display_name AS \"Approved / rejected by\", t.approved_at AS \"Decided at\", t.order_warning AS \"Order warning\" "
         "FROM fact_payment_txn t JOIN dim_date d ON d.date_key = t.txn_date_key JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key "
         "JOIN dim_company c ON c.company_key = t.company_key LEFT JOIN dim_party p ON p.party_key = t.party_key JOIN dim_payment_mode m ON m.mode_key = t.mode_key "
         "LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key LEFT JOIN dim_user au ON au.user_key = t.approved_by_user_key ORDER BY t.txn_key"),
        ("Expenses",
         "SELECT e.entry_no AS \"Entry no\", d.full_date AS \"Date\", a.account_name AS \"Account\", e.kind AS \"Kind\", e.amount AS \"Amount\", k.category_name AS \"Category\", "
         "su.display_name AS \"Spent by\", e.paid_to AS \"Paid to\", e.description AS \"Details\", cu.display_name AS \"Entered by\", e.voided_at AS \"Voided at\", e.void_reason AS \"Void reason\" "
         "FROM fact_expense_entry e JOIN dim_date d ON d.date_key = e.entry_date_key JOIN dim_expense_account a ON a.account_key = e.account_key "
         "LEFT JOIN dim_expense_category k ON k.category_key = e.category_key LEFT JOIN dim_user su ON su.user_key = e.spent_by_user_key "
         "LEFT JOIN dim_user cu ON cu.user_key = e.created_by_user_key ORDER BY e.entry_key")],
    "Delegation": [
        ("Tasks",
         "SELECT t.task_no AS \"Task no\", su.display_name AS \"Assigned to\", au.display_name AS \"Assigned by\", t.description AS \"Task\", ad.full_date AS \"Assigned on\", "
         "dd.full_date AS \"Deadline\", t.status AS \"Status\", cd.full_date AS \"Completed on\", t.remark AS \"Remark\", t.extra_work_notes AS \"Extra work\", t.score AS \"Score\", "
         "t.revision_count AS \"Revisions\", t.extension_count AS \"Extensions\" FROM fact_task t JOIN dim_user su ON su.user_key = t.staff_user_key "
         "LEFT JOIN dim_user au ON au.user_key = t.assigned_by_user_key LEFT JOIN dim_date ad ON ad.date_key = t.assigned_date_key "
         "LEFT JOIN dim_date dd ON dd.date_key = t.due_date_key LEFT JOIN dim_date cd ON cd.date_key = t.completed_date_key ORDER BY t.task_key"),
        ("Checklist",
         "SELECT c.title AS \"Checklist\", k.category_name AS \"Category\", u.display_name AS \"Assigned to\", c.frequency AS \"Frequency\", d.full_date AS \"Due date\", "
         "o.status AS \"Status\", o.marked_at AS \"Marked at\", mu.display_name AS \"Marked by\", o.remark AS \"Remark\" "
         "FROM fact_checklist_occurrence o JOIN dim_checklist c ON c.checklist_key = o.checklist_key JOIN dim_date d ON d.date_key = o.due_date_key "
         "LEFT JOIN dim_checklist_category k ON k.category_key = c.category_key LEFT JOIN dim_user u ON u.user_key = o.assignee_user_key "
         "LEFT JOIN dim_user mu ON mu.user_key = o.marked_by_user_key ORDER BY d.full_date, o.occurrence_key"),
        ("Extra work",
         "SELECT u.display_name AS \"Person\", d.full_date AS \"Date\", e.description AS \"Extra work\" FROM fact_extra_work e JOIN dim_user u ON u.user_key = e.user_key "
         "JOIN dim_date d ON d.date_key = e.work_date_key ORDER BY e.extra_key")],
    "Purchase": [("Entries",
        "SELECT e.purchase_key AS \"Entry\", s.site_name AS \"Site\", v.party_name AS \"Vendor\", md.full_date AS \"Material received\", idt.full_date AS \"Invoice date\", "
        "e.invoice_no AS \"Invoice no\", e.invoice_amount AS \"Invoice amount\", e.remarks AS \"Remarks\", cu.display_name AS \"Entered by\", e.created_at AS \"Entered at\" "
        "FROM fact_purchase_entry e JOIN dim_purchase_site s ON s.site_key = e.site_key JOIN dim_party v ON v.party_key = e.vendor_party_key "
        "LEFT JOIN dim_date md ON md.date_key = e.material_received_date_key LEFT JOIN dim_date idt ON idt.date_key = e.invoice_date_key "
        "LEFT JOIN dim_user cu ON cu.user_key = e.created_by_user_key ORDER BY e.purchase_key")],
    "VT_Decor": [
        ("Clients",
         "SELECT c.client_code AS \"Client\", c.client_name AS \"Name\", c.mobile AS \"Mobile\", c.address AS \"Address\", c.product AS \"Product\", t.type_name AS \"Product type\", "
         "c.contract_amount AS \"Contract amount\", c.start_date AS \"Start date\", c.status AS \"Status\", c.assigned_to AS \"Assigned to\", c.notes AS \"Notes\" "
         "FROM dim_decor_client c JOIN dim_decor_product_type t ON t.type_key = c.product_type_key ORDER BY c.client_key"),
        ("Payments",
         "SELECT c.client_code AS \"Client\", c.client_name AS \"Name\", COALESCE(s.stage_name, p.stage_text) AS \"Stage\", p.amount AS \"Amount\", m.mode_name AS \"Mode\", "
         "p.pay_date AS \"Date\", p.ref_no AS \"Reference\", p.remarks AS \"Remarks\" FROM fact_decor_payment p JOIN dim_decor_client c ON c.client_key = p.client_key "
         "LEFT JOIN dim_decor_stage s ON s.stage_key = p.stage_key LEFT JOIN dim_payment_mode m ON m.mode_key = p.mode_key ORDER BY p.payment_key"),
        ("Expenses",
         "SELECT c.client_code AS \"Client\", c.client_name AS \"Name\", e.expense_type AS \"Type\", e.amount AS \"Amount\", e.exp_date AS \"Date\", e.notes AS \"Notes\" "
         "FROM fact_decor_expense e JOIN dim_decor_client c ON c.client_key = e.client_key ORDER BY e.expense_key"),
        ("Items",
         "SELECT c.client_code AS \"Client\", i.item_index AS \"No\", i.product AS \"Product\", i.amount AS \"Amount\", i.item_date AS \"Date\", i.notes AS \"Notes\" "
         "FROM fact_decor_item i JOIN dim_decor_client c ON c.client_key = i.client_key ORDER BY i.item_key"),
        ("Quotations",
         "SELECT performa_no AS \"Performa no\", quote_date AS \"Date\", client_name AS \"Client\", mobile AS \"Mobile\", subtotal AS \"Subtotal\", cgst_pct AS \"CGST %\", "
         "sgst_pct AS \"SGST %\", grand_total AS \"Grand total\", created_by AS \"Created by\" FROM fact_decor_quotation ORDER BY quotation_key")],
    "Attendance": [
        ("Attendance",
         "SELECT e.employee_name AS \"Employee\", a.work_date AS \"Date\", a.check_in_time AS \"Check in\", a.check_out_time AS \"Check out\", l.location_name AS \"Location\", "
         "a.distance_m AS \"Distance (m)\", a.final_status AS \"Status\", a.hours_worked_min AS \"Minutes worked\", a.checkout_source AS \"Checked out by\" "
         "FROM fact_attendance a JOIN dim_att_employee e ON e.employee_key = a.employee_key LEFT JOIN dim_att_location l ON l.location_key = a.location_key "
         "ORDER BY a.work_date, e.employee_name"),
        ("Advances",
         "SELECT e.employee_name AS \"Employee\", a.advance_date AS \"Date\", a.amount AS \"Amount\", a.note AS \"Note\" FROM fact_att_advance a "
         "JOIN dim_att_employee e ON e.employee_key = a.employee_key ORDER BY a.advance_date, a.advance_key"),
        ("Employees",
         "SELECT employee_name AS \"Employee\", role_title AS \"Designation\", is_active AS \"Active\", added_on AS \"Added on\", monthly_salary AS \"Monthly salary\" "
         "FROM dim_att_employee ORDER BY lower(employee_name)")],
}


def _cell(v):
    """Excel-safe value: dates and numbers stay as they are, anything odd becomes text; timezones are dropped (Excel has none)."""
    if isinstance(v, datetime) and v.tzinfo:
        return v.replace(tzinfo=None)
    if isinstance(v, (str, int, float, date, datetime)) or v is None:
        return v
    return str(v)


def _workbook(sheets: list[tuple[str, list[str], list]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(list(headers))
        for r in rows:
            ws.append([_cell(v) for v in r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.get("/export.zip")
def export_everything(admin=Depends(ADMIN)):
    """One Excel file per dashboard (several sheets each), zipped."""
    out = io.BytesIO()
    counts: dict[str, int] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z, db.cursor() as cur:
        for fname, sheets in EXPORT_FILES.items():
            built = []
            for title, sql in sheets:
                cur.execute(sql + f" LIMIT {EXPORT_ROW_CAP + 1}")
                rows = cur.fetchall()
                if len(rows) > EXPORT_ROW_CAP:
                    raise HTTPException(400, f"{title} has more than {EXPORT_ROW_CAP} rows - export it from its own page in parts.")
                headers = list(rows[0].keys()) if rows else [c.name for c in cur.description]
                built.append((title, headers, [list(r.values()) for r in rows]))
                counts[f"{fname}/{title}"] = len(rows)
            z.writestr(f"{fname}.xlsx", _workbook(built))
        audit(cur, admin, "export.everything", None, {"sheets": counts})
    name = f"vardhman_export_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    return Response(out.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ delete: completed orders and the other dashboards' data
# "Completed" is what the archiving job calls fully complete: stage Closed or Cancelled (v_orders_archive also holds the archived ones).
COMPLETED_ORDERS = "(SELECT order_key FROM v_orders_archive WHERE stage IN ('Closed', 'Cancelled'))"
# [label, count SQL, delete SQL]; the order is the order they are deleted in (children before parents)
AREAS: list[tuple[str, str, str]] = [
    ("Completed orders (closed and cancelled, archived ones too)", f"SELECT count(*) FROM fact_orders WHERE order_key IN {COMPLETED_ORDERS}",
     f"DELETE FROM fact_orders WHERE order_key IN {COMPLETED_ORDERS}"),
    ("Cashier confirmations waiting for Payments", "SELECT count(*) FROM payment_push", "DELETE FROM payment_push"),
    ("Payments entries", "SELECT count(*) FROM fact_payment_txn", "DELETE FROM fact_payment_txn"),
    ("Expense entries (Payments)", "SELECT count(*) FROM fact_expense_entry", "DELETE FROM fact_expense_entry"),
    ("Delegation tasks", "SELECT count(*) FROM fact_task", "DELETE FROM fact_task"),
    ("Delegation weekly plans and extra work", "SELECT (SELECT count(*) FROM fact_weekly_plan) + (SELECT count(*) FROM fact_extra_work)",
     "DELETE FROM fact_weekly_plan; DELETE FROM fact_extra_work"),
    ("Checklist occurrences", "SELECT count(*) FROM fact_checklist_occurrence", "DELETE FROM fact_checklist_occurrence"),
    ("Purchase entries", "SELECT count(*) FROM fact_purchase_entry", "DELETE FROM fact_purchase_entry"),
    ("VT Decor clients (with their payments, expenses, items, photos)", "SELECT count(*) FROM dim_decor_client",
     "DELETE FROM dim_decor_client; DELETE FROM fact_decor_photo; DELETE FROM fact_decor_quotation; DELETE FROM decor_fixed_expense"),
    ("Attendance records and advances", "SELECT (SELECT count(*) FROM fact_attendance) + (SELECT count(*) FROM fact_att_advance)",
     "DELETE FROM fact_attendance; DELETE FROM fact_att_advance; DELETE FROM att_wa_log; DELETE FROM att_auto_run"),
    ("Challan uploads", "SELECT count(*) FROM fact_challan_upload", "DELETE FROM fact_challan_upload"),
    ("Daily delivery reports", "SELECT count(*) FROM daily_delivery_report", "DELETE FROM daily_delivery_report; DELETE FROM daily_report_run"),
    ("Closed missing bill numbers", "SELECT count(*) FROM doc_gap_void", "DELETE FROM doc_gap_void"),
]


def _counts(cur) -> tuple[list[dict], int, int]:
    out = []
    for label, count_sql, _ in AREAS:
        cur.execute(count_sql)
        out.append({"label": label, "count": list(cur.fetchone().values())[0]})
    cur.execute(f"SELECT count(*) AS n FROM fact_orders WHERE order_key NOT IN {COMPLETED_ORDERS}")
    kept = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM file_attachment")
    files = cur.fetchone()["n"]
    cur.execute(f"SELECT count(*) AS n FROM order_attachments WHERE order_key IN {COMPLETED_ORDERS}")
    files += cur.fetchone()["n"]
    return out, kept, files


@router.get("/delete-preview")
def delete_preview(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        areas, kept, files = _counts(cur)
    return {"areas": areas, "kept_orders": kept, "files": files, "confirm_word": CONFIRM_WORD,
            "kept_note": "Kept: the orders that are not completed, members, access settings, every dropdown list, WhatsApp settings, module settings and the audit log."}


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = ""


@router.post("/delete")
def delete_everything(body: DeleteIn, admin=Depends(ADMIN)):
    if body.confirm.strip() != CONFIRM_WORD:
        raise HTTPException(422, f"Type {CONFIRM_WORD} to confirm.")
    object_keys: list[str] = []
    with db.cursor() as cur:
        areas, kept, _ = _counts(cur)
        # files that go with the deleted data (removed from storage after the database change succeeds)
        cur.execute(f"SELECT object_key FROM order_attachments WHERE order_key IN {COMPLETED_ORDERS}")
        object_keys += [r["object_key"] for r in cur.fetchall()]
        cur.execute("SELECT object_key FROM file_attachment")
        object_keys += [r["object_key"] for r in cur.fetchall()]
        for _, _, delete_sql in AREAS:
            for stmt in delete_sql.split(";"):
                cur.execute(stmt)
        cur.execute("DELETE FROM file_attachment")
        cur.execute("UPDATE fact_orders SET cashier_confirmed_at = NULL, cashier_confirmed_by_user_key = NULL, cashier_note = NULL "
                    "WHERE cashier_confirmed_at IS NOT NULL")   # the payments they pointed to are gone: the cashier confirms again
        cur.execute("UPDATE import_batch SET undone_at = now() WHERE undone_at IS NULL")   # nothing left for an import's Undo to remove
        audit(cur, admin, "data.delete_all", None, {"deleted": {a["label"]: a["count"] for a in areas}, "kept_open_orders": kept})
    removed = _remove_files(object_keys)
    return {"ok": True, "deleted": areas, "kept_orders": kept, "files_removed": removed, "files_total": len(object_keys)}


def _remove_files(keys: list[str]) -> int:
    """Best effort: a file that cannot be removed from storage is only logged (the database no longer points to it)."""
    if not keys or not attachments.configured():
        return 0
    done = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            attachments.storage().delete_objects(Bucket=attachments._bucket(), Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            done += len(batch)
        except Exception:
            log.exception("could not remove %s stored file(s)", len(batch))
    return done
