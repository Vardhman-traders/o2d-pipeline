"""Order timeline: one event per change to an order (when, who, what changed), plus the reader that builds the
timeline shown in the order-details popup. Orders that predate event tracking get a reconstructed timeline from
the timestamps on the order itself, clearly marked as reconstructed."""
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

IST = ZoneInfo("Asia/Kolkata")

# order row (main.ORDER_SELECT names) -> label shown to people
FIELD_LABELS = {
    "order_received_date": "Order date", "dc_inv_no": "DC / Inv no", "order_via": "Order via",
    "submission_type": "Submission type", "shipping_location": "Address", "detailed_remarks": "Remarks",
    "ready_by": "Ready by", "colour_making_by": "Colour making by", "delivery_status": "Delivery status",
    "material_delivery_datetime": "Material delivered at", "delivered_by": "Delivered by", "cartage": "Cartage (Rs)",
    "date_of_receiving": "Date received", "payment_status": "Payment status", "amount_received": "Amount received (Rs)",
}
GODOWN = {"ready_by", "colour_making_by", "delivery_status"}
DISPATCH = {"material_delivery_datetime", "delivered_by", "cartage"}
RECEIVING = {"date_of_receiving", "payment_status", "amount_received"}


def _show(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return (v.astimezone(IST) if v.tzinfo else v).strftime("%d-%m-%Y %H:%M")
    return str(v)


def diff(old: dict, new: dict) -> list[dict]:
    out = []
    for key, label in FIELD_LABELS.items():
        a, b = _show(old.get(key)), _show(new.get(key))
        if a != b:
            out.append({"field": label, "from": a, "to": b, "_key": key})
    return out


def _classify(changes: list[dict], new: dict) -> tuple[str, str]:
    keys = {c["_key"] for c in changes}
    if new.get("is_cancelled") and "delivery_status" in keys or any(
            c["_key"] == "submission_type" and str(c["to"] or "").lower() == "cancelled" for c in changes):
        return "cancelled", "Order cancelled"
    parts = []
    if keys & GODOWN:
        parts.append("Godown update")
    if keys & DISPATCH:
        parts.append("Dispatch / delivery update")
    if keys & RECEIVING:
        parts.append("Receiving & payment update")
    if not parts:
        return "edited", "Order details edited"
    return ("godown" if keys & GODOWN else "dispatch" if keys & DISPATCH else "receiving"), " + ".join(parts)


def record_created(cur, order_key: int, user: dict):
    cur.execute("INSERT INTO order_event (order_key, event_type, title, by_user_key, by_name, by_role) "
                "VALUES (%s, 'created', 'Order logged', %s, %s, %s)",
                (order_key, user["user_key"], user["display_name"], user["role"]))


def record_update(cur, order_key: int, user: dict, old: dict, new: dict):
    """Called with the order before and after an update. Saves nothing when nothing really changed."""
    changes = diff(old, new)
    if not changes:
        return
    kind, title = _classify(changes, new)
    clean = [{k: v for k, v in c.items() if k != "_key"} for c in changes]
    cur.execute("INSERT INTO order_event (order_key, event_type, title, by_user_key, by_name, by_role, changes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (order_key, kind, title, user["user_key"], user["display_name"], user["role"], Json(clean)))


def _iso(dt):
    return dt.astimezone(IST).isoformat(timespec="seconds") if dt else None


def timeline_for(cur, order_key: int, row: dict) -> dict:
    """Events oldest first. `row` is the order's v_orders_archive row."""
    cur.execute("SELECT at, event_type, title, by_name, by_role, changes FROM order_event "
                "WHERE order_key = %s ORDER BY at, event_key", (order_key,))
    events = [{"at": _iso(e["at"]), "type": e["event_type"], "title": e["title"], "by": e["by_name"],
               "role": e["by_role"], "changes": e["changes"] or [], "reconstructed": False} for e in cur.fetchall()]
    note = None
    if not any(e["type"] == "created" for e in events):
        events.insert(0, {"at": _iso(row["timestamp_created"]), "type": "created", "title": "Order logged",
                          "by": row["created_by"], "role": None, "changes": [], "reconstructed": True})
    if not any(e["type"] not in ("created", "whatsapp") for e in events):
        # nothing recorded yet beyond the start: rebuild the milestones from the order record
        if row.get("delivery_status") or row.get("ready_by") or row.get("colour_making_by"):
            events.append({"at": None, "type": "godown", "title": "Godown update", "by": None, "role": None,
                           "reconstructed": True, "changes": [
                               {"field": lbl, "from": None, "to": row[k]} for k, lbl in (
                                   ("ready_by", "Ready by"), ("colour_making_by", "Colour making by"),
                                   ("delivery_status", "Delivery status")) if row.get(k)]})
        if row.get("material_delivery_datetime"):
            events.append({"at": _iso(row["material_delivery_datetime"]), "type": "dispatch",
                           "title": "Material delivered", "by": row.get("delivered_by"),
                           "role": None, "reconstructed": True, "changes": []})
        if row.get("date_of_receiving") or row.get("payment_status"):
            events.append({"at": None, "type": "receiving", "title": "Receiving & payment update", "by": None,
                           "role": None, "reconstructed": True, "changes": [
                               {"field": lbl, "from": None, "to": str(row[k])} for k, lbl in (
                                   ("date_of_receiving", "Date received"), ("payment_status", "Payment status"),
                                   ("amount_received", "Amount received (Rs)")) if row.get(k) is not None]})
        if row.get("last_updated_at") and row.get("last_updated_by") and len(events) > 1:
            events.append({"at": _iso(row["last_updated_at"]), "type": "edited", "title": "Last update on record",
                           "by": row["last_updated_by"], "role": None, "changes": [], "reconstructed": True})
    if any(e["reconstructed"] for e in events):
        note = ("Part of this timeline is rebuilt from the order record: this order was changed before step-by-step "
                "tracking began, so some exact times or names were not captured.")
    return {"events": events, "note": note}
