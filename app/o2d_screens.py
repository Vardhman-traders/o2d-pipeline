"""Server-side screens for the O2D (sales order) app: the screen data, the dashboard date window, the missing DC/invoice
numbers and the WhatsApp alert, all computed here next to the data. The order rules themselves (who may see or edit
what, the test-account guard, cancelled handling) are NOT duplicated: every write goes through
`main.update_order` / `main.create_order`, so there is one implementation of them.

Response shapes are {ok, success, message, ...} with camelCase orders, which is what the O2D screens read
(see static/sales/api.js).
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, config, db, doc_numbers, roles, whatsapp

log = logging.getLogger("o2d")
router = APIRouter(prefix="/o2d", tags=["o2d"])
IST = ZoneInfo("Asia/Kolkata")

ANY_VIEWER = auth.require_roles(*roles.ORDER_ROLES, "admin", "cashier", "accounts", "cartage")
ADMIN_ONLY = auth.require_roles("admin")
MAX_ROWS = 200
UNKNOWN_VALUE = "Unknown dropdown value selected. Please refresh and try again."

# ------------------------------------------------------------------ helpers
def today_ist() -> date:
    return datetime.now(IST).date()


def dashboard_window(today: date | None = None, off_days: set[int] | None = None) -> list[str]:
    """[previous working day, today] as ISO dates. 'Previous day' skips the admin-configured
    weekly off day(s) (Setup > Weekly off) - Monday only, by default."""
    today = today or today_ist()
    off_days = config.weekly_off_days() if off_days is None else off_days
    prev = today - timedelta(days=1)
    while prev.weekday() in off_days:
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
        "colourMakingBy": o["colour_making_by"] or "", "deliveryStatus": config.display_label("delivery-statuses", o["delivery_status"]) or "",
        "materialDeliveryDateTime": _iso_datetime(o["material_delivery_datetime"]),
        "dateOfReceiving": _iso_date(o["date_of_receiving"]), "paymentStatus": o["payment_status"] or "",
        "amountReceived": _num(o["amount_received"]), "deliveredByWhom": o["delivered_by"] or "",
        "deliveredByDetail": o.get("delivered_by_detail") or "",
        "cartage": _num(o["cartage"]), "lastUpdatedBy": o["last_updated_by"] or "",
        "createdBy": o["created_by"] or "",
    }


def _lookup_rows(kind: str, screen: str | None = None) -> list[dict]:
    """One lookup list in the admin's order. With `screen`, only the values that screen offers. `name` is the wording shown in
    dropdowns (a built-in may be shown under another label); `raw` is the stored name."""
    table, key, name = config.LOOKUPS[kind]
    sql = f"SELECT {key} AS key, {name} AS name FROM {table}"
    params: list = []
    if screen and kind in config.LOOKUP_SCREENS:
        sql += " WHERE screens IS NULL OR %s = ANY(screens)"
        params.append(screen)
    with db.cursor() as cur:
        cur.execute(sql + f" ORDER BY sort_order, lower({name})", params)
        rows = cur.fetchall()
    shown = [{"key": r["key"], "raw": r["name"], "name": config.display_label(kind, r["name"])} for r in rows]
    labels = {r["name"].strip().lower() for r in shown if r["name"] != r["raw"]}
    # a separate value that is already called what a built-in is labelled would show twice: the built-in wins
    return [r for r in shown if r["name"] != r["raw"] or r["raw"].strip().lower() not in labels]


DISPATCH_SCOPES = {"shop_dispatch": ("both", "shop"), "godown_dispatch": ("both", "godown")}


def _people_rows(person_role: str, scopes: tuple | None = None) -> list[dict]:
    """People for one dropdown. `scopes` limits the delivery list to those offered on one dispatch screen."""
    sql, params = ("SELECT person_key AS key, full_name AS name FROM dim_person WHERE person_role = %s", [person_role])
    if scopes:
        sql += " AND dispatch_scope = ANY(%s)"
        params.append(list(scopes))
    with db.cursor() as cur:
        cur.execute(sql + " ORDER BY sort_order, lower(full_name)", params)
        return cur.fetchall()


def _names(rows) -> list[str]:
    return [r["name"] for r in rows]


def _key_by_name(rows, name) -> int | None:
    if not name:
        return None
    wanted = str(name).strip().lower()
    for r in rows:
        if wanted in (str(r["name"]).strip().lower(), str(r.get("raw", r["name"])).strip().lower()):
            return r["key"]
    return None


def _dropdowns(role: str) -> dict:
    """The lists each role's form needs (by screen)."""
    out: dict[str, list] = {k: [] for k in (
        "orderVia", "typeOfSubmission", "readyByWhom", "colourMakingBy",
        "deliveryStatus", "paymentStatus", "deliveredByWhom")}
    out["deliveredByCustomTerms"] = config.delivered_by_custom_terms()
    if role == "shop":
        out["orderVia"] = _names(_lookup_rows("channels"))
        out["typeOfSubmission"] = _names(_lookup_rows("submission-types"))
    elif role == "godown":
        out["readyByWhom"] = _names(_people_rows("ready_by"))
        out["colourMakingBy"] = _names(_people_rows("colour_making"))
        out["deliveryStatus"] = _names(_lookup_rows("delivery-statuses", role))
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses", role))
    elif role in ("godown_dispatch", "shop_dispatch"):
        out["deliveryStatus"] = _names(_lookup_rows("delivery-statuses", role))
        out["deliveredByWhom"] = _names(_people_rows("delivery", DISPATCH_SCOPES.get(role)))
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses", role))
    elif role == "receiving":
        out["paymentStatus"] = _names(_lookup_rows("payment-statuses", role))
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
def shop_screen(user=Depends(access.require_page("o2d_shop"))):
    window = dashboard_window()
    return {"ok": True, "dropdowns": _dropdowns("shop"),
            "recentOrders": _orders(user, date_from=window[0], date_to=window[1]), "dateWindow": window}


@router.get("/godown")
def godown_screen(user=Depends(access.require_page("o2d_godown"))):
    orders = _orders(user)
    return {"ok": True, "dropdowns": _dropdowns("godown"),
            "pending": [o for o in orders if not o["deliveryStatus"]],
            "completed": [o for o in orders if o["deliveryStatus"]], "dateWindow": dashboard_window()}


@router.get("/dispatch")
def dispatch_screen(view_as: str | None = Query(default=None),
                    user=Depends(access.require_page("o2d_shop_dispatch", "o2d_godown_dispatch"))):
    # Admin has no dispatch role of its own: it picks which dispatch screen it is looking at ("View as").
    # Everyone else always gets their own role's lists, whatever is passed.
    role = view_as if user["role"] == "admin" and view_as in DISPATCH_SCOPES else user["role"]
    orders = _orders(user)
    pending = [o for o in orders if not o["materialDeliveryDateTime"]]
    completed = [o for o in orders
                 if o["materialDeliveryDateTime"] and (not o["dateOfReceiving"] or not o["paymentStatus"])]
    for o in completed:
        o["stuckReason"] = "Receiving pending"
    return {"ok": True, "dropdowns": _dropdowns(role), "pending": pending, "completed": completed,
            "dateWindow": dashboard_window(), "photosEnabled": attachments.configured()}


@router.get("/receiving")
def receiving_screen(user=Depends(access.require_page("o2d_receiving"))):
    orders = _orders(user)
    return {"ok": True, "dropdowns": _dropdowns("receiving"),
            "pending": [o for o in orders if not o["dateOfReceiving"] or not o["paymentStatus"]],
            "completed": [o for o in orders if o["dateOfReceiving"] and o["paymentStatus"]],
            "dateWindow": dashboard_window(), "photosEnabled": attachments.configured()}


@router.get("/admin/orders")
def admin_orders(date_from: date | None = None, date_to: date | None = None,
                 q: str | None = Query(default=None, max_length=100), archived: bool = False,
                 limit: int = Query(default=50, ge=1, le=MAX_ROWS), offset: int = Query(default=0, ge=0),
                 user=Depends(ADMIN_ONLY)):
    return {"ok": True, "orders": _orders(user, date_from=date_from, date_to=date_to, q=q,
                                          archived=True if archived else None, limit=limit, offset=offset)}


STAGES = ("Awaiting godown", "Awaiting dispatch", "Awaiting receiving", "Closed", "Cancelled")
CARD_COLUMNS = ("sl_no, dc_inv_no, order_date, stage, created_by, amount_received, submission_type, channel, "
                "shipping_location, detailed_remarks, delivery_status, payment_status, ready_by, colour_making_by, "
                "delivered_by, cartage, material_delivery_datetime, date_of_receiving")


def _card(r: dict) -> dict:
    iso = lambda v: v.isoformat() if v is not None else ""  # noqa: E731
    return {"slNo": r["sl_no"], "dcNo": r["dc_inv_no"] or "", "orderDate": iso(r["order_date"]), "stage": r["stage"],
            "createdBy": r["created_by"] or "", "amount": float(r["amount_received"]) if r["amount_received"] is not None else None,
            "submissionType": r["submission_type"] or "", "channel": r["channel"] or "",
            "shippingLocation": r["shipping_location"] or "", "remarks": r["detailed_remarks"] or "",
            "deliveryStatus": r["delivery_status"] or "", "paymentStatus": r["payment_status"] or "",
            "readyBy": r["ready_by"] or "", "colourMakingBy": r["colour_making_by"] or "",
            "deliveredBy": r["delivered_by"] or "", "cartage": float(r["cartage"]) if r["cartage"] is not None else None,
            "deliveredAt": iso(r["material_delivery_datetime"]), "receivedOn": iso(r["date_of_receiving"])}


@router.get("/admin/kanban")
def admin_kanban(request: Request, per_stage: int = Query(default=25, ge=1, le=500),
                 user=Depends(access.require_page("o2d_overview"))):
    """One card per order, grouped by stage, for the admin Overview board. Takes the same filters as the dashboard
    (period + stage, order via, ... logged by). The scorecards count every stage under those filters; the Stage
    filter (which the scorecards also set) decides which columns the board shows."""
    from . import admin_tools  # late import: keeps the router modules independent at load time
    f = admin_tools.filter_from_query(request)
    chosen = f.pop("stage", None)
    where, params = admin_tools.build_where(f)  # active orders only: archived ones live on the All orders page
    cols = ", ".join("v." + c.strip() for c in CARD_COLUMNS.split(","))
    with db.cursor() as cur:
        cur.execute(f"SELECT v.stage, count(*) AS n {admin_tools.ORDERS_FROM} WHERE {where} GROUP BY v.stage", params)
        counts = {row["stage"]: row["n"] for row in cur.fetchall()}
        columns = {}
        for stage in STAGES:
            if chosen and stage not in chosen:
                continue
            cur.execute(f"SELECT {cols} {admin_tools.ORDERS_FROM} WHERE {where} AND v.stage = %s "
                        f"ORDER BY v.timestamp_created DESC LIMIT %s", params + [stage, per_stage])
            columns[stage] = [_card(r) for r in cur.fetchall()]
    return {"ok": True, "total": sum(counts.values()), "selected": chosen or [],
            "stages": [{"name": s, "count": counts.get(s, 0), "orders": columns.get(s, [])} for s in STAGES]}


def _created_by_names() -> list[str]:
    with db.cursor() as cur:
        cur.execute("SELECT display_name FROM dim_user ORDER BY lower(display_name)")
        return [r["display_name"] for r in cur.fetchall()]


# ------------------------------------------------------------------ reminders (admin -> the people who handle a stage)
STAGE_ROLES = {"Awaiting godown": ("godown",), "Awaiting dispatch": ("shop_dispatch", "godown_dispatch"),
               "Awaiting receiving": ("receiving",)}


class ReminderIn(BaseModel):
    sl_no: int
    message: str | None = None


@router.post("/admin/reminders")
def send_reminder(body: ReminderIn, user=Depends(access.require_page("o2d_overview"))):
    """Remind the people responsible for this order's current stage. Closed and cancelled orders have nobody to remind."""
    with db.cursor() as cur:
        cur.execute("SELECT order_key, stage, dc_inv_no FROM v_orders WHERE sl_no = %s", (body.sl_no,))
        o = cur.fetchone()
        if not o:
            raise HTTPException(404, "Order not found")
        if o["stage"] not in STAGE_ROLES:
            raise HTTPException(400, f"This order is {o['stage']}; there is nobody to remind.")
        cur.execute("INSERT INTO order_reminder (order_key, stage, message, sent_by_user_key, sent_by_name) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (o["order_key"], o["stage"], (body.message or "").strip()[:300] or None,
                     user["user_key"], user.get("display_name") or user["username"]))
    return {"ok": True, "stage": o["stage"], "roles": list(STAGE_ROLES[o["stage"]])}


@router.get("/reminders")
def my_reminders(role: str | None = Query(default=None), user=Depends(ANY_VIEWER)):
    """Open reminders for this user's screen. Admin may pass ?role= to preview a screen. A reminder disappears
    by itself once the order has moved on from the stage it was sent for."""
    r = role if (user["role"] == "admin" and role) else user["role"]
    stages = [st for st, rs in STAGE_ROLES.items() if r in rs]
    if not stages:
        return {"ok": True, "reminders": []}
    with db.cursor() as cur:
        cur.execute("SELECT m.reminder_key, m.message, m.sent_by_name, m.sent_at, m.stage, v.sl_no, v.dc_inv_no, "
                    "v.shipping_location, v.order_date FROM order_reminder m "
                    "JOIN v_orders v ON v.order_key = m.order_key "
                    "WHERE m.acked_at IS NULL AND m.stage = ANY(%s) AND v.stage = m.stage ORDER BY m.sent_at DESC",
                    (stages,))
        rows = cur.fetchall()
    return {"ok": True, "reminders": [{
        "id": x["reminder_key"], "message": x["message"] or "", "sentBy": x["sent_by_name"] or "Admin",
        "sentAt": x["sent_at"].isoformat(), "stage": x["stage"], "slNo": x["sl_no"], "dcNo": x["dc_inv_no"] or "",
        "shippingLocation": x["shipping_location"] or "", "orderDate": str(x["order_date"])} for x in rows]}


@router.post("/reminders/{reminder_key}/ack")
def ack_reminder(reminder_key: int, user=Depends(ANY_VIEWER)):
    with db.cursor() as cur:
        cur.execute("SELECT stage FROM order_reminder WHERE reminder_key = %s", (reminder_key,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(404, "Reminder not found")
        if user["role"] != "admin" and user["role"] not in STAGE_ROLES.get(m["stage"], ()):
            raise HTTPException(403, "This reminder is for another screen.")
        cur.execute("UPDATE order_reminder SET acked_at = now(), acked_by_name = %s "
                    "WHERE reminder_key = %s AND acked_at IS NULL", (user.get("display_name") or user["username"], reminder_key))
    return {"ok": True}


@router.get("/admin/form-options")
def admin_form_options(user=Depends(ADMIN_ONLY)):
    return {"ok": True, "dropdowns": {
        "orderVia": _names(_lookup_rows("channels")),
        "typeOfSubmission": _names(_lookup_rows("submission-types")),
        "deliveryStatus": _names(_lookup_rows("delivery-statuses")),
        "paymentStatus": _names(_lookup_rows("payment-statuses")),
        "readyByWhom": _names(_people_rows("ready_by")),
        "colourMakingBy": _names(_people_rows("colour_making")),
        "deliveredByWhom": _names(_people_rows("delivery")),
        "stage": list(STAGES), "createdBy": _created_by_names()}}


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


# ------------------------------------------------------------------ missing bill numbers
GAP_RESOLVERS = {"admin", "shop"}  # who may mark a missing number as "no bill to punch"


@router.get("/doc-gaps")
def doc_gaps(user=Depends(ANY_VIEWER)):
    """Bill numbers nobody has punched: Challan per day (from 1), Invoice per financial year."""
    with db.cursor() as cur:
        out = doc_numbers.open_gaps(cur, today_ist())
    return {"ok": True, "canResolve": user["role"] in GAP_RESOLVERS, "canPunch": _can_punch(user), **out}


def _can_punch(user) -> bool:
    """May this person click a missing number and enter it? The shop always could; anyone else only when an
    admin has switched on the "o2d_missing_entry" page for their role or for them personally."""
    if user["role"] in roles.CREATE_ORDER_ROLES and not access.is_view_only(user, "o2d_shop"):
        return True
    return access.effective_access(user).get("o2d_missing_entry") == "edit"


@router.get("/missing-entry/options")
def missing_entry_options(user=Depends(ANY_VIEWER)):
    if not _can_punch(user):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    return {"ok": True, "orderVia": _names(_lookup_rows("channels")), "typeOfSubmission": _names(_lookup_rows("submission-types"))}


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
    deliveredByDetail: str | None = None
    viewAs: str | None = None  # admin only: the dispatch screen being acted on
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
    access.require_edit(user, "o2d_shop")
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
    access.require_edit(user, "o2d_shop")
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
    access.require_edit(user, "o2d_godown")
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


def _delivered_by(p: dict, form: Form, scopes: tuple | None) -> str | None:
    """Fill the delivered-by fields of an update. Returns an error message, or None when fine.
    Porter / By Company / Transport (admin setting) need the free-text detail; every other person clears it."""
    key = _key_by_name(_people_rows("delivery", scopes), form.deliveredByWhom)
    if key is None:
        return UNKNOWN_VALUE
    p["delivered_by_person_key"] = key
    if config.needs_delivered_by_detail(form.deliveredByWhom):
        detail = " ".join((form.deliveredByDetail or "").split())
        if not detail:
            return f"Please fill in the details for '{form.deliveredByWhom}' (name / company / vehicle)."
        p["delivered_by_detail"] = detail[:200]
    else:
        p["delivered_by_detail"] = None
    return None


@router.put("/orders/{sl_no}/dispatch")
def update_dispatch(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    access.require_edit(user, "o2d_shop_dispatch", "o2d_godown_dispatch")
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
        err = _delivered_by(p, form, DISPATCH_SCOPES.get(user["role"] if user["role"] != "admin" else form.viewAs))
        if err:
            return {"ok": True, "success": False, "message": err}
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
            outcome = "failed"
            try:
                sent = send_dispatch_alert(updated)
                outcome = alert_outcome(sent)
            except Exception:  # the order is saved; a failed alert must never undo or fail that
                log.exception("WhatsApp dispatch alert failed (order %s saved fine)", sl_no)
            _record_alert(updated["orderKey"], outcome, user)
    result.pop("order", None)
    return result


@router.post("/orders/{sl_no}/whatsapp-resend")
def resend_dispatch_alert(sl_no: int, user=Depends(ANY_VIEWER)):
    """Send the dispatch WhatsApp alert for an order again (the first one goes automatically, once). Godown dispatch / admin."""
    access.require_edit(user, "o2d_godown_dispatch")
    from . import main
    try:
        order = map_order(main.get_order(sl_no, archived=None, user=user))
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    if not order["materialDeliveryDateTime"] or not order["deliveredByWhom"]:
        return {"ok": True, "success": False, "message": "Fill in the delivery date/time and Delivered by first."}
    outcome = "failed"
    try:
        sent = send_dispatch_alert(order, resend=True)
        outcome = alert_outcome(sent)
    except Exception:
        log.exception("WhatsApp dispatch alert re-send failed (order %s)", sl_no)
    _record_alert(order["orderKey"], outcome, user, resend=True)
    message = {"sent": "WhatsApp alert sent again.", "skipped": "WhatsApp is not set up on the server, so nothing was sent.",
               "off": "This alert is switched off in Setup > WhatsApp, so nothing was sent.",
               "failed": "WhatsApp did not accept the message. Nothing was sent - try again in a minute."}[outcome]
    return {"ok": True, "success": outcome == "sent", "message": message}


@router.put("/orders/{sl_no}/receiving")
def update_receiving(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    access.require_edit(user, "o2d_receiving")
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
        err = _delivered_by(p, form, None)
        if err:
            return {"ok": True, "success": False, "message": err}
    if _has(form.cartage):
        p["cartage"] = float(form.cartage)
    p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    result = _write(user, sl_no, p, archived=True if archived else None)
    result.pop("order", None)
    return result


@router.post("/missing-entry")
def missing_entry(form: Form, user=Depends(ANY_VIEWER)):
    """Enter an order for a missing bill number. Same rules as the shop's order form (all required fields,
    no duplicate numbers); the only difference is who may use it, which is the admin-set page access."""
    from . import main
    if not _can_punch(user):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    missing = _missing_required(form)
    if missing:
        return {"ok": True, "success": False, "message": "Missing required field(s): " + ", ".join(missing)}
    payload = _shop_payload(form, _lookup_rows("channels"), _lookup_rows("submission-types"))
    if not payload["order_via_key"] or not payload["submission_type_key"]:
        return {"ok": True, "success": False, "message": UNKNOWN_VALUE}
    try:
        row = main.insert_order(main.OrderIn(**payload), user, check_fields=False)
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    return {"ok": True, "success": True, "slNo": row["sl_no"]}


class GapVoid(BaseModel):
    doc_type: str
    series_key: str
    number_from: int
    number_to: int
    reason: str


@router.post("/doc-gaps/void")
def void_gap(body: GapVoid, user=Depends(ANY_VIEWER)):
    """Close a missing number (or a run of them) that needs no punch, e.g. the bill was voided in BUSY."""
    if user["role"] not in GAP_RESOLVERS:
        raise HTTPException(403, "Only admin or the shop can close a missing number.")
    access.require_edit(user, "o2d_shop")
    reason = body.reason.strip()
    if body.doc_type not in ("Challan", "Invoice"):
        raise HTTPException(422, "doc_type must be Challan or Invoice")
    if len(reason) < 3:
        raise HTTPException(422, "Please give a reason.")
    if not 0 < body.number_from <= body.number_to or body.number_to - body.number_from >= doc_numbers.MAX_LISTED:
        raise HTTPException(422, "Invalid number range.")
    with db.cursor() as cur:
        for n in range(body.number_from, body.number_to + 1):
            cur.execute("INSERT INTO doc_gap_void (doc_type, series_key, doc_no, reason, by_user_key, by_name) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (body.doc_type, body.series_key[:10], n, reason[:300], user["user_key"],
                         user.get("display_name") or user["username"]))
    return {"ok": True}


@router.get("/doc-check")
def doc_check(doc_type: str, date_: date = Query(alias="date"), number: int = Query(ge=0),
              user=Depends(auth.require_roles(*roles.CREATE_ORDER_ROLES))):
    """Used by the order form while a number is typed: is it already taken, does it skip ahead of the series?"""
    if doc_type not in ("Challan", "Invoice"):
        return {"ok": True, "duplicate": False, "skipped": []}
    with db.cursor() as cur:
        return {"ok": True, **doc_numbers.check_entry(cur, doc_type, date_, number)}


# ------------------------------------------------------------------ WhatsApp alert
def _fmt_date(iso: str) -> str:
    parts = str(iso or "").split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else str(iso or "")


def _dispatch_values(o: dict) -> dict:
    return {"order_date": _fmt_date(o["orderRcvdDate"]), "dc_no": o["dcNo"] or "-", "ready_by": o["readyByWhom"] or "-",
            "delivery_status": o["deliveryStatus"] or "-", "address": o["shippingLocation"] or "-",
            "remarks": o["detailedRemarks"] or "-", "delivered_by": o["deliveredByWhom"] or "-"}


def build_dispatch_message(o: dict, resend: bool = False) -> str:
    """The dispatch alert text: the template an admin keeps under Setup > WhatsApp, filled in for this order."""
    return ("(Sent again)\n" if resend else "") + whatsapp.render(whatsapp.get_alert("dispatch_godown")["template"], _dispatch_values(o))


ALERT_TITLES = {"sent": "WhatsApp dispatch alert sent to the group", "failed": "WhatsApp dispatch alert FAILED - not sent",
                "skipped": "WhatsApp dispatch alert not sent - WhatsApp is not set up",
                "off": "WhatsApp dispatch alert not sent - switched off in Setup"}


def _record_alert(order_key: int, outcome: str, user: dict, resend: bool = False):
    """Note on the order's timeline when the dispatch alert went out (or why it did not). Never blocks the save."""
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO order_event (order_key, event_type, title, by_user_key, by_name, by_role) "
                        "VALUES (%s, 'whatsapp', %s, %s, %s, %s)",
                        (order_key, ("Sent again - " if resend else "") + ALERT_TITLES[outcome], user["user_key"], user["display_name"], user["role"]))
    except Exception:
        log.exception("Could not note the WhatsApp alert on order %s", order_key)


def send_dispatch_alert(o: dict, post=whatsapp._http_post, resend: bool = False) -> dict:
    """Post the dispatch alert to its WhatsApp group (settings: Setup > WhatsApp; login: WHATSAPP_API_KEY).
    Nothing is sent, and the reason is returned, when the alert is off or WhatsApp is not set up."""
    return whatsapp.send_alert("dispatch_godown", _dispatch_values(o), prefix="(Sent again)\n" if resend else "", post=post)


def alert_outcome(result: dict | None) -> str:
    r = result or {}
    if r.get("skipped"):
        return "off" if "switched off" in r.get("reason", "") else "skipped"
    return "sent"
