"""What can be bulk-uploaded, and the rules for each: orders and users.

Each entity provides: the template columns, EXAMPLE rows, `validate(rows)` -> report, and `insert(cur, rows,
batch_id, admin)`. Validation never trusts the client: confirm re-validates every row on the server before
anything is written, and everything for one import goes in a single transaction under one batch id.
"""
import re
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from . import auth, db, roles

IST = ZoneInfo("Asia/Kolkata")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,39}$")
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y")
DATETIME_FORMATS = ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M")
MAX_MONEY = Decimal("99999999.99")


def parse_date(s: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(s: str) -> datetime | None:
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_money(s: str) -> Decimal | None:
    cleaned = re.sub(r"(?i)rs\.?|inr|₹|,|\s", "", s)
    try:
        v = Decimal(cleaned)
    except InvalidOperation:
        return None
    return v if v.is_finite() else None


def _report(rows_in, results):
    """results: list of (row, error|None, normalised|None) in file order."""
    errors = [{"row": r.get("_row"), "error": e, "data": r} for r, e, _ in results if e]
    ok = [(r, n) for r, e, n in results if not e]
    return {"total": len(results), "valid": len(ok), "errors": errors, "rows": [r for r, _ in ok],
            "_normalised": [n for _, n in ok]}


# ============================================================ ORDERS
ORDER_COLUMNS = ["Order Date", "Order Via", "Type of Submission", "DC/Inv No", "Address", "Remarks", "Ready By",
                 "Colour Making By", "Delivery Status", "Delivered By", "Delivery Date & Time", "Cartage",
                 "Date of Receiving", "Payment Status", "Amount Received"]
ORDER_REQUIRED = ["Order Date", "Order Via", "Type of Submission", "DC/Inv No"]
ORDER_EXAMPLES = [
    ["EXAMPLE 15-09-2026", "Call", "Challan", "EXAMPLE-101", "Rohini", "Deliver before noon",
     "", "", "", "", "", "", "", "", ""],
    ["EXAMPLE 16-09-2026", "Walk-in", "Invoice", "EXAMPLE-5001", "Pitampura", "", "Ravi", "Sonu", "Delivered", "Amit",
     "16-09-2026 14:30", "50", "17-09-2026", "Paid", "1500"],
]
ORDER_HELP = ("Fill one order per row. Order Date, Order Via, Type of Submission and DC/Inv No are required. Dates: "
              "dd-mm-yyyy. Date & time: dd-mm-yyyy hh:mm. Order Via, Type, Delivery Status, Payment Status and people "
              "must already exist under Setup (the check tells you the valid values). Delete the two EXAMPLE rows or "
              "leave them - rows marked EXAMPLE are ignored.")


def _lookup_maps():
    """{'channels': {lower name: key}, ...} plus the display names for helpful error messages."""
    out = {}
    with db.cursor() as cur:
        for kind, table, key, name in (("channel", "dim_order_channel", "channel_key", "channel_name"),
                                       ("type", "dim_submission_type", "submission_type_key", "type_name"),
                                       ("delivery", "dim_delivery_status", "status_key", "status_name"),
                                       ("payment", "dim_payment_status", "payment_status_key", "status_name")):
            cur.execute(f"SELECT {key} AS k, {name} AS n FROM {table} ORDER BY lower({name})")
            out[kind] = {r["n"].strip().lower(): (r["k"], r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT person_key AS k, full_name AS n, person_role AS r FROM dim_person "
                    "ORDER BY lower(full_name)")
        for r in cur.fetchall():
            out.setdefault("person_" + r["r"], {})[r["n"].strip().lower()] = (r["k"], r["n"])
    return out


def _pick(maps, kind, value, label):
    """(key, error). Empty value -> (None, None)."""
    if not value:
        return None, None
    hit = maps.get(kind, {}).get(value.strip().lower())
    if hit:
        return hit[0], None
    valid = ", ".join(n for _, n in maps.get(kind, {}).values()) or "(none set up yet)"
    return None, f"{label} '{value}' not found. Valid values: {valid}"


def validate_orders(rows: list[dict]) -> dict:
    maps = _lookup_maps()
    today = datetime.now(IST).date()
    parsed_dates = {}
    for r in rows:
        d = parse_date(r.get("Order Date", ""))
        if d:
            parsed_dates[d] = int(d.strftime("%Y%m%d"))
    with db.cursor() as cur:
        calendar = set()
        if parsed_dates:
            cur.execute("SELECT date_key FROM dim_date WHERE date_key = ANY(%s)", (list(parsed_dates.values()),))
            calendar = {x["date_key"] for x in cur.fetchall()}
        dcs = [r.get("DC/Inv No", "").strip().lower() for r in rows if r.get("DC/Inv No", "").strip()]
        existing = {}
        if dcs:
            cur.execute("SELECT lower(o.dc_inv_no) AS dc, d.full_date AS dt, o.sl_no FROM fact_orders o "
                        "JOIN dim_date d ON d.date_key = o.order_received_date_key WHERE lower(o.dc_inv_no) = ANY(%s)",
                        (dcs,))
            existing = {(x["dc"], x["dt"]): x["sl_no"] for x in cur.fetchall()}

    seen: dict = {}
    results = []
    for r in rows:
        errs: list[str] = []
        def g(c, r=r):
            return (r.get(c) or "").strip()
        for c in ORDER_REQUIRED:
            if not g(c):
                errs.append(f"{c} is required")
        n: dict = {}
        d = parse_date(g("Order Date")) if g("Order Date") else None
        if g("Order Date") and not d:
            errs.append(f"Order Date '{g('Order Date')}' is not a date (use dd-mm-yyyy)")
        if d:
            if d > today + timedelta(days=1):
                errs.append("Order Date is in the future")
            elif int(d.strftime("%Y%m%d")) not in calendar:
                errs.append(f"Order Date {d:%d-%m-%Y} is outside the calendar table - "
                            "ask for the calendar to be extended")
            n["order_date"] = d
        for col, kind, key in (
                ("Order Via", "channel", "order_via_key"), ("Type of Submission", "type", "submission_type_key"),
                ("Delivery Status", "delivery", "delivery_status_key"),
                ("Payment Status", "payment", "payment_status_key"),
                ("Ready By", "person_ready_by", "ready_by_person_key"),
                ("Colour Making By", "person_colour_making", "colour_making_person_key"),
                ("Delivered By", "person_delivery", "delivered_by_person_key")):
            k, e = _pick(maps, kind, g(col), col)
            if e:
                errs.append(e)
            elif k is not None:
                n[key] = k
        if g("Delivery Status").lower() == "cancelled":
            n["is_cancelled"] = True
        dc = g("DC/Inv No")
        if dc:
            if len(dc) > 50:
                errs.append("DC/Inv No is longer than 50 characters")
            n["dc_inv_no"] = dc
            if d:
                dup = (dc.lower(), d)
                if dup in existing:
                    errs.append(f"DC/Inv No {dc} on {d:%d-%m-%Y} already exists (Sl No {existing[dup]})")
                elif dup in seen:
                    errs.append(f"Duplicate of row {seen[dup]} in this file (same DC/Inv No and date)")
                else:
                    seen[dup] = r.get("_row")
        n["shipping_location"] = g("Address") or None
        n["detailed_remarks"] = g("Remarks") or None
        if g("Delivery Date & Time"):
            dt = parse_datetime(g("Delivery Date & Time"))
            if not dt:
                errs.append(f"Delivery Date & Time '{g('Delivery Date & Time')}' is not valid (use dd-mm-yyyy hh:mm)")
            elif d and dt.date() < d:
                errs.append("Delivery Date & Time is before the Order Date")
            else:
                n["material_delivery_datetime"] = dt
        if g("Date of Receiving"):
            rd = parse_date(g("Date of Receiving"))
            if not rd:
                errs.append(f"Date of Receiving '{g('Date of Receiving')}' is not a date (use dd-mm-yyyy)")
            elif d and rd < d:
                errs.append("Date of Receiving is before the Order Date")
            elif not g("Delivery Date & Time"):
                errs.append("Date of Receiving needs a Delivery Date & Time "
                            "(it cannot be received before it is delivered)")
            else:
                n["date_of_receiving"] = rd
        for col, key in (("Cartage", "cartage"), ("Amount Received", "amount_received")):
            if g(col):
                m = parse_money(g(col))
                if m is None or m < 0 or m > MAX_MONEY:
                    errs.append(f"{col} '{g(col)}' must be a number between 0 and 99,999,999.99")
                else:
                    n[key] = m.quantize(Decimal("0.01"))
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_orders(cur, normalised: list[dict], batch_id: int, admin: dict) -> int:
    """One order per row, sl_no assigned in file order under the same lock create_order uses."""
    cur.execute("SELECT pg_advisory_xact_lock(7001)")
    cur.execute("SELECT COALESCE(MAX(sl_no), 0) AS n FROM fact_orders")
    sl = cur.fetchone()["n"]
    cur.execute("SELECT now() AS t")   # transaction time = import_batch.created_at, so later edits are detectable
    now = cur.fetchone()["t"]
    today = now.astimezone(IST).date()
    for n in normalised:
        sl += 1
        d = n["order_date"]
        # Logged "at" 09:30 IST on its own day for past orders (so time-to-deliver stays meaningful), now for today's.
        created = datetime.combine(d, time(9, 30), tzinfo=IST) if d < today else now
        cols = {"sl_no": sl, "order_received_date_key": int(d.strftime("%Y%m%d")), "timestamp_created": created,
                "last_updated_at": now, "created_by_user_key": admin["user_key"],
                "last_updated_by_user_key": admin["user_key"], "import_batch_id": batch_id}
        for k in ("dc_inv_no", "order_via_key", "submission_type_key", "delivery_status_key", "ready_by_person_key",
                  "colour_making_person_key", "delivered_by_person_key", "payment_status_key", "shipping_location",
                  "detailed_remarks", "material_delivery_datetime", "cartage", "amount_received", "is_cancelled"):
            if k in n:
                cols[k] = n[k]
        if "date_of_receiving" in n:
            cols["date_of_receiving_key"] = int(n["date_of_receiving"].strftime("%Y%m%d"))
        names = list(cols)
        cur.execute(f"INSERT INTO fact_orders ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})",
                    [cols[c] for c in names])
    return len(normalised)


# ============================================================ USERS
USER_COLUMNS = ["Display Name", "Username", "Role", "Temporary Password"]
USER_REQUIRED = ["Display Name", "Username", "Role"]
USER_EXAMPLES = [["EXAMPLE Ravi Kumar", "example.ravi", "godown", ""],
                 ["EXAMPLE Priya Sharma", "example.priya", "shop", "leave-blank-to-auto-generate"]]
USER_HELP = ("One member per row. Display Name, Username and Role are required. Username: lowercase letters, "
             "digits, dot, dash or underscore (3-40 characters). Leave Temporary Password blank to have one "
             "generated - you will see them once, after the import. Everyone must change their password at first "
             "sign-in. Admin accounts cannot be bulk-created; add them one at a time under Members.")
BULK_ROLES = sorted(roles.ALL_ROLES - {"legacy", "admin"})


def validate_users(rows: list[dict]) -> dict:
    names = [r.get("Username", "").strip().lower() for r in rows if r.get("Username", "").strip()]
    with db.cursor() as cur:
        taken = set()
        if names:
            cur.execute("SELECT lower(username) AS u FROM dim_user WHERE lower(username) = ANY(%s)", (names,))
            taken = {x["u"] for x in cur.fetchall()}
    seen: dict = {}
    results = []
    for r in rows:
        errs: list[str] = []
        display, username = (r.get("Display Name") or "").strip(), (r.get("Username") or "").strip().lower()
        role, pw = (r.get("Role") or "").strip().lower(), (r.get("Temporary Password") or "").strip()
        for c in USER_REQUIRED:
            if not (r.get(c) or "").strip():
                errs.append(f"{c} is required")
        if display and len(display) > 150:
            errs.append("Display Name is longer than 150 characters")
        if username:
            if not USERNAME_RE.match(username):
                errs.append("Username must be 3-40 characters: lowercase letters, digits, dot, dash, underscore")
            elif username in taken:
                errs.append(f"Username '{username}' already exists")
            elif username in seen:
                errs.append(f"Duplicate of row {seen[username]} in this file")
            else:
                seen[username] = r.get("_row")
        if role:
            if role == "admin":
                errs.append("Admin accounts cannot be bulk-created - add them one at a time under Members")
            elif role not in BULK_ROLES:
                errs.append(f"Role '{role}' not recognised. Valid roles: {', '.join(BULK_ROLES)}")
        if pw and len(pw) < 10:
            errs.append("Temporary Password must be at least 10 characters (or leave it blank)")
        clean = {"display_name": display, "username": username, "role": role, "password": pw}
        results.append((r, "; ".join(errs) or None, None if errs else clean))
    return _report(rows, results)


def insert_users(cur, normalised: list[dict], batch_id: int, admin: dict) -> list[dict]:
    """Creates the accounts (must change password at first sign-in). Returns the credentials, shown once."""
    creds = []
    for n in normalised:
        pw = n["password"] or secrets.token_urlsafe(9)
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password, "
                    "import_batch_id) VALUES (%s, %s, %s, %s, TRUE, %s)",
                    (n["username"], auth.hash_password(pw), n["role"], n["display_name"], batch_id))
        creds.append({"username": n["username"], "display_name": n["display_name"], "role": n["role"],
                      "temporary_password": pw})
    return creds


ENTITIES = {
    "orders": {"label": "orders", "columns": ORDER_COLUMNS, "required": ORDER_REQUIRED, "examples": ORDER_EXAMPLES,
               "help": ORDER_HELP, "validate": validate_orders},
    "users": {"label": "members", "columns": USER_COLUMNS, "required": USER_REQUIRED, "examples": USER_EXAMPLES,
              "help": USER_HELP, "validate": validate_users},
}
