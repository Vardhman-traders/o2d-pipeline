"""Server-side screens for the O2D (sales order) app: the screen data, the dashboard date window, the missing DC/invoice
numbers and the WhatsApp alert, all computed here next to the data. The order rules themselves (who may see or edit
what, the test-account guard, cancelled handling) are NOT duplicated: every write goes through
`main.update_order` / `main.create_order`, so there is one implementation of them.

Response shapes are {ok, success, message, ...} with camelCase orders, which is what the O2D screens read
(see static/sales/api.js).
"""
import contextvars
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import access, attachments, auth, config, db, doc_numbers, roles, timeline, whatsapp

log = logging.getLogger("o2d")
router = APIRouter(prefix="/o2d", tags=["o2d"])
IST = ZoneInfo("Asia/Kolkata")

ANY_VIEWER = access.require_page(*access.O2D_PAGES)   # anyone holding an O2D page; the page decides, not the role


def _need_edit(user, *pages: str):
    """Saving on a screen needs that screen's page with full (not "View only") access."""
    acc = access.effective_access(user)
    if any(acc.get(p) == "edit" for p in pages):
        return
    if any(p in acc for p in pages):
        raise HTTPException(403, "You have view-only access to this page, so you cannot save changes here.")
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)
ADMIN_ONLY = auth.require_roles("admin")
MAX_ROWS = 200
MAX_BULK = 100       # most orders one "Mark done" may save at once
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
        "dcNo": o["dc_inv_no"] or "", "typeOfSubmission": o["submission_type"] or "", "billSeries": o.get("bill_series") or "",
        "detailedRemarks": o["detailed_remarks"] or "", "readyByWhom": o["ready_by"] or "",
        "colourMakingBy": o["colour_making_by"] or "", "deliveryStatus": config.display_label("delivery-statuses", o["delivery_status"]) or "",
        "deliveryStatusRaw": o["delivery_status"] or "",   # the stored name the routing rules use (deliveryStatus may be a label)
        "materialDeliveryDateTime": _iso_datetime(o["material_delivery_datetime"]),
        "dateOfReceiving": _iso_date(o["date_of_receiving"]), "paymentStatus": o["payment_status"] or "",
        "amountReceived": _num(config.shown_amount(o["amount_received"], o.get("amount_effect"))),   # shown value (entered / 100, minus for a sales return)
        "dispatchRoute": o.get("dispatch_route") or "", "amountEffect": o.get("amount_effect") or "",
        "amountReceivedRaw": _num(o["amount_received"]),                      # exactly as entered: what the edit forms start from
        "lastUpdatedAt": o["last_updated_at"].isoformat() if o.get("last_updated_at") else "",
        "deliveredByWhom": o["delivered_by"] or "",
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
    rows = db.read(sql + f" ORDER BY sort_order, lower({name})", params, tables=(table,))
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
    return db.read(sql + " ORDER BY sort_order, lower(full_name)", params, tables=("dim_person",))


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


def _either_types() -> list[str]:
    """Submission types whose order can be tagged to Challan or Invoice: the form asks which one."""
    rows = db.read("SELECT type_name FROM dim_submission_type WHERE bill_series IN ('both', 'goods_return') ORDER BY sort_order, lower(type_name)",
                   tables=("dim_submission_type",))
    return [r["type_name"] for r in rows]


def _dropdowns(role: str) -> dict:
    """The lists each role's form needs (by screen)."""
    out: dict[str, list] = {k: [] for k in (
        "orderVia", "typeOfSubmission", "readyByWhom", "colourMakingBy",
        "deliveryStatus", "paymentStatus", "deliveredByWhom")}
    out["deliveredByCustomTerms"] = config.delivered_by_custom_terms()
    out["eitherTypes"] = _either_types()
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


def _with_photos(*lists: list[dict]) -> None:
    """Put each order's photo count on the rows (the lists show a camera badge you can click to see them)."""
    rows = [o for lst in lists for o in lst]
    counts = attachments.photo_counts(sorted({o["orderKey"] for o in rows}))
    for o in rows:
        o["photoCount"] = counts.get(o["orderKey"], 0)


# The most open / closed orders one screen sends. With the 11 PM clean-up off (the default) a screen's rule matches every closed order
# that is not archived, so closed rows get their own, smaller cap: every refresh (each 20 s, per open screen) used to send up to 500 rows,
# nearly all of them long-closed, and the list grew with every order. Open orders keep the full cap. With the clean-up on, the closed rows
# are only those of the current day, so they keep the full cap too.
OPEN_ROWS = 500
CLOSED_ROWS_NO_CLEANUP = 150


def _open_then_closed(user, where: str, open_order: str) -> list[dict]:
    """ORDER_SELECT rows matching `where` (on top of the user's visibility): the open orders (sorted by open_order) followed by the
    closed ones, most recently closed first. One query; each half has its own cap, so closed history can never push an open order out
    and an old order that is just closed still shows (it is the most recently updated)."""
    from . import main
    vis_sql, vis_params = roles.visibility(user)
    base = main.ORDER_SELECT + f" WHERE {vis_sql} AND {where} AND "
    closed_rows = OPEN_ROWS if config.cleanup_enabled() else CLOSED_ROWS_NO_CLEANUP
    with db.cursor() as cur:
        cur.execute(f"({base} NOT {roles._CLOSED} ORDER BY {open_order}, o.order_key DESC LIMIT %s) UNION ALL "
                    f"({base} {roles._CLOSED} ORDER BY o.last_updated_at DESC, o.order_key DESC LIMIT %s)",
                    vis_params + [OPEN_ROWS] + vis_params + [closed_rows])
        return [map_order(r) for r in cur.fetchall()]


def _screen_orders(user, screen: str) -> list[dict]:
    """The orders one screen lists (roles.screen_filter), chosen in the database so a long history can never push an open order out."""
    return _open_then_closed(user, roles.screen_filter(screen), "o.timestamp_created DESC")


def _write(user, sl_no: int, payload: dict, archived: bool | None = None, allow_closed: bool = False) -> dict:
    """Apply a partial update through the one real update path; translate errors to {success: False}."""
    from . import main
    if any(k.endswith("_key") and v is None for k, v in payload.items()):
        return {"ok": True, "success": False, "message": UNKNOWN_VALUE}
    try:
        row = main.save_order(sl_no, main.OrderPatch(**payload), archived, user, allow_closed=allow_closed or user["role"] == "admin")   # an admin may change an order at any stage
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    except ValueError as e:  # pydantic validation
        return {"ok": True, "success": False, "message": str(e)}
    return {"ok": True, "success": True, "order": map_order(row)}


def _has(v) -> bool:
    return v not in ("", None)


# ------------------------------------------------------------------ where an order shows (the order's journey)
# 1. Logged by the shop            -> Shop "New Orders" + Godown "Pending Orders". The Shop box is the tracker: the order stays there
#       and its Godown status / Delivered by columns update as it moves on. It is also the only place a closed order stays visible
#       (until 11 PM), see 5.
# 2. Godown sets a delivery status -> leaves Godown "Pending Orders"; Godown "Under Processing" until it is dispatched, and the status's
#       "Goes to" setting (Setup > Dropdown values > Delivery status) decides the dispatch screen:
#       Godown Dispatch  (e.g. Godown to Dispatch, Sent to the Company) -> its "Ready for dispatch"
#       Shop Dispatch & Receiving (Dispatch for Shop - stored as Shop - and Cancelled) -> its "Ready for dispatch"
#       Stays with the godown (e.g. Processing, Delivery for NextDay) -> no dispatch screen
# 3. Dispatch is filled in         -> Godown Dispatch order: "Awaiting receiving" on Godown Dispatch and on Godown Receiving
#                                     Shop Dispatch & Receiving order: the receiving details are filled on the same screen
# 4. Receiving date + payment set  -> the order is fully closed: leaves every "Awaiting receiving"; shows in "Received"
#       (Godown Receiving for godown-side orders, Shop Dispatch & Receiving for shop-side ones)
# 5. At 11 PM IST every dashboard resets: closed orders vanish from the screens (the data stays), open ones stay.
# The rules below decide the lists from the order itself, so they hold for admin ("see everything") exactly as for each role.
def _side(o: dict) -> str | None:
    """Where the order goes after the godown: 'shop' / 'godown' (a dispatch screen), 'stay' (stays with the godown) or None (no status yet)."""
    if not (o.get("deliveryStatusRaw") or "").strip():
        return None
    return {"shop": "shop", "none": "stay"}.get(o.get("dispatchRoute"), "godown")


def _is_cancelled_type(o: dict) -> bool:
    return (o.get("typeOfSubmission") or "").strip().lower() == "cancelled"


def _delivered(o: dict) -> bool:
    return bool(o["materialDeliveryDateTime"])


def _received(o: dict) -> bool:
    return bool(o["dateOfReceiving"] and o["paymentStatus"])


def awaiting_godown(o: dict) -> bool:
    return _side(o) is None and not _is_cancelled_type(o)


def under_processing(o: dict) -> bool:
    """The Godown dashboard's "Under Processing": the godown has set a status that keeps the order with the godown (route "stays": Processing,
    Delivery for NextDay ...) and it is not dispatched. A status that sends the order on - to Godown Dispatch (Godown to Dispatch, Sent to the
    Company), to the shop (Dispatch for Shop, Cancelled) or a sales return (amount subtracted) - means it has left the godown's work, so it
    is removed from this box (it shows on the screen it was sent to)."""
    return _side(o) == "stay" and o.get("amountEffect") != "subtract" and not _delivered(o)


def is_closed(o: dict) -> bool:
    """Fully closed: delivered and received (an order typed Cancelled needs no action either)."""
    return (_delivered(o) and _received(o)) or _is_cancelled_type(o)


def ready_for_dispatch(o: dict, side: str | None) -> bool:
    """Godown Dispatch: not delivered yet. Shop Dispatch & Receiving: not fully closed (delivered with receiving still open stays, tagged)."""
    s = _side(o)
    if s not in ("shop", "godown") or (side is not None and s != side):
        return False
    return not _delivered(o) if s == "godown" else not is_closed(o)


def awaiting_receiving_dispatch(o: dict) -> bool:
    """The Godown Dispatch screen's second box."""
    return _delivered(o) and not _received(o) and _side(o) in ("godown", None)


def awaiting_receiving_godown(o: dict) -> bool:
    return _delivered(o) and not _received(o) and _side(o) != "shop"


def received_shop(o: dict) -> bool:
    """The Shop Dispatch & Receiving screen's "Received" box: closed, and not yet cleaned up at 11 PM."""
    return _side(o) == "shop" and _delivered(o) and _received(o) and closed_since_cleanup(o)


CLEANUP_AT = time(23, 0)


def cleanup_cutoff(now: datetime | None = None) -> datetime:
    """The most recent 11 PM IST, when the clean-up is switched on (Setup > Module settings; off by default). Fully closed orders closed
    before it are then no longer listed: the dashboards start each day with only what is still open (the data itself stays in the database
    and on the Overview / All orders pages). Switched off, closed orders stay on the dashboards until they are archived."""
    if not config.cleanup_enabled():   # switched off: nothing is ever cleaned up, so every closed order still counts
        return datetime(2000, 1, 1, tzinfo=IST)
    now = now or datetime.now(IST)
    cut = datetime.combine(now.date(), CLEANUP_AT, tzinfo=IST)
    return cut if now >= cut else cut - timedelta(days=1)


def closed_since_cleanup(o: dict, now: datetime | None = None) -> bool:
    try:
        return datetime.fromisoformat(o["lastUpdatedAt"]) > cleanup_cutoff(now)
    except (KeyError, ValueError, TypeError):
        return True


def received_godown(o: dict) -> bool:
    return _delivered(o) and _received(o) and _side(o) != "shop" and closed_since_cleanup(o)


def _shop_box_orders(user) -> list[dict]:
    """The Shop's own orders that are not fully closed or were closed since the last 11 PM - chosen in the database, so a long history
    of closed orders can never push an old open one out of the list."""
    open_or_recent = (f"((NOT {roles._CLOSED} AND COALESCE(lower(btrim(st.type_name)), '') <> 'cancelled') OR {roles.since_cleanup_sql()})")
    return _open_then_closed(user, open_or_recent, "o.timestamp_created DESC")


# ------------------------------------------------------------------ read screens
@router.get("/shop")
def shop_screen(user=Depends(access.require_page("o2d_shop"))):
    # "recentOrders" is the Shop's New Orders box: every order that is not fully closed, plus the ones closed since the last 11 PM
    # (the status columns update as the order moves on; at 11 PM the closed ones drop off)
    recent = [o for o in _shop_box_orders(user) if not is_closed(o) or closed_since_cleanup(o)]
    _with_photos(recent)
    return {"ok": True, "dropdowns": _dropdowns("shop"), "recentOrders": recent, "dateWindow": dashboard_window()}


@router.get("/godown")
def godown_screen(user=Depends(access.require_page("o2d_godown"))):
    orders = _screen_orders(user, "godown")
    pending, completed = [o for o in orders if awaiting_godown(o)], [o for o in orders if under_processing(o)]
    _with_photos(pending, completed)
    return {"ok": True, "dropdowns": _dropdowns("godown"), "pending": pending, "completed": completed, "dateWindow": dashboard_window()}


@router.get("/dispatch")
def dispatch_screen(view_as: str | None = Query(default=None),
                    user=Depends(access.require_page("o2d_shop_dispatch", "o2d_godown_dispatch"))):
    # The side comes from the SCREEN being opened, never from who is looking: Godown Dispatch lists godown-route orders only and
    # Shop Dispatch & Receiving lists shop-route orders only, for admin and for any role that was given the page.
    pages = {"shop_dispatch": "o2d_shop_dispatch", "godown_dispatch": "o2d_godown_dispatch"}
    held = {v for v, pg in pages.items() if pg in access.effective_pages(user)}
    if user["role"] == "admin":
        held = set(pages)
    # the screen asked for, if this person holds its page; otherwise the one they hold (their own screen first)
    role = view_as if view_as in held else (user["role"] if user["role"] in held else (sorted(held)[0] if held else "godown_dispatch"))
    side = {"shop_dispatch": "shop", "godown_dispatch": "godown"}[role]
    orders = _screen_orders(user, role)
    pending = [o for o in orders if ready_for_dispatch(o, side)]
    for o in pending:
        if _delivered(o):
            o["stuckReason"] = "Receiving pending"      # shop side: dispatched, receiving details still to fill
    # second box: Godown Dispatch -> "Awaiting receiving"; Shop Dispatch & Receiving -> "Received" (closed, until 11 PM)
    completed = [o for o in orders if (awaiting_receiving_dispatch(o) if side == "godown" else received_shop(o) if side == "shop"
                                       else awaiting_receiving_dispatch(o) or received_shop(o))]
    for o in completed:
        if not _received(o):
            o["stuckReason"] = "Receiving pending"
    _with_photos(pending, completed)
    return {"ok": True, "dropdowns": _dropdowns(role), "pending": pending, "completed": completed,
            "dateWindow": dashboard_window(), "photosEnabled": attachments.configured()}


@router.get("/receiving")
def receiving_screen(user=Depends(access.require_page("o2d_receiving"))):
    orders = _screen_orders(user, "receiving")
    pending, completed = [o for o in orders if awaiting_receiving_godown(o)], [o for o in orders if received_godown(o)]
    _with_photos(pending, completed)
    return {"ok": True, "dropdowns": _dropdowns("receiving"), "pending": pending, "completed": completed,
            "dateWindow": dashboard_window(), "photosEnabled": attachments.configured()}


@router.get("/admin/orders")
def admin_orders(date_from: date | None = None, date_to: date | None = None,
                 q: str | None = Query(default=None, max_length=100), archived: bool = False,
                 limit: int = Query(default=50, ge=1, le=MAX_ROWS), offset: int = Query(default=0, ge=0),
                 user=Depends(ADMIN_ONLY)):
    return {"ok": True, "orders": _orders(user, date_from=date_from, date_to=date_to, q=q,
                                          archived=True if archived else None, limit=limit, offset=offset)}


STAGES = ("Awaiting godown", "Awaiting dispatch", "Awaiting receiving", "Closed", "Cancelled")
UNDER_PROCESSING = "Under processing (godown)"   # not a stage: the Godown dashboard's Under Processing, selectable like one
# same rule as under_processing() above, in SQL on v_orders_archive (alias v): a status that keeps the order with the godown, not yet dispatched
UNDER_PROCESSING_SQL = ("(v.material_delivery_datetime IS NULL AND EXISTS (SELECT 1 FROM dim_delivery_status uds "
                        "WHERE uds.status_name = v.delivery_status AND uds.dispatch_route = 'none' AND uds.amount_effect IS NULL))")
CARD_COLUMNS = ("order_key, sl_no, dc_inv_no, order_date, stage, created_by, amount_received, submission_type, channel, "
                "shipping_location, detailed_remarks, delivery_status, payment_status, ready_by, colour_making_by, "
                "delivered_by, cartage, material_delivery_datetime, date_of_receiving")


def _card(r: dict) -> dict:
    iso = lambda v: v.isoformat() if v is not None else ""  # noqa: E731
    return {"slNo": r["sl_no"], "orderKey": r["order_key"], "dcNo": r["dc_inv_no"] or "", "orderDate": iso(r["order_date"]), "stage": r["stage"],
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
    find = (request.query_params.get("find") or "").strip()[:50]
    if find:   # the board's search box: an S number (exact) or part of an invoice / DC number
        where += " AND (v.sl_no::text = %s OR v.dc_inv_no ILIKE %s)"
        params = params + [find, f"%{find}%"]
    cols = ", ".join("v." + c.strip() for c in CARD_COLUMNS.split(","))
    with db.cursor() as cur:
        cur.execute(f"SELECT v.stage, count(*) AS n {admin_tools.ORDERS_FROM} WHERE {where} GROUP BY v.stage", params)
        counts = {row["stage"]: row["n"] for row in cur.fetchall()}
        # the Godown dashboard's "Under Processing": a delivery status is set and the order has not been dispatched yet
        cur.execute(f"SELECT count(*) AS n {admin_tools.ORDERS_FROM} WHERE {where} "
                    f"AND {UNDER_PROCESSING_SQL}", params)
        under_processing_count = cur.fetchone()["n"]
        columns = {}
        for stage in STAGES:
            if chosen and stage not in chosen:
                continue
            cur.execute(f"SELECT {cols} {admin_tools.ORDERS_FROM} WHERE {where} AND v.stage = %s "
                        f"ORDER BY v.timestamp_created DESC LIMIT %s", params + [stage, per_stage])
            columns[stage] = [_card(r) for r in cur.fetchall()]
        if chosen and UNDER_PROCESSING in chosen:       # the Under processing scorecard was clicked: its own column
            cur.execute(f"SELECT {cols} {admin_tools.ORDERS_FROM} WHERE {where} AND {UNDER_PROCESSING_SQL} "
                        "ORDER BY v.timestamp_created DESC LIMIT %s", params + [per_stage])
            columns[UNDER_PROCESSING] = [_card(r) for r in cur.fetchall()]
    shown = [o for lst in columns.values() for o in lst]
    photos = attachments.photo_counts(sorted({o["orderKey"] for o in shown}))   # the cards carry a camera badge
    for o in shown:
        o["photoCount"] = photos.get(o["orderKey"], 0)
    stages = [{"name": s, "count": counts.get(s, 0), "orders": columns.get(s, [])} for s in STAGES]
    if chosen and UNDER_PROCESSING in chosen:
        stages.append({"name": UNDER_PROCESSING, "count": under_processing_count, "orders": columns.get(UNDER_PROCESSING, [])})
    return {"ok": True, "total": sum(counts.values()), "selected": chosen or [], "underProcessing": under_processing_count,
            "stages": stages}


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
        "typeOfSubmission": _names(_lookup_rows("submission-types")), "eitherTypes": _either_types(),
        "deliveryStatus": _names(_lookup_rows("delivery-statuses")),
        "paymentStatus": _names(_lookup_rows("payment-statuses")),
        "readyByWhom": _names(_people_rows("ready_by")),
        "colourMakingBy": _names(_people_rows("colour_making")),
        "deliveredByWhom": _names(_people_rows("delivery")),
        "stage": list(STAGES), "createdBy": _created_by_names(),
        "billKind": ["Challan", "Invoice"], "saleKind": ["Sales", "Sales return"]}}


@router.get("/search")
def search_orders(date_: date | None = Query(default=None, alias="date"),
                  dc_no: str | None = Query(default=None, max_length=100), user=Depends(ANY_VIEWER)):
    from . import main
    rows = main.list_orders(delivery_status_key=None, date_from=date_, date_to=date_, q=dc_no, include_cancelled=True, archived=None,
                            limit=100, offset=0, user=user)
    found = []
    for r in rows:
        o = map_order(r)
        # the same stage rule as v_orders, so a search result reads like the Overview's order details
        o["stage"] = ("Cancelled" if r["is_cancelled"] or _is_cancelled_type(o) else "Awaiting godown" if not r["delivery_status_key"]
                      else "Awaiting dispatch" if not _delivered(o) else "Awaiting receiving" if not _received(o) else "Closed")
        found.append(o)
    _with_photos(found)
    return {"ok": True, "results": found}


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
def _can_resolve(user) -> bool:
    """Who may mark a missing number as "no bill to punch": anyone with full access to the Shop screen."""
    return access.effective_access(user).get("o2d_shop") == "edit"


@router.get("/doc-gaps")
def doc_gaps(user=Depends(ANY_VIEWER)):
    """Bill numbers nobody has punched: Challan per day (from 1), Invoice per financial year."""
    with db.cursor() as cur:
        out = doc_numbers.open_gaps(cur, today_ist())
        series_types = doc_numbers.types_by_series(cur)
    return {"ok": True, "canResolve": _can_resolve(user), "canPunch": _can_punch(user), "seriesTypes": series_types, **out}


def _can_punch(user) -> bool:
    """May this person click a missing number and enter it? The shop always could; anyone else only when an
    admin has switched on the "o2d_missing_entry" page for their role or for them personally."""
    acc = access.effective_access(user)
    return acc.get("o2d_shop") == "edit" or acc.get("o2d_missing_entry") == "edit"


@router.get("/missing-entry/options")
def missing_entry_options(user=Depends(ANY_VIEWER)):
    if not _can_punch(user):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    return {"ok": True, "orderVia": _names(_lookup_rows("channels")), "typeOfSubmission": _names(_lookup_rows("submission-types")),
            "eitherTypes": _either_types()}


# ------------------------------------------------------------------ write screens
class Form(BaseModel):
    """The camelCase form the HTML posts. Unknown keys are ignored (the HTML sends a few extras)."""
    model_config = ConfigDict(extra="ignore")
    orderRcvdDate: str | None = None
    orderVia: str | None = None
    dcNo: str | None = None
    typeOfSubmission: str | None = None
    billSeries: str | None = None     # "challan" / "invoice": which bill series an "either" submission type is tagged to
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
    photosFollow: bool = False     # dispatch save: photos are uploaded next, so the WhatsApp message waits for them (the page asks for it afterwards)


def _chosen_series(form: Form) -> str | None:
    """The Challan / Invoice tag, kept only when the submission type really is an "either" type (anything else is not tagged)."""
    choice = (form.billSeries or "").strip().lower()
    if choice not in ("challan", "invoice") or not form.typeOfSubmission:
        return None
    with db.cursor() as cur:
        return choice if doc_numbers.series_of(cur, form.typeOfSubmission) in ("Both", "Goods Return") else None


def _shop_payload(form: Form, channels, types) -> dict:
    return {"order_received_date": form.orderRcvdDate, "order_via_key": _key_by_name(channels, form.orderVia),
            "submission_type_key": _key_by_name(types, form.typeOfSubmission), "dc_inv_no": form.dcNo,
            "bill_series": _chosen_series(form),
            "shipping_location": form.shippingLocation or None, "detailed_remarks": form.detailedRemarks or None}


def _missing_required(form: Form) -> list[str]:
    need = [("orderRcvdDate", "Order Received Date"), ("orderVia", "Order Received Thru"),
            ("dcNo", "DC/Inv No."), ("typeOfSubmission", "Type of Submission")]
    return [label for attr, label in need if not getattr(form, attr)]


@router.post("/orders")
def add_order(form: Form, user=Depends(access.require_page("o2d_shop"))):
    from . import main
    _need_edit(user, "o2d_shop")
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
    _need_edit(user, "o2d_shop")
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
    _need_edit(user, "o2d_godown")
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


def _dispatch_payload(form: Form, screen: str) -> tuple[dict, str | None]:
    """The order fields a dispatch save sets (shared by the single save and the bulk "Mark done"). Returns (fields, error message or None)."""
    p: dict = {}
    if form.deliveryStatus:
        p["delivery_status_key"] = _key_by_name(_lookup_rows("delivery-statuses"), form.deliveryStatus)
    if form.materialDeliveryDateTime:
        p["material_delivery_datetime"] = form.materialDeliveryDateTime
    if form.deliveredByWhom:
        err = _delivered_by(p, form, DISPATCH_SCOPES.get(screen))
        if err:
            return p, err
    if _has(form.cartage):
        p["cartage"] = float(form.cartage)
    if form.paymentStatus is not None:  # payment inputs are only sent when this role is allowed them
        p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    return p, None


def _first_dispatch(before: dict | None, updated: dict) -> bool:
    """True when this save is the one that filled in the delivery date/time and Delivered by for the first time."""
    if before is None:
        return False
    return (not before["materialDeliveryDateTime"] and not before["deliveredByWhom"]) and bool(updated["materialDeliveryDateTime"]) and bool(updated["deliveredByWhom"])


def _alert_if_first_dispatch(user, sl_no: int, before: dict | None, updated: dict, screen: str) -> None:
    """The first time delivery date/time and Delivered by are both filled, the WhatsApp groups are told (once): the VT Dispatch / Porter group
    from Godown Dispatch, and the Receiving group from either dispatch screen."""
    if screen in ("godown_dispatch", "shop_dispatch") and _first_dispatch(before, updated):
        _in_background(_tell_dispatch_groups, user, updated, screen)


# Each WhatsApp post may wait up to 10 s and a dispatch tells two groups, one after the other (a bulk save: two per order). The save
# answers first and the messages go out from here. Two workers keep the provider from being flooded; a burst simply queues.
_alert_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wa-alert")


def _in_background(fn, *args, **kwargs) -> None:
    """Run fn after the response (with the request's context, so the timeline still notes the IP). VT_ALERTS_INLINE=1 runs it right
    away - the tests check the messages as soon as the save returns."""
    if os.environ.get("VT_ALERTS_INLINE") == "1":
        fn(*args, **kwargs)
        return
    ctx = contextvars.copy_context()

    def run():
        try:
            ctx.run(fn, *args, **kwargs)
        except Exception:   # _tell_dispatch_groups already logs its own failures; this is the last guard
            log.exception("Background WhatsApp alert failed")
    _alert_pool.submit(run)


def _tell_dispatch_groups(user, updated: dict, screen: str, resend: bool = False) -> str:
    """Send the dispatch message to the groups of this screen and note on the order's timeline how it went. Returns the outcome."""
    outcome = "skipped"
    if screen == "godown_dispatch":
        outcome = "failed"
        try:
            outcome = alert_outcome(send_dispatch_alert(updated, resend=True) if resend else send_dispatch_alert(updated))
        except Exception:  # the order is saved; a failed alert must never undo or fail that
            log.exception("WhatsApp dispatch alert failed (order %s saved fine)", updated.get("slNo"))
        _tell_receiving(updated, resend=resend)
    else:               # Shop Dispatch & Receiving: the Receiving group is the only one told
        outcome = "failed"
        try:
            outcome = alert_outcome(send_receiving_alert(updated, resend=resend))
        except Exception:
            log.exception("WhatsApp receiving-group alert failed (order %s saved fine)", updated.get("slNo"))
    _record_alert(updated["orderKey"], outcome, user, resend=resend)
    return outcome


class BulkItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slNo: int
    form: Form


class BulkIn(BaseModel):
    """One entry per ticked order, each with its OWN form values (the bulk modal has one section per order)."""
    model_config = ConfigDict(extra="ignore")
    items: list[BulkItem] = Field(min_length=1, max_length=MAX_BULK)


class _BulkRefused(Exception):
    pass


def _bulk_save(user, items: list[tuple[int, dict]]) -> dict:
    """Save each order's own fields in ONE transaction: every order goes through main.apply_order_update (the single-order rules and its own
    order_event row), and if any order is refused nothing at all is saved. Returns {"done": [(sl, before, after)]} or {"error": message}
    naming the order that was refused."""
    from . import main
    patches = []
    for sl, payload in items:
        if any(k.endswith("_key") and v is None for k, v in payload.items()):
            return {"error": f"Sl {sl}: {UNKNOWN_VALUE} - nothing was saved."}
        try:
            patches.append((sl, main.OrderPatch(**payload)))
        except ValueError as e:
            return {"error": f"Sl {sl}: {e} - nothing was saved."}
    done: list[tuple] = []
    try:
        with db.cursor() as cur:
            for sl, patch in patches:
                try:
                    done.append((sl, *main.apply_order_update(cur, sl, patch, None, user, allow_closed=user["role"] == "admin")))
                except HTTPException as e:
                    raise _BulkRefused(f"Sl {sl}: {e.detail}")
    except _BulkRefused as e:
        return {"error": f"{e} - nothing was saved."}
    except Exception as e:   # database refusals (bad reference ...): the whole batch rolled back
        if e.__class__.__module__.startswith("psycopg2"):
            return {"error": "An order could not be saved (invalid value) - nothing was saved."}
        raise
    return {"done": done}


def _unique_items(body: BulkIn) -> list[BulkItem]:
    seen: dict[int, BulkItem] = {}
    for it in body.items:
        seen[it.slNo] = it            # an order listed twice: the last entry wins
    return list(seen.values())


@router.put("/bulk/dispatch")
def bulk_dispatch(body: BulkIn, user=Depends(ANY_VIEWER)):
    """Godown Dispatch / Shop Dispatch & Receiving: dispatch (and, on the shop screen, receiving) details for many orders, each with its own values."""
    _need_edit(user, "o2d_shop_dispatch", "o2d_godown_dispatch")
    work, screens, photos_follow = [], {}, {}
    for it in _unique_items(body):
        form = it.form
        screen = form.viewAs if form.viewAs in DISPATCH_SCOPES else user["role"]
        screens[it.slNo] = screen
        photos_follow[it.slNo] = bool(form.photosFollow)
        missing = [label for label, v in (("Material delivery date & time", form.materialDeliveryDateTime), ("Delivered by", form.deliveredByWhom)) if not v]
        if missing:
            return {"ok": True, "success": False, "message": f"Sl {it.slNo}: missing required field(s): " + ", ".join(missing) + " - nothing was saved."}
        p, err = _dispatch_payload(form, screen)
        if err:
            return {"ok": True, "success": False, "message": f"Sl {it.slNo}: {err} - nothing was saved."}
        work.append((it.slNo, p))
    out = _bulk_save(user, work)
    if "error" in out:
        return {"ok": True, "success": False, "message": out["error"]}
    pending = []
    for sl, before, after in out["done"]:
        b, a = map_order(before), map_order(after)
        if photos_follow[sl] and screens[sl] in ("godown_dispatch", "shop_dispatch") and _first_dispatch(b, a):
            pending.append(sl)                  # its photos go up next; the page asks for the message afterwards
        else:
            _alert_if_first_dispatch(user, sl, b, a, screens[sl])
    return {"ok": True, "success": True, "count": len(out["done"]), "alertPending": pending}


@router.put("/bulk/receiving")
def bulk_receiving(body: BulkIn, user=Depends(ANY_VIEWER)):
    """Godown Receiving: receiving details for many orders, each with its own values."""
    _need_edit(user, "o2d_receiving")
    work = []
    for it in _unique_items(body):
        form = it.form
        missing = [label for label, v in (("Date of receiving", form.dateOfReceiving), ("Payment status", form.paymentStatus)) if not v]
        if missing:
            return {"ok": True, "success": False, "message": f"Sl {it.slNo}: missing required field(s): " + ", ".join(missing) + " - nothing was saved."}
        work.append((it.slNo, _receiving_fields(form, _lookup_rows("payment-statuses"))))
    out = _bulk_save(user, work)
    if "error" in out:
        return {"ok": True, "success": False, "message": out["error"]}
    return {"ok": True, "success": True, "count": len(out["done"])}


@router.put("/orders/{sl_no}/dispatch")
def update_dispatch(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    _need_edit(user, "o2d_shop_dispatch", "o2d_godown_dispatch")
    before = None
    screen = form.viewAs if form.viewAs in DISPATCH_SCOPES else user["role"]   # the dispatch screen being worked
    if screen in ("godown_dispatch", "shop_dispatch"):
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
        err = _delivered_by(p, form, DISPATCH_SCOPES.get(screen))
        if err:
            return {"ok": True, "success": False, "message": err}
    if _has(form.cartage):
        p["cartage"] = float(form.cartage)
    if form.paymentStatus is not None:  # payment inputs are only sent when this role is allowed them
        p.update(_receiving_fields(form, _lookup_rows("payment-statuses")))
    result = _write(user, sl_no, p)
    if result["success"] and screen in ("godown_dispatch", "shop_dispatch") and _first_dispatch(before, result["order"]):
        if form.photosFollow:
            result["alertPending"] = True       # the photos go up next; the page then asks for the message so it can carry them
        else:
            _in_background(_tell_dispatch_groups, user, result["order"], screen)
    result.pop("order", None)
    return result


@router.post("/orders/{sl_no}/whatsapp-send")
def send_pending_dispatch_alert(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    """The dispatch message of a save whose photos were uploaded afterwards (the save answered alertPending): sent now, once, with the photos."""
    _need_edit(user, "o2d_shop_dispatch", "o2d_godown_dispatch")
    from . import main
    screen = form.viewAs if form.viewAs in DISPATCH_SCOPES else user["role"]
    try:
        order = map_order(main.get_order(sl_no, archived=None, user=user))
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    if not order["materialDeliveryDateTime"] or not order["deliveredByWhom"]:
        return {"ok": True, "success": False, "message": "Fill in the delivery date/time and Delivered by first."}
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM order_event WHERE order_key = %s AND event_type = 'whatsapp' AND title NOT LIKE 'Sent again%%' LIMIT 1", (order["orderKey"],))
        if cur.fetchone():
            return {"ok": True, "success": True, "message": "The WhatsApp message for this order was already sent."}
    # the page only waits for this to close the form, and ignores the outcome: answer now, send after (the timeline records how it went)
    _in_background(_tell_dispatch_groups, user, order, screen)
    return {"ok": True, "success": True, "outcome": "queued"}


@router.post("/orders/{sl_no}/whatsapp-resend")
def resend_dispatch_alert(sl_no: int, user=Depends(ANY_VIEWER)):
    """Send the dispatch WhatsApp alert for an order again (the first one goes automatically, once). Godown dispatch / admin."""
    _need_edit(user, "o2d_godown_dispatch")
    from . import main
    try:
        order = map_order(main.get_order(sl_no, archived=None, user=user))
    except HTTPException as e:
        return {"ok": True, "success": False, "message": str(e.detail)}
    if not order["materialDeliveryDateTime"] or not order["deliveredByWhom"]:
        return {"ok": True, "success": False, "message": "Fill in the delivery date/time and Delivered by first."}
    outcome = _tell_dispatch_groups(user, order, "godown_dispatch", resend=True)
    message = {"sent": "WhatsApp alert sent again.", "skipped": "WhatsApp is not set up on the server, so nothing was sent.",
               "off": "This alert is switched off in Setup > WhatsApp, so nothing was sent.",
               "failed": "WhatsApp did not accept the message. Nothing was sent - try again in a minute."}[outcome]
    return {"ok": True, "success": outcome == "sent", "message": message}


@router.put("/orders/{sl_no}/receiving")
def update_receiving(sl_no: int, form: Form, user=Depends(ANY_VIEWER)):
    _need_edit(user, "o2d_receiving")
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
    result = _write(user, sl_no, p, archived=True if archived else None, allow_closed=True)   # All orders > Edit (admin) is the one way to change a closed order
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
    if not _can_resolve(user):
        raise HTTPException(403, "Only admin or the shop can close a missing number.")
    reason = body.reason.strip()
    if body.doc_type not in ("Challan", "Invoice", "Sales Return Challan", "Sales Return Invoice"):
        raise HTTPException(422, "doc_type must be Challan, Invoice, Sales Return Challan or Sales Return Invoice")
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
def doc_check(type_name: str, date_: date = Query(alias="date"), number: str = Query(max_length=50),
              bill: str | None = Query(default=None, max_length=10),
              user=Depends(access.require_page("o2d_shop", "o2d_missing_entry"))):
    """Used by the order form while a number is typed: which bill series is this submission type (if any), is the number digits,
    already taken, or skipping ahead of the series? For a type that allows either series, `bill` is the shop's Challan / Invoice pick."""
    with db.cursor() as cur:
        series = doc_numbers.series_of(cur, type_name)
        if not series:
            return {"ok": True, "series": None, "duplicate": False, "skipped": []}
        if series in ("Both", "Goods Return"):   # the order is tagged to one series: the one the shop picked
            pick = (bill or "").strip().lower()
            if pick not in ("challan", "invoice"):
                return {"ok": True, "series": series, "needsChoice": True, "help": doc_numbers.CHOOSE_HELP, "duplicate": False, "skipped": []}
            series = doc_numbers.SERIES[pick] if series == "Both" else ("Sales Return Challan" if pick == "challan" else "Sales Return Invoice")
        if not number.strip().isdigit():
            return {"ok": True, "series": series, "notDigits": True, "duplicate": False, "skipped": []}
        return {"ok": True, "series": series, **doc_numbers.check_entry(cur, series, date_, int(number))}


# ------------------------------------------------------------------ WhatsApp alert
def _fmt_date(iso: str) -> str:
    parts = str(iso or "").split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else str(iso or "")


def delivery_kind(name: str | None) -> str:
    """'porter' / 'company' / 'transport' for the By Porter / By Company / By Transport values (they carry an extra text), else 'person'."""
    n = (name or "").lower()
    for kind in ("porter", "company", "transport"):
        if re.search(rf"(?<!\w){kind}(?!\w)", n):
            return kind
    return "person"


def _person_phone(name: str) -> str:
    try:
        with db.cursor() as cur:
            cur.execute("SELECT phone_number FROM dim_person WHERE lower(btrim(full_name)) = lower(btrim(%s)) AND person_role = 'delivery' "
                        "AND phone_number IS NOT NULL ORDER BY person_key LIMIT 1", (name,))
            row = cur.fetchone()
        return (row["phone_number"] or "").strip() if row else ""
    except Exception:
        return ""


def _money_text(v) -> str:
    if v in (None, ""):
        return "-"
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _dispatch_values(o: dict) -> dict:
    """What the dispatch message needs, as in the WhatsApp template document: Cartage is shown for a delivery person and for By Porter (not for
    By Company / By Transport), Delivered By carries a delivery person's phone, By Porter / Company / Transport carry their extra text, and the
    photo is a link to the dispatch photo(s)."""
    who = o["deliveredByWhom"] or "-"
    kind = delivery_kind(o["deliveredByWhom"])
    phone = _person_phone(o["deliveredByWhom"]) if kind == "person" and o["deliveredByWhom"] else ""
    detail = (o.get("deliveredByDetail") or "").strip()
    cartage = _money_text(o.get("cartage"))
    try:
        photos = attachments.photo_links(o["orderKey"], "dispatch") if o.get("orderKey") else []
    except Exception:
        log.exception("Could not make the photo links for order %s", o.get("slNo"))
        photos = []
    return {"order_date": _fmt_date(o["orderRcvdDate"]), "dc_no": o["dcNo"] or "-", "ready_by": o["readyByWhom"] or "-",
            "delivery_status": o["deliveryStatus"] or "-", "address": o["shippingLocation"] or "-", "remarks": o["detailedRemarks"] or "-",
            "delivered_by": (f"{who} {phone}" if phone else who), "cartage": cartage,
            "cartage_line": f"Cartage: {cartage}\n" if kind in ("person", "porter") else "",
            "details": detail or "-", "details_line": f"Details: {detail}\n" if detail else "",
            "photo": "\n".join(photos) if photos else "-"}


def _dispatch_alert_key(o: dict) -> str:
    """By Porter has its own group; everything else goes to the VT Dispatch group."""
    return "dispatch_porter" if delivery_kind(o.get("deliveredByWhom")) == "porter" else "dispatch_godown"


def build_dispatch_message(o: dict, resend: bool = False) -> str:
    """The dispatch alert text: the template an admin keeps under Setup > WhatsApp, filled in for this order."""
    key = _dispatch_alert_key(o)
    return ("(Sent again)\n" if resend else "") + whatsapp.render(whatsapp.get_alert(key)["template"], _dispatch_values(o))


ALERT_TITLES = {"sent": "WhatsApp dispatch alert sent to the group", "failed": "WhatsApp dispatch alert FAILED - not sent",
                "skipped": "WhatsApp dispatch alert not sent - WhatsApp is not set up",
                "off": "WhatsApp dispatch alert not sent - switched off in Setup"}


def _record_alert(order_key: int, outcome: str, user: dict, resend: bool = False):
    """Note on the order's timeline when the dispatch alert went out (or why it did not). Never blocks the save."""
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO order_event (order_key, event_type, title, by_user_key, by_name, by_role, by_ip) "
                        "VALUES (%s, 'whatsapp', %s, %s, %s, %s, %s)",
                        (order_key, ("Sent again - " if resend else "") + ALERT_TITLES[outcome], user["user_key"], user["display_name"], user["role"], timeline.client_ip()))
    except Exception:
        log.exception("Could not note the WhatsApp alert on order %s", order_key)


def send_dispatch_alert(o: dict, post=None, resend: bool = False) -> dict:
    """Post the dispatch alert to its WhatsApp group: the VT Dispatch group, or the Porter group for By Porter (settings: Setup > WhatsApp;
    login: WHATSAPP_API_KEY). Nothing is sent, and the reason is returned, when the alert is off or WhatsApp is not set up."""
    return whatsapp.send_alert(_dispatch_alert_key(o), _dispatch_values(o), prefix="(Sent again)\n" if resend else "", post=post)


def send_receiving_alert(o: dict, post=None, resend: bool = False) -> dict:
    """The same dispatch message to the Receiving group (the people on the Shop Dispatch & Receiving / Godown Receiving dashboards)."""
    return whatsapp.send_alert("dispatch_receiving", _dispatch_values(o), prefix="(Sent again)\n" if resend else "", post=post)


def _tell_receiving(o: dict, resend: bool = False) -> None:
    try:
        out = send_receiving_alert(o, resend=resend)
        if not out.get("skipped") and not whatsapp.reply_ok(out.get("code"), out.get("body", "")):
            log.warning("The Receiving group did not accept the dispatch message for order %s: %s", o.get("slNo"), whatsapp._snippet(out.get("body")))
    except Exception:   # the order is saved and the main alert is handled separately: never let this one fail anything
        log.exception("WhatsApp receiving-group alert failed (order %s)", o.get("slNo"))


def alert_outcome(result: dict | None) -> str:
    r = result or {}
    if r.get("skipped"):
        return "off" if "switched off" in r.get("reason", "") else "skipped"
    return "sent"
