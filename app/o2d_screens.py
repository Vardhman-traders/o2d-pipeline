"""Server-side screens for the O2D (sales order) app - replaces everything Apps Script's Code.gs did.

Code.gs was a thin proxy: it called this API, mapped names <-> keys, reshaped orders into camelCase for the
HTML, applied the dashboard date window, computed missing DC/invoice numbers and sent the WhatsApp alert.
All of that now lives here, in Python, next to the data. The order rules themselves (who may see or edit
what, the test-account guard, cancelled handling) are NOT duplicated: every write goes through
`main.update_order` / `main.create_order`, so there is one implementation of them.

Response shapes deliberately match what Code.gs returned ({ok, success, message, ...}, camelCase orders),
so the existing HTML only needs its server calls pointed at these routes (see static/sales/gas-shim.js).
"""
import base64
import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import auth, config, db, roles

log = logging.getLogger("o2d")
router = APIRouter(prefix="/o2d", tags=["o2d"])
IST = ZoneInfo("Asia/Kolkata")

ANY_VIEWER = auth.require_roles(*roles.ORDER_ROLES, "admin", "cashier", "accounts", "cartage")
ADMIN_ONLY = auth.require_roles("admin")
MAX_ROWS = 200
UNKNOWN_VALUE = "Unknown dropdown value selected. Please refresh and try again."

WHATSAPP_API_URL = os.environ.get("WHATSAPP_API_URL", "https://app.messageautosender.com/api/v1/message/create")
WHATSAPP_GROUP_ID = os.environ.get("WHATSAPP_GROUP_ID", "120363410985827601@g.us")


# ------------------------------------------------------------------ helpers
def today_ist() -> date:
    return datetime.now(IST).date()


def dashboard_window(today: date | None = None) -> list[str]:
    """[previous working day, today] as ISO dates. Monday is the weekly off, so 'previous day' skips it."""
    today = today or today_ist()
    prev = today - timedelta(days=1)
    while prev.weekday() == 0:  # Monday
        prev -= timedelta(days=1)
    return [prev.isoformat(), today.isoformat()]


def _iso_date(v) -> str:
    return str(v)[:10] if v else ""


def _iso_datetime(v) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M")
    return str(v)[:16]


def _num(v):
    return "" if v is None else float(v)


def map_order(o: dict) -> dict:
    """DB row (snake_case) -> the camelCase shape the HTML renders."""
    return {
        "slNo": o["sl_no"], "orderKey": o["order_key"],
        "orderRcvdDate": _iso_date(o["order_received_date"]),
        "orderVia": o["order_via"] or "", "shippingLocation": o["shipping_location"] or "",
        "dcNo": o["dc_inv_no"] or "", "typeOfSubmission": o["submission_type"] or "",
        "detailedRemarks": o["detailed_remarks"] or "", "readyByWhom": o["ready_by"] or "",
        "colourMakingBy": o["colour_making_by"] or "", "deliveryStatus": o["delivery_status"] or "",
        "materialDeliveryDateTime": _iso_datetime(o["material_delivery_datetime"]),
        "dateOfReceiving": _iso_date(o["date_of_receiving"]), "paymentStatus": o["payment_status"] or "",
        "amountReceived": _num(o["amount_received"]), "deliveredByWhom": o["delivered_by"] or "",
        "cartage": _num(o["cartage"]), "lastUpdatedBy": o["last_updated_by"] or "",
        "createdBy": o["created_by"] or "",
    }


def _lookup_rows(kind: str) -> list[dict]:
    table, key, name = config.LOOKUPS[kind]
    with db.cursor() as cur:
        cur.execute(f"SELECT {key} AS key, {name} AS name FROM {table} ORDER BY lower({name})")
        return cur.fetchall()


def _people_rows(person_role: str) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT person_key AS key, full_name AS name FROM dim_person WHERE person_role = %s "
                    "ORDER BY lower(full_name)", (person_role,))
        return cur.fetchall()


def _names(rows) -> list[str]:
    return [r["name"] for r in rows]


def _key_by_name(rows, name) -> int | None:
    if not name:
        return None
    wanted = str(name).strip().lower()
    for r in rows:
        if str(r["name"]).strip().lower() == wanted:
            return r["key"]
    return None


def _dropdowns(role: str) -> dict:
    """The lists each role's form needs (same rule as Code.gs getRoleData_)."""
    out: dict[str, list] = {k: [] for k in (
        "orderVia", "typeOfSubmission", "readyByWhom", "colourMakingBy",
        "deliveryStatus", "paymentStatus", "deliveredByWhom")}
    if role == "shop":
        out["orderVia"] = _names(_lookup_rows("channels"))
        out["typeOfSubmission"] = _names(_lookup_rows("submission-types"))
    elif role == "godown":
        out["readyByWhom"] = _names(_people_rows("ready_by"))
        out["colourMakingBy"] = _names(_people_rows("colour_making"))
        out["deliveryStatus"] = _names(_lookup_rows("delivery-statuses"))
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses"))
    elif role in ("godown_dispatch", "shop_dispatch"):
        out["deliveryStatus"] = _names(_lookup_rows("delivery-statuses"))
        out["deliveredByWhom"] = _names(_people_rows("delivery"))
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses"))
    elif role == "receiving":
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses"))
    return out


def _orders(user, *, date_from=None, date_to=None, q=None, archived=None, limit=MAX_ROWS, offset=0) -> list[dict]:
    from . import main  # late import: main includes this router
    rows = main.list_orders(delivery_status_key=None, date_from=date_from, date_to=date_to, q=q,
                            include_cancelled=True, archived=archived, limit=limit, offset=offset,
                            user=user)
    return [map_order(r) for r in rows]


def _write(user, sl_no: int, payload: dict, archived: bool | None = None) -> dict:
    """Apply a partial update through the one real update path; translate errors to {success: False}."""
    from . import main
    if any(k.endswith("_key") and v is None for k, v in payload.items()):
        return {"ok": True, "success": False, "message": UNKNOWN_VALUE}
    try:
        row = main.update_order(sl_no, main.OrderPatch(**payload), archived, user=user)
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    except ValueError as e:  # pydantic validation
        return {"ok": True, "success": False, "message": str(e)}
    return {"ok": True, "success": True, "order": map_order(row)}


def _has(v) -> bool:
    return v not in ("", None)


# ------------------------------------------------------------------ read screens
@router.get("/shop")
def shop_screen(user=Depends(ANY_VIEWER)):
    window = dashboard_window()
    return {"ok": True, "dropdowns": _dropdowns("shop"),
            "recentOrders": _orders(user, date_from=window[0], date_to=window[1]), "dateWindow": window}


@router.get("/godown")
def godown_screen(user=Depends(ANY_VIEWER)):
    orders = _orders(user)
    return {"ok": True, "dropdowns": _dropdowns("godown"),
            "pending": [o for o in orders if not o["deliveryStatus"]],
            "completed": [o for o in orders if o["deliveryStatus"]], "dateWindow": dashboard_window()}


@router.get("/dispatch")
def dispatch_screen(user=Depends(ANY_VIEWER)):
    orders = _orders(user)
    pending = [o for o in orders if not o["materialDeliveryDateTime"]]
    completed = [o for o in orders
                 if o["materialDeliveryDateTime"] and (not o["dateOfReceiving"] or not o["paymentStatus"])]
    for o in completed:
        o["stuckReason"] = "Receiving pending"
    return {"ok": True, "dropdowns": _dropdowns(user["role"]), "pending": pending, "completed": completed,
            "dateWindow": dashboard_window()}


@router.get("/receiving")
def receiving_screen(user=Depends(ANY_VIEWER)):
    orders = _orders(user)
    return {"ok": True, "dropdowns": _dropdowns("receiving"),
            "pending": [o for o in orders if not o["dateOfReceiving"] or not o["paymentStatus"]],
            "completed": [o for o in orders if o["dateOfReceiving"] and o["paymentStatus"]],
            "dateWindow": dashboard_window()}


@router.get("/admin/orders")
def admin_orders(date_from: date | None = None, date_to: date | None = None,
                 q: str | None = Query(default=None, max_length=100), archived: bool = False,
                 limit: int = Query(default=50, ge=1, le=MAX_ROWS), offset: int = Query(default=0, ge=0),
                 user=Depends(ADMIN_ONLY)):
    return {"ok": True, "orders": _orders(user, date_from=date_from, date_to=date_to, q=q,
                                          archived=True if archived else None, limit=limit, offset=offset)}


@router.get("/admin/form-options")
def admin_form_options(user=Depends(ADMIN_ONLY)):
    return {"ok": True, "dropdowns": {
        "orderVia": _names(_lookup_rows("channels")),
        "typeOfSubmission": _names(_lookup_rows("submission-types")),
        "deliveryStatus": _names(_lookup_rows("delivery-statuses")),
        "paymentStatus": _names(_lookup_rows("payment-statuses")),
        "readyByWhom": _names(_people_rows("ready_by")),
        "colourMakingBy": _names(_people_rows("colour_making")),
        "deliveredByWhom": _names(_people_rows("delivery"))}}


@router.get("/search")
def search_orders(date_: date | None = Query(default=None, alias="date"),
                  dc_no: str | None = Query(default=None, max_length=100), user=Depends(ANY_VIEWER)):
    return {"ok": True, "results": _orders(user, date_from=date_, date_to=date_, q=dc_no, limit=100)}


# ------------------------------------------------------------------ missing numbers
def _numeric_dc_nos(orders):
    small, large = [], []
    for o in orders:
        s = str(o["dcNo"]).strip()
        if not s.isdigit():
            continue
        (small if int(s) < 1000 else large).append(int(s))
    return small, large


def _gaps(nums):
    if not nums:
        return {"min": None, "max": None, "missing": []}
    lo, hi, present = min(nums), max(nums), set(nums)
    return {"min": lo, "max": hi, "missing": [n for n in range(lo, hi + 1) if n not in present]}


def _invoice_gaps(nums):
    """Invoice numbers are big and sparse: only gaps of 2..10 between neighbours count as 'missing'."""
    if not nums:
        return {"min": None, "max": None, "missing": []}
    nums = sorted(nums)
    missing = []
    for cur, nxt in zip(nums, nums[1:], strict=False):
        if 1 < nxt - cur <= 10:
            missing.extend(range(cur + 1, nxt))
    return {"min": nums[0], "max": nums[-1], "missing": missing}


def missing_numbers_for(user, day: date) -> dict:
    day_orders = [o for o in _orders(user, date_from=day, date_to=day) if o["orderRcvdDate"] == day.isoformat()]
    small, large = _numeric_dc_nos(day_orders)
    return {"ok": True,
            "challanByDate": [{"date": day.isoformat(), "challan": _gaps(small)}],
            "invoiceByDate": [{"date": day.isoformat(), "invoice": _invoice_gaps(large)}]}


@router.get("/missing-numbers")
def missing_numbers(date_: date | None = Query(default=None, alias="date"), user=Depends(ANY_VIEWER)):
    """Today for everyone; any date for admin. Only covers orders this user is allowed to see."""
    if date_ is not None and user["role"] != "admin":
        raise HTTPException(403, "Only admin can pick a date")
    return missing_numbers_for(user, date_ or today_ist())


# ------------------------------------------------------------------ write screens
class Form(BaseModel):
    """The camelCase form the HTML posts. Unknown keys are ignored (the HTML sends a few extras)."""
    model_config = ConfigDict(extra="ignore")
    orderRcvdDate: str | None = None
    orderVia: str | None = None
    dcNo: str | None = None
    typeOfSubmission: str | None = None
    shippingLocation: str | None = None
    detailedRemarks: str | None = None
    readyByWhom: str | None = None
    colourMakingBy: str | None = None
    deliveryStatus: str | None = None
    materialDeliveryDateTime: str | None = None
    deliveredByWhom: str | None = None
    cartage: Any = None
    dateOfReceiving: str | None = None
    paymentStatus: str | None = None
    amountReceived: Any = None


def _shop_payload(form: Form, channels, types) -> dict:
    return {"order_received_date": form.orderRcvdDate, "order_via_key": _key_by_name(channels, form.orderVia),
            "submission_type_key": _key_by_name(types, form.typeOfSubmission), "dc_inv_no": form.dcNo,
            "shipping_location": form.shippingLocation or None, "detailed_remarks": form.detailedRemarks or None}


def _missing_required(form: Form) -> list[str]:
    need = [("orderRcvdDate", "Order Received Date"), ("orderVia", "Order Received Thru"),
            ("dcNo", "DC/Inv No."), ("typeOfSubmission", "Type of Submission")]
    return [label for attr, label in need if not getattr(form, attr)]


@router.post("/orders")
def add_order(form: Form, user=Depends(auth.require_roles(*roles.CREATE_ORDER_ROLES))):
    from . import main
    missing = _missing_required(form)
    if missing:
        return {"ok": True, "success": False, "message": "Missing required field(s): " + ", ".join(missing)}
    payload = _shop_payload(form, _lookup_rows("channels"), _lookup_rows("submission-types"))
    if not payload["order_via_key"] or not payload["submission_type_key"]:
        return {"ok": True, "success": False, "message": UNKNOWN_VALUE}
    try:
        row = main.create_order(main.OrderIn(**payload), user=user)
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    return {"ok": True, "success": True, "slNo": row["sl_no"]}


@router.put("/orders/{sl_no}/shop")
def update_shop(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    missing = _missing_required(form)
    if missing:
        return {"ok": True, "success": False, "message": "Missing required field(s): " + ", ".join(missing)}
    return _write(user, sl_no, _shop_payload(form, _lookup_rows("channels"), _lookup_rows("submission-types")))


def _receiving_fields(form: Form, payments) -> dict:
    p: dict = {}
    if form.dateOfReceiving:
        p["date_of_receiving"] = form.dateOfReceiving
    if form.paymentStatus:
        p["payment_status_key"] = _key_by_name(payments, form.paymentStatus)
    if _has(form.amountReceived):
        p["amount_received"] = float(form.amountReceived)
    return p


@router.put("/orders/{sl_no}/godown")
def update_godown(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    p: dict = {}
    if form.readyByWhom:
        p["ready_by_person_key"] = _key_by_name(_people_rows("ready_by"), form.readyByWhom)
    if form.colourMakingBy:
        p["colour_making_person_key"] = _key_by_name(_people_rows("colour_making"), form.colourMakingBy)
    if form.deliveryStatus:
        p["delivery_status_key"] = _key_by_name(_lookup_rows("delivery-statuses"), form.deliveryStatus)
    if form.paymentStatus is not None or form.dateOfReceiving or _has(form.amountReceived):
        p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    return _write(user, sl_no, p)


@router.put("/orders/{sl_no}/dispatch")
def update_dispatch(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    before = None
    if user["role"] == "godown_dispatch":
        from . import main
        try:
            before = map_order(main.get_order(sl_no, archived=None, user=user))
        except HTTPException:
            before = None
    p: dict = {}
    if form.deliveryStatus:
        p["delivery_status_key"] = _key_by_name(_lookup_rows("delivery-statuses"), form.deliveryStatus)
    if form.materialDeliveryDateTime:
        p["material_delivery_datetime"] = form.materialDeliveryDateTime
    if form.deliveredByWhom:
        p["delivered_by_person_key"] = _key_by_name(_people_rows("delivery"), form.deliveredByWhom)
    if _has(form.cartage):
        p["cartage"] = float(form.cartage)
    if form.paymentStatus is not None:  # payment inputs are only sent when this role is allowed them
        p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    result = _write(user, sl_no, p)
    if result["success"] and user["role"] == "godown_dispatch" and before is not None:
        updated = result["order"]
        was_empty = not before["materialDeliveryDateTime"] and not before["deliveredByWhom"]
        now_filled = bool(updated["materialDeliveryDateTime"]) and bool(updated["deliveredByWhom"])
        if was_empty and now_filled:
            try:
                send_dispatch_alert(updated)
            except Exception:  # the order is saved; a failed alert must never undo or fail that
                log.exception("WhatsApp dispatch alert failed (order %s saved fine)", sl_no)
    result.pop("order", None)
    return result


@router.put("/orders/{sl_no}/receiving")
def update_receiving(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    result = _write(user, sl_no, _receiving_fields(form, _lookup_rows("payment-statuses")))
    result.pop("order", None)
    return result


@router.put("/orders/{sl_no}/admin")
def update_admin(sl_no: int, form: Form, archived: bool = False, user=Depends(ADMIN_ONLY)):
    p = _shop_payload(form, _lookup_rows("channels"), _lookup_rows("submission-types"))
    p = {k: v for k, v in p.items() if v is not None}  # admin edits only what was filled in
    if form.dcNo is not None:
        p["dc_inv_no"] = form.dcNo
    if form.shippingLocation is not None:  # admin may clear these two
        p["shipping_location"] = form.shippingLocation or None
    if form.detailedRemarks is not None:
        p["detailed_remarks"] = form.detailedRemarks or None
    if form.readyByWhom:
        p["ready_by_person_key"] = _key_by_name(_people_rows("ready_by"), form.readyByWhom)
    if form.colourMakingBy:
        p["colour_making_person_key"] = _key_by_name(_people_rows("colour_making"), form.colourMakingBy)
    if form.deliveryStatus:
        p["delivery_status_key"] = _key_by_name(_lookup_rows("delivery-statuses"), form.deliveryStatus)
    if form.materialDeliveryDateTime:
        p["material_delivery_datetime"] = form.materialDeliveryDateTime
    if form.deliveredByWhom:
        p["delivered_by_person_key"] = _key_by_name(_people_rows("delivery"), form.deliveredByWhom)
    if _has(form.cartage):
        p["cartage"] = float(form.cartage)
    p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    result = _write(user, sl_no, p, archived=True if archived else None)
    result.pop("order", None)
    return result


# ------------------------------------------------------------------ WhatsApp alert (was Code.gs)
def _fmt_date(iso: str) -> str:
    parts = str(iso or "").split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else str(iso or "")


def build_dispatch_message(o: dict) -> str:
    return ("New Dispatch - Godown\n\nOrder Date: " + _fmt_date(o["orderRcvdDate"]) +
            "\nDC No: " + (o["dcNo"] or "-") + "\nReady By: " + (o["readyByWhom"] or "-") +
            "\nDelivery Status: " + (o["deliveryStatus"] or "-") + "\nAddress: " + (o["shippingLocation"] or "-") +
            "\nRemarks: " + (o["detailedRemarks"] or "-") + "\nDelivered By: " + (o["deliveredByWhom"] or "-"))


def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https URL from config
        return resp.status, resp.read().decode("utf-8", "replace")


def send_dispatch_alert(o: dict, post=_http_post) -> dict:
    """Post the dispatch alert to the WhatsApp group. Credentials come from the environment
    (WHATSAPP_API_USERNAME / WHATSAPP_API_PASSWORD on Render); without them the alert is skipped."""
    user, pwd = os.environ.get("WHATSAPP_API_USERNAME"), os.environ.get("WHATSAPP_API_PASSWORD")
    if not user or not pwd:
        return {"skipped": True, "reason": "WhatsApp credentials not set."}
    body = json.dumps({"recipientIds": [WHATSAPP_GROUP_ID], "message": [build_dispatch_message(o)]}).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}
    code, text = post(WHATSAPP_API_URL, body, headers)
    return {"code": code, "body": text}
