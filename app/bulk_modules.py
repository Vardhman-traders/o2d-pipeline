# ruff: noqa: E501
"""Bulk upload for the Payments, Delegation (tasks) and Purchase modules. Same contract as bulk_entities.py: every entity
gives the template columns, EXAMPLE rows, `validate(rows)` and `insert(cur, normalised, batch_id, admin)`; the shared
flow (template, validate, fix, confirm, undo) lives in admin_tools.py. Imports are admin-only (Setup > Import).
"""
from datetime import datetime
from decimal import Decimal

from . import auth, db
from . import modcommon as mc
from .bulk_entities import IST, MAX_MONEY, _report, parse_date, parse_money


# ------------------------------------------------------------------ helpers
def _names(table: str, key: str, name: str, where: str = "") -> dict:
    """{lower name: (key, name)} of one master list."""
    with db.cursor() as cur:
        cur.execute(f"SELECT {key} AS k, {name} AS n FROM {table} {where} ORDER BY lower({name})")
        return {r["n"].strip().lower(): (r["k"], r["n"]) for r in cur.fetchall()}


def _list_names(table: str, key: str, name: str, where: str = "") -> list[str]:
    return [n for _, n in _names(table, key, name, where).values()]


def _calendar(dates) -> set:
    keys = sorted({int(d.strftime("%Y%m%d")) for d in dates if d})
    if not keys:
        return set()
    with db.cursor() as cur:
        cur.execute("SELECT date_key FROM dim_date WHERE date_key = ANY(%s)", (keys,))
        return {x["date_key"] for x in cur.fetchall()}


def _date_field(g, col: str, seen: list, errs: list):
    """Read one optional date column. Returns the date (also remembered in `seen` for the calendar check) or None."""
    raw = g(col)
    if not raw:
        return None
    d = parse_date(raw)
    if not d:
        errs.append(f"{col} '{raw}' is not a date (use dd-mm-yyyy)")
        return None
    seen.append((col, d))
    return d


def _calendar_errors(seen: list, cal: set, errs: list):
    for col, d in seen:
        if int(d.strftime("%Y%m%d")) not in cal:
            errs.append(f"{col} {d:%d-%m-%Y} is outside the calendar table - ask for the calendar to be extended")


def _pick(names: dict, value: str, label: str):
    """(key, error)."""
    hit = names.get(value.strip().lower())
    if hit:
        return hit[0], None
    return None, f"{label} '{value}' not found. Valid values: " + (", ".join(n for _, n in names.values()) or "(none set up yet)")


def _money(g, col: str, errs: list, positive: bool = False):
    raw = g(col)
    if not raw:
        return None
    m = parse_money(raw)
    if m is None or m > MAX_MONEY or m < 0 or (positive and m == 0):
        errs.append(f"{col} '{raw}' must be a number " + ("above 0" if positive else "from 0") + " up to 99,999,999.99")
        return None
    return m.quantize(Decimal("0.01"))


def _row_getter(r: dict):
    return lambda c: (r.get(c) or "").strip()


# ------------------------------------------------------------------ payments
PAYMENT_COLUMNS = ["Date", "Type", "Company", "Party", "Mode", "Amount", "Account Name", "Invoice No", "Remarks"]
PAYMENT_REQUIRED = ["Date", "Type", "Company", "Amount"]
PAYMENT_EXAMPLES = [
    ["EXAMPLE 15-09-2026", "Payment Received", "VT", "Sharma Traders", "Cash", "25000", "", "INV-101", "Part payment"],
    ["EXAMPLE 16-09-2026", "Expense", "VT", "", "UPI", "1200", "", "", "Tea and snacks"]]
PAYMENT_HELP = ("One payment entry per row. Date, Type, Company and Amount are required. Date: dd-mm-yyyy. Type, Company and "
                "Mode must already exist under Setup > Dropdown values (the check lists the valid values); Mode defaults to Cash. "
                "A Party that is new is added to the party list for that company and type, waiting for an admin to review it. "
                "Imported entries are saved as Approved, with the importing admin as the creator. Rows marked EXAMPLE are ignored.")


def payment_lists() -> dict:
    return {"Type": _list_names("dim_txn_type", "txn_type_key", "type_name"),
            "Company": _list_names("dim_company", "company_key", "company_name"),
            "Mode": _list_names("dim_payment_mode", "mode_key", "mode_name")}


def validate_payments(rows: list[dict]) -> dict:
    types = _names("dim_txn_type", "txn_type_key", "type_name")
    companies = _names("dim_company", "company_key", "company_name")
    modes = _names("dim_payment_mode", "mode_key", "mode_name")
    cal = _calendar(parse_date((r.get("Date") or "").strip()) for r in rows)
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in PAYMENT_REQUIRED if not g(c)]
        n: dict = {}
        seen: list = []
        n["date"] = _date_field(g, "Date", seen, errs)
        _calendar_errors(seen, cal, errs)
        for col, names, key in (("Type", types, "type_key"), ("Company", companies, "company_key"),
                                ("Mode", modes, "mode_key")):
            value = g(col) or ("Cash" if col == "Mode" else "")
            if value:
                k, e = _pick(names, value, col)
                if e:
                    errs.append(e)
                else:
                    n[key] = k
        n["amount"] = _money(g, "Amount", errs, positive=True)
        for col, limit in (("Party", 150), ("Account Name", 150), ("Invoice No", 80)):
            if len(g(col)) > limit:
                errs.append(f"{col} is longer than {limit} characters")
        n.update(party=" ".join(g("Party").split()), account=g("Account Name") or None,
                 invoice=g("Invoice No") or None, remarks=g("Remarks") or None)
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_payments(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    from . import payments
    today = mc.today_ist()
    for n in normalised:
        party = payments._party_key(cur, n["party"], n["company_key"], n["type_key"], admin) if n["party"] else None
        cur.execute(
            "INSERT INTO fact_payment_txn (txn_date_key, txn_type_key, company_key, party_key, mode_key, amount, account_name, "
            "invoice_no, remarks, status, is_backdated, created_by_user_key, approved_by_user_key, approved_at, import_batch_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Approved', %s, %s, %s, now(), %s)",
            (mc.date_key(n["date"]), n["type_key"], n["company_key"], party, n["mode_key"], n["amount"], n["account"],
             n["invoice"], n["remarks"], n["date"] < today, admin["user_key"], admin["user_key"], batch_id))


# ------------------------------------------------------------------ delegation tasks
TASK_COLUMNS = ["Staff Username", "Task", "Assigned Date", "Deadline"]
TASK_REQUIRED = ["Staff Username", "Task", "Deadline"]
TASK_EXAMPLES = [["EXAMPLE example.ravi", "Count the white cement stock and report", "", "30-09-2026"],
                 ["EXAMPLE example.priya", "Follow up the pending payment of Sharma Traders", "15-09-2026", "20-09-2026"]]
TASK_HELP = ("One task per row. Staff Username (as shown under Setup > Members), Task and Deadline are required. Deadline and "
             "Assigned Date: dd-mm-yyyy; Assigned Date defaults to today and cannot be after the Deadline. Tasks start as "
             "Pending, assigned by the importing admin. Rows marked EXAMPLE are ignored.")


def validate_tasks(rows: list[dict]) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT user_key, lower(username) AS u FROM dim_user WHERE password_hash <> %s", (auth.DISABLED_HASH,))
        staff = {r["u"]: r["user_key"] for r in cur.fetchall()}
    cal = _calendar(parse_date((r.get(c) or "").strip()) for r in rows for c in ("Assigned Date", "Deadline"))
    today = datetime.now(IST).date()
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in TASK_REQUIRED if not g(c)]
        n: dict = {"description": g("Task")}
        if g("Staff Username"):
            key = staff.get(g("Staff Username").lower())
            if key:
                n["staff_user_key"] = key
            else:
                errs.append(f"Staff Username '{g('Staff Username')}' not found (or the account is disabled)")
        if len(g("Task")) > 2000:
            errs.append("Task is longer than 2000 characters")
        seen: list = []
        assigned = _date_field(g, "Assigned Date", seen, errs) or today
        due = _date_field(g, "Deadline", seen, errs)
        _calendar_errors(seen, cal, errs)
        if due and due < assigned:
            errs.append("Deadline is before the Assigned Date")
        n["assigned"], n["due"] = assigned, due
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_tasks(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_task (staff_user_key, assigned_by_user_key, description, assigned_date_key, due_date_key, "
                    "import_batch_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (n["staff_user_key"], admin["user_key"], n["description"], mc.date_key(n["assigned"]),
                     mc.date_key(n["due"]), batch_id))


# ------------------------------------------------------------------ purchase
PURCHASE_COLUMNS = ["Site", "Vendor", "Material Received Date", "Invoice Date", "Invoice No", "Invoice Amount", "Remarks"]
PURCHASE_REQUIRED = ["Site", "Vendor"]
PURCHASE_EXAMPLES = [["EXAMPLE Godown", "Asian Paints Ltd", "14-09-2026", "13-09-2026", "AP-4471", "86500", "Primer, 40 drums"],
                     ["EXAMPLE Shop", "Sharma Hardware", "15-09-2026", "15-09-2026", "SH-221", "12400", ""]]
PURCHASE_HELP = ("One purchase entry per row. Site (Godown or Shop) and Vendor are required. Dates: dd-mm-yyyy. A Vendor that is "
                 "new is added to the vendor list, waiting for an admin to review it - check the spelling, a typo creates a "
                 "second vendor. An entry with the same site, vendor, invoice no and invoice date as an existing one is rejected "
                 "as a duplicate. Photos cannot be uploaded in bulk; add them by editing the entry. Rows marked EXAMPLE are ignored.")


def purchase_lists() -> dict:
    return {"Site": _list_names("dim_purchase_site", "site_key", "site_name"),
            "Vendor": _list_names("dim_party", "party_key", "party_name", "WHERE party_kind = 'vendor'")}


def validate_purchases(rows: list[dict]) -> dict:
    sites = _names("dim_purchase_site", "site_key", "site_name")
    with db.cursor() as cur:
        cur.execute("SELECT e.site_key, lower(btrim(p.party_name)) AS v, lower(e.invoice_no) AS inv, d.full_date AS inv_date "
                    "FROM fact_purchase_entry e JOIN dim_party p ON p.party_key = e.vendor_party_key "
                    "LEFT JOIN dim_date d ON d.date_key = e.invoice_date_key WHERE e.invoice_no IS NOT NULL")
        existing = {(r["site_key"], r["v"], r["inv"], r["inv_date"]) for r in cur.fetchall()}
    cal = _calendar(parse_date((r.get(c) or "").strip()) for r in rows for c in ("Material Received Date", "Invoice Date"))
    seen_rows: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in PURCHASE_REQUIRED if not g(c)]
        n: dict = {}
        if g("Site"):
            k, e = _pick(sites, g("Site"), "Site")
            if e:
                errs.append(e)
            else:
                n["site_key"] = k
        vendor = " ".join(g("Vendor").split())
        if len(vendor) > 150:
            errs.append("Vendor is longer than 150 characters")
        n["vendor"] = vendor
        seen: list = []
        n["material"] = _date_field(g, "Material Received Date", seen, errs)
        n["invoice_date"] = _date_field(g, "Invoice Date", seen, errs)
        _calendar_errors(seen, cal, errs)
        if len(g("Invoice No")) > 80:
            errs.append("Invoice No is longer than 80 characters")
        n["invoice_no"] = g("Invoice No") or None
        n["amount"] = _money(g, "Invoice Amount", errs)
        n["remarks"] = g("Remarks") or None
        if n["invoice_no"] and "site_key" in n and vendor:
            dup = (n["site_key"], vendor.lower(), n["invoice_no"].lower(), n["invoice_date"])
            if dup in existing:
                errs.append(f"Invoice {n['invoice_no']} from {vendor} already exists on this site")
            elif dup in seen_rows:
                errs.append(f"Duplicate of row {seen_rows[dup]} in this file (same site, vendor, invoice no and date)")
            else:
                seen_rows[dup] = r.get("_row")
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_purchases(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    vendors: dict = {}
    for n in normalised:
        v = n["vendor"].lower()
        if v not in vendors:
            cur.execute("SELECT party_key FROM dim_party WHERE party_kind = 'vendor' AND lower(btrim(party_name)) = lower(btrim(%s)) "
                        "AND company_key IS NULL AND txn_type_key IS NULL", (n["vendor"],))
            row = cur.fetchone()
            if not row:   # same rule as typing a new vendor on the Purchase screen
                cur.execute("INSERT INTO dim_party (party_name, party_kind, review_status, created_by_user_key) "
                            "VALUES (%s, 'vendor', 'pending', %s) RETURNING party_key", (n["vendor"], admin["user_key"]))
                row = cur.fetchone()
            vendors[v] = row["party_key"]
        cur.execute("INSERT INTO fact_purchase_entry (site_key, vendor_party_key, material_received_date_key, invoice_date_key, "
                    "invoice_no, invoice_amount, remarks, created_by_user_key, import_batch_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (n["site_key"], vendors[v], mc.date_key(n["material"]), mc.date_key(n["invoice_date"]), n["invoice_no"],
                     n["amount"], n["remarks"], admin["user_key"], batch_id))


# table each entity writes to (used by undo) and the screen label
ENTITIES = {
    "payments": {"label": "payment entries", "columns": PAYMENT_COLUMNS, "required": PAYMENT_REQUIRED, "examples": PAYMENT_EXAMPLES,
                 "help": PAYMENT_HELP, "validate": validate_payments, "insert": insert_payments, "lists": payment_lists,
                 "table": "fact_payment_txn", "edited": "updated_at IS NOT NULL OR status <> 'Approved'"},
    "tasks": {"label": "tasks", "columns": TASK_COLUMNS, "required": TASK_REQUIRED, "examples": TASK_EXAMPLES,
              "help": TASK_HELP, "validate": validate_tasks, "insert": insert_tasks, "lists": lambda: {},
              "table": "fact_task", "edited": "status <> 'Pending' OR updated_at IS NOT NULL"},
    "purchases": {"label": "purchase entries", "columns": PURCHASE_COLUMNS, "required": PURCHASE_REQUIRED, "examples": PURCHASE_EXAMPLES,
                  "help": PURCHASE_HELP, "validate": validate_purchases, "insert": insert_purchases, "lists": purchase_lists,
                  "table": "fact_purchase_entry", "edited": "updated_at IS NOT NULL"},
}
