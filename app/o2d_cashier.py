"""Cashier screen of the O2D app: the orders whose payment was taken in cash / bank (Cash, Bank, Cash + Bank ...),
listed so the cashier can confirm the money was received.

Everything is driven by settings, not code:
  - WHICH payment statuses land here is Setup > Dropdown values > Payment status > "Cashier dashboard" (column
    dim_payment_status.cashier_review). An order shows up the moment one of those statuses is selected on Shop
    Dispatch & Receiving or Godown Receiving (any screen that can set the payment status);
  - who may OPEN the screen and who may CONFIRM is the page access "o2d_cashier" (Setup > Access: tick it for a
    role, or give one person "Always allow" to confirm or "View only" to just look) - nothing is hard-wired to a role;
  - a confirmation is cleared by the database if the payment status or amount is changed afterwards (migration
    032), so the cashier always confirms what is currently recorded.

Cancelled orders are included on purpose: they are negative sales, so their amount is shown (and totalled) minus.

Two boxes, like the other dashboards: "Awaiting cashier" (never drops off until confirmed) and "Confirmed" (until
the 11 PM IST clean-up, the same rule every dashboard follows - the data itself stays on the order).
"""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import access, auth, config, db, payment_merge, roles, timeline
from .o2d_screens import MAX_ADVANCE, _advance_modes, _key_by_name, cleanup_cutoff

router = APIRouter(prefix="/o2d/cashier", tags=["o2d-cashier"])
IST = ZoneInfo("Asia/Kolkata")
PAGE = "o2d_cashier"

_CONFIRM_COLS = ("o.cashier_confirmed_at AS confirmed_at, o.cashier_note AS confirm_note, "
                 "cb.display_name AS confirmed_by, ps.cashier_review, ps.payments_split, pp.status AS push_status, "
                 "pp.merged_at AS pushed_merged_at, pp.edited_after_merge")
_CONFIRM_FROM = (" LEFT JOIN dim_user cb ON cb.user_key = o.cashier_confirmed_by_user_key"
                 " LEFT JOIN payment_push pp ON pp.order_key = o.order_key")


def _select() -> str:
    """The shared order query plus the cashier columns (inserted before FROM, joined after the shared joins)."""
    from . import main
    head, _, tail = main.ORDER_SELECT.partition("\nFROM fact_orders o")
    return f"{head},\n       {_CONFIRM_COLS}\nFROM fact_orders o{tail}{_CONFIRM_FROM}"


def _test_guard(user) -> str:
    # TEMPORARY test guard (see roles.py): test accounts only ever see TEST- orders
    return f" AND upper(o.dc_inv_no) LIKE '{roles.TEST_DC_PREFIX}%%'" if roles.is_test_user(user) else ""


def _ist(d) -> str:
    return d.astimezone(IST).strftime("%Y-%m-%dT%H:%M") if isinstance(d, datetime) else ""


def _iso(v) -> str:
    return str(v)[:10] if v else ""


def _cancelled(r: dict) -> bool:
    """Cancelled however it was recorded (flag, submission type or delivery status)."""
    return bool(r.get("is_cancelled")) or any(
        (r.get(k) or "").strip().lower() == "cancelled" for k in ("submission_type", "delivery_status"))


def _signed_amount(r: dict):
    """The amount as dashboards show it; a cancelled order is a negative sale, so it is shown (and totalled) minus."""
    amount = config.shown_amount(r["amount_received"], r.get("amount_effect"))
    return -abs(amount) if amount is not None and _cancelled(r) else amount


def _payments_state(r: dict) -> str:
    """Where the money is in Payments: "" (not pushed), awaiting (in a Sales or Sales Return tab), merged, edited."""
    if not r.get("push_status"):
        return ""
    if r.get("edited_after_merge"):
        return "edited"
    return "merged" if r["push_status"] == "merged" else "awaiting"


def _advance_pending(r: dict) -> bool:
    """An advance from the shop that has not reached receiving yet."""
    return bool(r.get("amount_locked_at")) and not r.get("date_of_receiving")


def _row(r: dict) -> dict:
    amount = _signed_amount(r)
    delivered_by = " - ".join(x for x in (r.get("delivered_by"), r.get("delivered_by_detail")) if x)
    return {"slNo": r["sl_no"], "orderDate": _iso(r["order_received_date"]), "dcNo": r["dc_inv_no"] or "",
            "submissionType": r["submission_type"] or "", "shippingLocation": r["shipping_location"] or "",
            "remarks": r["detailed_remarks"] or "",
            "deliveryStatus": config.display_label("delivery-statuses", r["delivery_status"]) or "",
            "paymentStatus": r["payment_status"] or "", "cancelled": _cancelled(r),
            "amountReceived": None if amount is None else float(amount),
            "dateOfReceiving": _iso(r["date_of_receiving"]), "deliveredBy": delivered_by,
            # which dashboard the receiving was entered on, from the delivery status's "Goes to" setting
            "receivedOn": "Shop (advance)" if _advance_pending(r) else
            ("Shop Dispatch & Receiving" if r.get("dispatch_route") == "shop" else "Godown Receiving"),
            # taken at the shop counter: locked, but the cashier may correct it until it is merged into Payments
            "advance": bool(r.get("amount_locked_at")), "amountRaw": None if r["amount_received"] is None else float(r["amount_received"]),
            "canCorrect": bool(r.get("amount_locked_at")) and not r.get("pushed_merged_at"),
            "receivedBy": r["last_updated_by"] or "", "lastUpdatedAt": _ist(r["last_updated_at"]),
            "confirmedAt": _ist(r["confirmed_at"]), "confirmedBy": r["confirmed_by"] or "",
            "note": r["confirm_note"] or "", "payments": _payments_state(r)}


def fetch_cashier(user) -> tuple[list[dict], list[dict]]:
    """(awaiting, confirmed): the latest-updated order first in the awaiting box (so today's orders are never buried under
    older ones), latest confirmation first in the other. Nothing is cut off: every awaiting order is listed."""
    guard = _test_guard(user)
    with db.cursor() as cur:
        cur.execute(_select() + " WHERE ps.cashier_review = 'yes' AND o.cashier_confirmed_at IS NULL"
                    f"{guard} ORDER BY o.last_updated_at DESC, o.sl_no DESC")
        awaiting = cur.fetchall()
        # confirmed ones stay listed whatever the status flag says now: the cashier did confirm them
        cur.execute(_select() + f" WHERE o.cashier_confirmed_at > %s{guard} "
                    "ORDER BY o.cashier_confirmed_at DESC, o.sl_no", [cleanup_cutoff()])
        confirmed = cur.fetchall()
    return awaiting, confirmed


def _summary(rows: list[dict]) -> dict:
    modes: dict[str, dict] = {}
    for r in rows:
        name = r["paymentStatus"] or "Not set"
        m = modes.setdefault(name, {"mode": name, "orders": 0, "amount": 0.0})
        m["orders"] += 1
        m["amount"] += r["amountReceived"] or 0
    total = sum((r["amountReceived"] or 0 for r in rows), 0.0)
    return {"orders": len(rows), "amount": round(total, 2),
            "byMode": sorted(modes.values(), key=lambda m: -m["amount"])}


@router.get("")
def cashier_screen(user=Depends(access.require_page(PAGE))):
    awaiting, confirmed = fetch_cashier(user)
    pend, done = [_row(r) for r in awaiting], [_row(r) for r in confirmed]
    return {"ok": True, "awaiting": pend, "confirmed": done, "canEdit": not access.is_view_only(user, PAGE),
            "modes": [m["name"] for m in _advance_modes()],     # what an advance can be corrected to

            "summary": {"awaiting": _summary(pend), "confirmed": _summary(done)}, "truncated": False}


@router.get("/visible-to")
def visible_to(user_key: int | None = None, admin=Depends(auth.require_roles("admin"))):
    """Admin only. Without `user_key`: the people who can open the Cashier dashboard. With it: the orders that person sees on it right now
    (the same lists the dashboard builds for them), so an admin can check which orders are visible to which user."""
    if user_key is None:
        return {"ok": True, "users": [{"userKey": u["user_key"], "name": u["display_name"], "role": u["role"]} for u in access.holders(PAGE)]}
    target = access.user_by_key(user_key)
    if PAGE not in access.effective_access(target):
        return {"ok": True, "canOpen": False, "name": target["display_name"], "awaiting": [], "confirmed": []}
    awaiting, confirmed = fetch_cashier(target)
    return {"ok": True, "canOpen": True, "name": target["display_name"],
            "awaiting": [_row(r) for r in awaiting], "confirmed": [_row(r) for r in confirmed]}


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirmed: bool = True                              # False = take a confirmation back (entered by mistake)
    note: str | None = Field(default=None, max_length=300)


def _event(cur, order_key: int, user: dict, title: str):
    cur.execute("INSERT INTO order_event (order_key, event_type, title, by_user_key, by_name, by_role, by_ip) "
                "VALUES (%s, 'cashier', %s, %s, %s, %s, %s)",
                (order_key, title[:120], user["user_key"], user["display_name"], user["role"], timeline.client_ip()))


def _refuse(message: str) -> dict:
    return {"ok": True, "success": False, "message": message}


class CorrectIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    amount: Decimal = Field(gt=0, le=MAX_ADVANCE, max_digits=10, decimal_places=2)    # as entered (the screens show it / 100)
    paymentStatus: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=300)


@router.put("/{sl_no}/correct")
def correct_advance(sl_no: int, body: CorrectIn, user=Depends(access.require_page(PAGE))):
    """The cashier corrects an advance entered at the shop (a typing mistake in the amount, or the wrong mode) while it is not merged into
    Payments yet. A confirmation already given is cleared by the database (migration 033) and the order comes back to be confirmed again.
    After the merge only an admin can change it (All orders > Edit)."""
    access.require_edit(user, PAGE)
    mode = _key_by_name(_advance_modes(), body.paymentStatus)
    if mode is None:
        return _refuse("Choose how it was paid from the list.")
    with db.cursor() as cur:
        cur.execute(_select() + f" WHERE o.sl_no = %s{_test_guard(user)} FOR UPDATE OF o", (sl_no,))
        o = cur.fetchone()
        if not o:
            raise HTTPException(404, "Order not found")
        if not o["amount_locked_at"]:
            return _refuse("Only an advance taken at the shop can be corrected here. Ask an admin to change this order.")
        if o["pushed_merged_at"]:
            return _refuse("This is already merged into Payments - ask an admin to correct it.")
        amount = body.amount.quantize(Decimal("0.01"))
        if o["amount_received"] == amount and o["payment_status_key"] == mode:
            return _refuse("Nothing was changed.")
        cur.execute("UPDATE fact_orders SET amount_received = %s, payment_status_key = %s, last_updated_by_user_key = %s, "
                    "last_updated_at = now() WHERE order_key = %s", (amount, mode, user["user_key"], o["order_key"]))
        old = config.shown_amount(o["amount_received"], o.get("amount_effect"))
        new = config.shown_amount(amount, o.get("amount_effect"))
        note = " ".join((body.note or "").split())
        _event(cur, o["order_key"], user, f"Cashier corrected the advance: Rs {Decimal(old or 0):,.2f} {o['payment_status']} -> "
               f"Rs {Decimal(new or 0):,.2f} {body.paymentStatus}" + (f" ({note})" if note else ""))
    return {"ok": True, "success": True}


@router.put("/{sl_no}/confirm")
def confirm_receipt(sl_no: int, body: ConfirmIn, user=Depends(access.require_page(PAGE))):
    """The cashier confirms the money for this order was received (or takes that back). Only orders whose current
    payment status is a cashier one can be confirmed; the order's own fields are never touched here."""
    access.require_edit(user, PAGE)
    note = " ".join((body.note or "").split()) or None
    with db.cursor() as cur:
        cur.execute(_select() + f" WHERE o.sl_no = %s{_test_guard(user)} FOR UPDATE OF o", (sl_no,))
        o = cur.fetchone()
        if not o:
            raise HTTPException(404, "Order not found")
        if not body.confirmed:
            if not o["confirmed_at"]:
                return _refuse("This order is not confirmed.")
            blocked = payment_merge.withdraw_order(cur, o["order_key"])
            if blocked:
                return _refuse(blocked)
            cur.execute("UPDATE fact_orders SET cashier_confirmed_at = NULL, cashier_confirmed_by_user_key = NULL, "
                        "cashier_note = NULL WHERE order_key = %s", (o["order_key"],))
            _event(cur, o["order_key"], user, "Cashier confirmation taken back")
            return {"ok": True, "success": True}
        if o["confirmed_at"]:
            return _refuse(f"Already confirmed by {o['confirmed_by'] or 'the cashier'}.")
        if o["cashier_review"] != "yes":
            return _refuse("This order's payment status is not one the cashier confirms.")
        cur.execute("UPDATE fact_orders SET cashier_confirmed_at = now(), cashier_confirmed_by_user_key = %s, "
                    "cashier_note = %s WHERE order_key = %s", (user["user_key"], note, o["order_key"]))
        amount = _signed_amount(o)
        shown = config.shown_amount(o["amount_received"], o.get("amount_effect"))
        negative = _cancelled(o) or o.get("amount_effect") == "subtract"
        payment_merge.push_order(cur, o, user, "return" if negative else "sales", None if shown is None else abs(shown))
        _event(cur, o["order_key"], user, f"Cashier confirmed {o['payment_status']} received"
               + (f" - Rs {Decimal(amount):,.2f}" if amount is not None else ""))
    return {"ok": True, "success": True}
