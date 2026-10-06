"""Cartage screen of the O2D app: the money paid to carry each delivery, who carried it, and how much each person
has been paid over a period.

Everything is driven by settings, not code:
  - who may OPEN the screen and who may CHANGE cartage is the page access "o2d_cartage" (Setup > Access: tick it for a
    role, or give one person "Always allow" to manage it or "View only" to just look) - nothing is hard-wired to a role;
  - the delivery people come from the Delivered-by list under Setup > Dropdown values;
  - the table shows the same columns, in the same order, as the full order record.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import access, db, timeline

router = APIRouter(prefix="/o2d/cartage", tags=["o2d-cartage"])
IST = ZoneInfo("Asia/Kolkata")
PAGE = "o2d_cartage"
MAX_ROWS = 2000
DEFAULT_DAYS = 30
NOT_RECORDED = "Not recorded"


def window(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    from .o2d_screens import today_ist
    to = date_to or today_ist()
    return (date_from or to - timedelta(days=DEFAULT_DAYS - 1), to)


def cartage_date(r: dict) -> date | None:
    """The day a cartage payment belongs to: the day the goods were received, else the day they were delivered."""
    if r.get("date_of_receiving"):
        return r["date_of_receiving"]
    d = r.get("material_delivery_datetime")
    return d.astimezone(IST).date() if d else None


def fetch_cartage(date_from: date | None, date_to: date | None, delivered_by: str | None = None) -> list[dict]:
    """The deliveries cartage is accounted on - the same rule the Archive portal's Cartage report used:
    not cancelled, receiving/payment already done, optionally one delivery person, and (when dates are given) the
    receiving date (else the delivery date) inside the period. Archived orders are included on purpose: cartage is
    paid over weeks and months, long after an order is archived. Oldest first, each row carries `cartage_on`."""
    from . import main
    where = ["NOT o.is_cancelled", "lower(btrim(COALESCE(st.type_name, ''))) <> 'cancelled'",
             "lower(btrim(COALESCE(ds.status_name, ''))) <> 'cancelled'", "o.payment_status_key IS NOT NULL"]
    params: list = []
    if delivered_by:
        where.append("dp.full_name = %s")
        params.append(delivered_by)
    with db.cursor() as cur:
        cur.execute(main.ORDER_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY o.sl_no LIMIT %s", params + [MAX_ROWS * 5])
        rows = cur.fetchall()
    out = []
    for r in rows:
        r["cartage_on"] = cartage_date(r)
        if (date_from or date_to) and r["cartage_on"] is None:
            continue
        if (date_from and r["cartage_on"] < date_from) or (date_to and r["cartage_on"] > date_to):
            continue
        r["delivered_by_full"] = " - ".join(x for x in (r.get("delivered_by"), r.get("delivered_by_detail")) if x)
        out.append(r)
    return out[:MAX_ROWS]


def by_employee(rows: list[dict]) -> list[dict]:
    """One line per delivery person: how many deliveries, how many carried cartage, and the money."""
    acc: dict[str, dict] = {}
    for r in rows:
        name = r.get("delivered_by") or NOT_RECORDED
        a = acc.setdefault(name, {"employee": name, "deliveries": 0, "with_cartage": 0, "total": Decimal(0)})
        a["deliveries"] += 1
        if r.get("cartage") is not None and r["cartage"] > 0:
            a["with_cartage"] += 1
            a["total"] += r["cartage"]
    out = sorted(acc.values(), key=lambda a: (-a["total"], a["employee"].lower()))
    for a in out:
        a["average"] = (a["total"] / a["with_cartage"]) if a["with_cartage"] else Decimal(0)
    return out


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _num(v):
    return None if v is None else float(v)


def _row(r: dict) -> dict:
    """The full order record, in reading order (plus the keys the screen needs)."""
    ist = lambda d: d.astimezone(IST).strftime("%Y-%m-%dT%H:%M") if d else ""  # noqa: E731
    return {"slNo": r["sl_no"], "timestamp": ist(r["timestamp_created"]), "orderDate": _iso(r["order_received_date"]) or "",
            "cartageOn": _iso(r.get("cartage_on")) or "", "orderVia": r["order_via"] or "", "shippingLocation": r["shipping_location"] or "", "dcNo": r["dc_inv_no"] or "",
            "submissionType": r["submission_type"] or "", "remarks": r["detailed_remarks"] or "",
            "readyBy": r["ready_by"] or "", "colourMakingBy": r["colour_making_by"] or "",
            "deliveryStatus": r["delivery_status"] or "", "deliveredOn": ist(r["material_delivery_datetime"]),
            "dateOfReceiving": _iso(r["date_of_receiving"]) or "", "paymentStatus": r["payment_status"] or "",
            "amountReceived": _num(r["amount_received"]), "deliveredBy": r["delivered_by_full"],
            "deliveredByName": r["delivered_by"] or "", "cartage": _num(r["cartage"]),
            "lastUpdatedBy": r["last_updated_by"] or "", "lastUpdatedAt": ist(r["last_updated_at"]), "createdBy": r["created_by"] or ""}


@router.get("")
def cartage_screen(date_from: date | None = Query(default=None), date_to: date | None = Query(default=None),
                   delivered_by: str | None = Query(default=None, max_length=150),
                   user=Depends(access.require_page(PAGE))):
    df, dt = window(date_from, date_to)
    if df > dt:
        raise HTTPException(422, "From date is after the To date.")
    rows = fetch_cartage(df, dt, delivered_by or None)
    emp = by_employee(rows)
    paid = [r for r in rows if r.get("cartage")]
    with db.cursor() as cur:
        cur.execute("SELECT full_name FROM dim_person WHERE person_role = 'delivery' ORDER BY lower(full_name)")
        people = [p["full_name"] for p in cur.fetchall()]
    return {"ok": True, "dateFrom": df.isoformat(), "dateTo": dt.isoformat(), "orders": [_row(r) for r in rows],
            "truncated": len(rows) >= MAX_ROWS, "people": people,
            "canEdit": not access.is_view_only(user, PAGE),
            "summary": {"deliveries": len(rows), "withCartage": len(paid), "withoutCartage": len(rows) - len(paid),
                        "total": float(sum((r["cartage"] for r in paid), Decimal(0)))},
            "byEmployee": [{"employee": a["employee"], "deliveries": a["deliveries"], "withCartage": a["with_cartage"],
                            "total": float(a["total"]), "average": float(a["average"])} for a in emp]}


class CartageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cartage: float | None = None
    deliveredByWhom: str | None = None


@router.put("/{sl_no}")
def update_cartage(sl_no: int, body: CartageIn, user=Depends(access.require_page(PAGE))):
    """Set the cartage (and, if it was entered wrongly, who carried it) for one delivered order."""
    from . import main
    access.require_edit(user, PAGE)
    if body.cartage is not None and not 0 <= body.cartage < 10 ** 8:
        return {"ok": True, "success": False, "message": "Cartage must be a positive amount in rupees."}
    with db.cursor() as cur:
        cur.execute(main.ORDER_SELECT + " WHERE o.sl_no = %s AND o.archived_at IS NULL FOR UPDATE OF o", (sl_no,))
        before = cur.fetchone()
        if not before:
            raise HTTPException(404, "Order not found")
        sets, params = ["cartage = %s"], [None if body.cartage is None else round(body.cartage, 2)]
        if body.deliveredByWhom:
            cur.execute("SELECT person_key FROM dim_person WHERE person_role = 'delivery' AND lower(btrim(full_name)) = lower(btrim(%s))",
                        (body.deliveredByWhom,))
            person = cur.fetchone()
            if not person:
                return {"ok": True, "success": False, "message": "Unknown dropdown value selected. Please refresh and try again."}
            sets.append("delivered_by_person_key = %s")
            params.append(person["person_key"])
        cur.execute(f"UPDATE fact_orders SET {', '.join(sets)}, last_updated_by_user_key = %s, last_updated_at = now() "
                    "WHERE order_key = %s", params + [user["user_key"], before["order_key"]])
        cur.execute(main.ORDER_SELECT + " WHERE o.order_key = %s", (before["order_key"],))
        timeline.record_update(cur, before["order_key"], user, before, cur.fetchone())
    return {"ok": True, "success": True}
