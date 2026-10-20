# ruff: noqa: E501
"""Bulk upload for the Payments, Delegation (tasks), Purchase, VT Decor and Attendance modules. Same contract as bulk_entities.py: every entity
gives the template columns, EXAMPLE rows, `validate(rows)` and `insert(cur, normalised, batch_id, admin)`; the shared
flow (template, validate, fix, confirm, undo) lives in admin_tools.py. Imports are admin-only (Setup > Import).
"""
import json
import re
from datetime import datetime
from decimal import Decimal

from psycopg2.extras import Json

from . import auth, config, db
from . import modcommon as mc
from .bulk_entities import IST, MAX_MONEY, _report, parse_date, parse_datetime, parse_money


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
# Column order follows the old Payments "Transactions" sheet, so it can be pasted under these headings as it is. The first column
# and the last five are for old entries (Txn ID given = an entry carried over from the old sheet); a hand-made file can leave them empty.
PAYMENT_COLUMNS = ["Txn ID", "Date", "Type", "Company", "Party", "Mode", "Amount", "Account Name", "Invoice No", "Remarks",
                   "Created By", "Created At", "Status", "Approved By", "Approved At"]
PAYMENT_REQUIRED = ["Date", "Type", "Company", "Amount"]
PAYMENT_ALIASES = {"Invoice": "Invoice No", "TxnID": "Txn ID"}
PAYMENT_EXAMPLES = [
    ["", "EXAMPLE 15-09-2026", "Payment Received", "VT", "Sharma Traders", "Cash", "25000", "", "INV-101", "Part payment", "", "", "", "", ""],
    ["EXAMPLE TXN-0450", "16-09-2026", "Expense", "VT", "", "UPI", "12", "", "", "Tea and snacks", "cashier", "16-09-2026 10:30", "Approved", "admin", "16-09-2026 11:00"]]
PAYMENT_HELP = ("One payment entry per row, in the order of the old Payments sheet. Date, Type, Company and Amount are required. Date: dd-mm-yyyy. "
                "Type, Company and Mode must already exist under Setup > Dropdown values (the check lists the valid values); Mode defaults to Cash. "
                "A Party that is new is added to the one party list, waiting for an admin to review it. "
                "Amount: for a hand-made file it is divided by 100 when saved (type 10,000 for 100), like typing it on the Payments page. "
                "For an entry carried over from the old sheet (Txn ID given: TXN-0001 ...) the Amount is the figure the old sheet stored, "
                "already divided by 100, and is saved as it is; the entry keeps its Txn ID (an ID already used is rejected). "
                "Status: Approved (default), Pending or Rejected - Rejected entries are kept but never counted. Created By / Approved By: a "
                "username under Setup > Members; empty = the importing admin. Created At / Approved At: dd-mm-yyyy hh:mm; empty = now. "
                "Rows marked EXAMPLE are ignored.")


def payment_lists() -> dict:
    return {"Type": _list_names("dim_txn_type", "txn_type_key", "type_name"),
            "Company": _list_names("dim_company", "company_key", "company_name"),
            "Mode": _list_names("dim_payment_mode", "mode_key", "mode_name"),
            "Status": ["Approved", "Pending", "Rejected"]}


def _txn_id(raw: str):
    """'TXN-0045' / 'txn-45' / '45' -> 45; None when it is not an ID."""
    m = re.fullmatch(r"(?:[Tt][Xx][Nn][-\s]?)?0*(\d{1,12})", raw.strip())
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _stamp(g, col: str, errs: list):
    """An optional 'dd-mm-yyyy hh:mm' (or a plain date = midnight) -> timezone-aware datetime, else None."""
    raw = g(col)
    if not raw:
        return None
    dt = parse_datetime(raw[:19].replace("T", " "))
    if dt:
        return dt
    d = parse_date(raw[:10])
    if d:
        return datetime(d.year, d.month, d.day, tzinfo=IST)
    errs.append(f"{col} '{raw}' is not a date and time (use dd-mm-yyyy hh:mm)")
    return None


def validate_payments(rows: list[dict]) -> dict:
    types = _names("dim_txn_type", "txn_type_key", "type_name")
    companies = _names("dim_company", "company_key", "company_name")
    modes = _names("dim_payment_mode", "mode_key", "mode_name")
    cal = _calendar(parse_date((r.get("Date") or "").strip()[:10]) for r in rows)
    with db.cursor() as cur:
        cur.execute("SELECT lower(username) AS u, user_key FROM dim_user")
        users = {r["u"]: r["user_key"] for r in cur.fetchall()}
        cur.execute("SELECT txn_key FROM fact_payment_txn")
        used_ids = {r["txn_key"] for r in cur.fetchall()}
    seen_ids: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in PAYMENT_REQUIRED if not g(c)]
        n: dict = {"txn_key": None}
        if g("Txn ID"):
            tid = _txn_id(g("Txn ID"))
            if tid is None:
                errs.append(f"Txn ID '{g('Txn ID')}' must look like TXN-0001")
            elif tid in used_ids:
                errs.append(f"Txn ID TXN-{tid:04d} is already used by another entry")
            elif tid in seen_ids:
                errs.append(f"Txn ID TXN-{tid:04d} is also on row {seen_ids[tid]} of this file")
            else:
                n["txn_key"] = tid
                seen_ids[tid] = r.get("_row")
        seen: list = []
        n["date"] = _day_only(g, "Date", errs, None)
        if n["date"]:
            seen.append(("Date", n["date"]))
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
        typed = _money(g, "Amount", errs, positive=True)
        if typed is None:
            n["amount"] = None
        elif n["txn_key"]:          # carried over from the old sheet: the figure it stored, already divided by 100
            n["amount"] = typed
        else:
            n["amount"] = (typed / config.AMOUNT_RECEIVED_DIVISOR).quantize(Decimal("0.01"))   # divided by 100, like typing it
            if n["amount"] <= 0:
                errs.append("Amount is too small: it is divided by 100 and must stay above 0")
        for col, limit in (("Party", 150), ("Account Name", 150), ("Invoice No", 80)):
            if len(g(col)) > limit:
                errs.append(f"{col} is longer than {limit} characters")
        status = g("Status").title() or "Approved"
        if status not in ("Approved", "Pending", "Rejected"):
            errs.append("Status must be Approved, Pending or Rejected")
        n["status"] = status
        for col, key in (("Created By", "created_by"), ("Approved By", "approved_by")):
            who = g(col).lower()
            if who and who not in users:
                errs.append(f"{col} '{g(col)}' is not a username under Setup > Members")
            n[key] = users.get(who)
        n["created_at"], n["approved_at"] = _stamp(g, "Created At", errs), _stamp(g, "Approved At", errs)
        n.update(party=" ".join(g("Party").split()), account=g("Account Name") or None,
                 invoice=g("Invoice No") or None, remarks=g("Remarks") or None)
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


_MOVE_TXN_SEQUENCE = ("SELECT setval(pg_get_serial_sequence('fact_payment_txn', 'txn_key'), "
                      "GREATEST((SELECT COALESCE(max(txn_key), 0) FROM fact_payment_txn), 1))")


def insert_payments(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    from . import payments
    cols = ("txn_date_key, txn_type_key, company_key, party_key, mode_key, amount, account_name, invoice_no, remarks, status, "
            "is_backdated, created_by_user_key, created_at, approved_by_user_key, approved_at, import_batch_id")
    # entries that keep their old Txn ID go first; the sequence is then moved past them so the others get free numbers
    for n in sorted(normalised, key=lambda x: x["txn_key"] is None):
        party = payments._party_key(cur, n["party"], admin) if n["party"] else None
        decided = n["status"] in ("Approved", "Rejected")
        created_day = (n["created_at"].astimezone(IST).date() if n["created_at"] else mc.today_ist())
        vals = (mc.date_key(n["date"]), n["type_key"], n["company_key"], party, n["mode_key"], n["amount"], n["account"], n["invoice"],
                n["remarks"], n["status"], n["date"] < created_day, n["created_by"] or admin["user_key"], n["created_at"],
                (n["approved_by"] or admin["user_key"]) if decided else None, n["approved_at"], batch_id)
        marks = ["%s"] * 12 + ["COALESCE(%s, now())", "%s", "COALESCE(%s, now())" if decided else "%s", "%s"]
        if n["txn_key"] is None:
            cur.execute(_MOVE_TXN_SEQUENCE)
            cur.execute(f"INSERT INTO fact_payment_txn ({cols}) VALUES ({','.join(marks)})", vals)
        else:
            cur.execute(f"INSERT INTO fact_payment_txn (txn_key, {cols}) VALUES (%s,{','.join(marks)})", (n["txn_key"], *vals))
    cur.execute(_MOVE_TXN_SEQUENCE)


# ------------------------------------------------------------------ payment parties (the one party list)
PARTY_COLUMNS = ["Party Name"]
PARTY_REQUIRED = ["Party Name"]
PARTY_EXAMPLES = [["EXAMPLE Sharma Traders"], ["EXAMPLE Gupta & Sons"]]
PARTY_HELP = ("One party per row; this is the list the Payments entry form suggests from. Party Name is required, up to 150 characters. "
              "A name already in the list (capital letters and extra spaces do not matter) is rejected as a duplicate, and so is a repeat "
              "inside the file. Uploaded parties are approved straight away. A party typed on an entry that is not in the list is added "
              "to it by itself later. Rows marked EXAMPLE are ignored.")


def validate_parties(rows: list[dict]) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT lower(btrim(party_name)) AS n FROM dim_party WHERE party_kind = 'payment'")
        existing = {r["n"] for r in cur.fetchall()}
    seen_rows: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        name = " ".join(g("Party Name").split())
        if not name:
            errs.append("Party Name is required")
        elif len(name) > 150:
            errs.append("Party Name is longer than 150 characters")
        elif name.lower() in existing:
            errs.append(f"{name} is already in the party list")
        elif name.lower() in seen_rows:
            errs.append(f"Duplicate of row {seen_rows[name.lower()]} in this file")
        else:
            seen_rows[name.lower()] = r.get("_row")
        results.append((r, "; ".join(errs) or None, None if errs else {"name": name}))
    return _report(rows, results)


def insert_parties(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO dim_party (party_name, party_kind, review_status, created_by_user_key, import_batch_id) "
                    "VALUES (%s, 'payment', 'approved', %s, %s)", (n["name"], admin["user_key"], batch_id))


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
PURCHASE_COLUMNS = ["Site", "Vendor", "Material Received Date", "Invoice Date", "Invoice No", "Invoice Amount", "Remarks", "Photos"]
PURCHASE_ALIASES = {"Vendor name": "Vendor", "Invoice number": "Invoice No", "Source": "Site"}
PURCHASE_REQUIRED = ["Site", "Vendor"]
PURCHASE_EXAMPLES = [["EXAMPLE Godown", "Asian Paints Ltd", "14-09-2026", "13-09-2026", "AP-4471", "86500", "Primer, 40 drums", "https://drive.google.com/uc?export=view&id=EXAMPLE1 | https://drive.google.com/uc?export=view&id=EXAMPLE2"],
                     ["EXAMPLE Shop", "Sharma Hardware", "15-09-2026", "15-09-2026", "SH-221", "12400", "", ""]]
PURCHASE_HELP = ("One purchase entry per row. Site (Godown or Shop) and Vendor are required. Dates: dd-mm-yyyy. A Vendor that is "
                 "new is added to the vendor list, waiting for an admin to review it - check the spelling, a typo creates a "
                 "second vendor. An entry with the same site, vendor, invoice no, invoice date and amount as an existing one is rejected "
                 "as a duplicate. Photos: only links can be loaded (the old sheet's Google Drive links, several separated by ' | '); the pictures stay in "
                 "Drive and must stay shared. New photos are added by editing the entry. Rows marked EXAMPLE are ignored.")


def purchase_lists() -> dict:
    return {"Site": _list_names("dim_purchase_site", "site_key", "site_name"),
            "Vendor": _list_names("dim_party", "party_key", "party_name", "WHERE party_kind = 'vendor'")}


def validate_purchases(rows: list[dict]) -> dict:
    sites = _names("dim_purchase_site", "site_key", "site_name")
    with db.cursor() as cur:
        cur.execute("SELECT e.site_key, lower(btrim(p.party_name)) AS v, lower(e.invoice_no) AS inv, d.full_date AS inv_date, e.invoice_amount AS amt "
                    "FROM fact_purchase_entry e JOIN dim_party p ON p.party_key = e.vendor_party_key "
                    "LEFT JOIN dim_date d ON d.date_key = e.invoice_date_key WHERE e.invoice_no IS NOT NULL")
        existing = {(r["site_key"], r["v"], r["inv"], r["inv_date"], r["amt"]) for r in cur.fetchall()}
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
        links = [x.strip() for x in g("Photos").split(" | ") if x.strip()]
        if any(not x.lower().startswith(("http://", "https://", "data:image/")) for x in links):
            errs.append("Photos must be links starting with https:// (several separated by ' | ')")
        n["photos"] = " | ".join(links) or None
        if n["invoice_no"] and "site_key" in n and vendor:
            dup = (n["site_key"], vendor.lower(), n["invoice_no"].lower(), n["invoice_date"], n["amount"])
            if dup in existing:
                errs.append(f"Invoice {n['invoice_no']} from {vendor} already exists on this site")
            elif dup in seen_rows:
                errs.append(f"Duplicate of row {seen_rows[dup]} in this file (same site, vendor, invoice no, date and amount)")
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
                    "invoice_no, invoice_amount, remarks, legacy_photo_urls, created_by_user_key, import_batch_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (n["site_key"], vendors[v], mc.date_key(n["material"]), mc.date_key(n["invoice_date"]), n["invoice_no"],
                     n["amount"], n["remarks"], n.get("photos"), admin["user_key"], batch_id))


# ------------------------------------------------------------------ purchase, in the Payments sheet layout
# The same column layout as the Payments upload (Date, Type, Company, Party, Mode, Amount, Account Name, Invoice No, Remarks) plus Site, so one
# sheet format serves both dashboards. Party is the vendor; Type / Company / Mode / Account Name have no home on a purchase entry and are ignored.
PURCHASE_PF_COLUMNS = ["Date", "Type", "Company", "Party", "Mode", "Amount", "Account Name", "Invoice No", "Remarks", "Site"]
PURCHASE_PF_REQUIRED = ["Date", "Site", "Party", "Amount"]
PURCHASE_PF_EXAMPLES = [
    ["EXAMPLE 15-09-2026", "", "", "Asian Paints Ltd", "", "86500", "", "AP-4471", "Primer, 40 drums", "Godown"],
    ["EXAMPLE 16-09-2026", "", "", "Sharma Hardware", "", "12400", "", "SH-221", "", "Shop"]]
PURCHASE_PF_HELP = ("Purchase entries in the same layout as the Payments upload, plus a Site column. Date, Site (Godown or Shop), Party "
                    "(the vendor) and Amount are required. Date: dd-mm-yyyy; it is saved as both the material-received date and the invoice date. "
                    "Amount is the invoice amount in rupees as typed (NOT divided by 100, unlike a Payments upload). Type, Company, Mode and Account Name "
                    "are not part of a purchase entry and are ignored. A Party that is new is added to the vendor list, waiting for an admin to review it. "
                    "An entry with the same site, vendor, invoice no, date and amount as an existing one is rejected as a duplicate. Rows marked EXAMPLE are ignored.")
_PF_NAMES = (("Vendor", "Party"), ("Invoice Amount", "Amount"), ("Material Received Date", "Date"), ("Invoice Date", "Date"))


def purchase_pf_lists() -> dict:
    return {"Site": _list_names("dim_purchase_site", "site_key", "site_name"),
            "Party": _list_names("dim_party", "party_key", "party_name", "WHERE party_kind = 'vendor'")}


def validate_purchases_pf(rows: list[dict]) -> dict:
    translated = []
    for r in rows:
        g = _row_getter(r)
        translated.append({"_row": r.get("_row"), "Site": g("Site"), "Vendor": g("Party"), "Material Received Date": g("Date"),
                           "Invoice Date": g("Date"), "Invoice No": g("Invoice No"), "Invoice Amount": g("Amount"), "Remarks": g("Remarks")})
    inner = validate_purchases(translated)
    bad = {e["row"]: e["error"] for e in inner["errors"]}
    good = {t.get("_row"): n for t, n in zip(inner["rows"], inner["_normalised"])}
    results = []
    for r in rows:
        g = _row_getter(r)
        err = bad.get(r.get("_row"))
        if err:
            for old, new_name in _PF_NAMES:
                err = err.replace(old, new_name)
        missing = [f"{c} is required" for c in ("Date", "Amount") if not g(c)]
        if missing and not err:
            err = "; ".join(missing)
        results.append((r, err, None if err else good.get(r.get("_row"))))
    return _report(rows, results)


# table each entity writes to (used by undo) and the screen label
ENTITIES = {
    "payments": {"label": "payment entries", "columns": PAYMENT_COLUMNS, "required": PAYMENT_REQUIRED, "examples": PAYMENT_EXAMPLES,
                 "help": PAYMENT_HELP, "validate": validate_payments, "insert": insert_payments, "lists": payment_lists,
                 "table": "fact_payment_txn", "aliases": PAYMENT_ALIASES,
                 "edited": "updated_at IS NOT NULL OR approved_at > (SELECT b.created_at FROM import_batch b WHERE b.batch_id = fact_payment_txn.import_batch_id)"},
    "tasks": {"label": "tasks", "columns": TASK_COLUMNS, "required": TASK_REQUIRED, "examples": TASK_EXAMPLES,
              "help": TASK_HELP, "validate": validate_tasks, "insert": insert_tasks, "lists": lambda: {},
              "table": "fact_task", "edited": "status <> 'Pending' OR updated_at IS NOT NULL"},
    "purchases": {"label": "purchase entries", "columns": PURCHASE_COLUMNS, "required": PURCHASE_REQUIRED, "examples": PURCHASE_EXAMPLES,
                  "help": PURCHASE_HELP, "validate": validate_purchases, "insert": insert_purchases, "lists": purchase_lists, "aliases": PURCHASE_ALIASES,
                  "table": "fact_purchase_entry", "edited": "updated_at IS NOT NULL"},
    "purchases_pf": {"label": "purchase entries (Payments layout)", "columns": PURCHASE_PF_COLUMNS, "required": PURCHASE_PF_REQUIRED,
                     "examples": PURCHASE_PF_EXAMPLES, "help": PURCHASE_PF_HELP, "validate": validate_purchases_pf,
                     "insert": insert_purchases, "lists": purchase_pf_lists, "table": "fact_purchase_entry", "edited": "updated_at IS NOT NULL"},
}


# ================================================================== VT Decor
# Decor money is stored in real rupees. The screen divides what is typed by 100; a bulk upload does NOT - the figure in the file
# is the rupee amount that will be stored and shown.
YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}


def _yes_no(g, col: str, errs: list) -> bool:
    raw = g(col).lower()
    if not raw or raw in NO:
        return False
    if raw in YES:
        return True
    errs.append(f"{col} must be Yes or No")
    return False


def _client_codes() -> dict:
    """{upper client code: client_key}."""
    with db.cursor() as cur:
        cur.execute("SELECT upper(client_code) AS c, client_key FROM dim_decor_client")
        return {r["c"]: r["client_key"] for r in cur.fetchall()}


def _optional_date(g, col: str, errs: list, default=None):
    seen: list = []
    d = _date_field(g, col, seen, errs)
    return d or default


def _day_only(g, col: str, errs: list, default):
    """An optional date; a sheet's timestamp ('dd-mm-yyyy hh:mm', ISO 'yyyy-mm-dd hh:mm:ss') is accepted too - the time is dropped."""
    raw = g(col)
    if len(raw) > 10:
        dt = parse_datetime(raw[:19].replace("T", " "))
        if dt:
            return dt.date()
    return _optional_date(g, col, errs, default)


def _code(raw: str) -> str:
    """'c1', '1' and 'C001' are the same client code."""
    cid = _client_id(raw)
    return f"C{cid:03d}" if cid else raw.strip().upper()


# ------------------------------------------------------------------ decor clients
# Column order follows the old VT Decor "Master" sheet, so its columns can be pasted under these headings as they are.
DECOR_CLIENT_COLUMNS = ["Client ID", "Client Name", "Mobile", "Address", "Product", "Contract Amount", "Start Date", "Notes", "Status",
                        "Assigned To", "Added By", "Product Type"]
DECOR_CLIENT_REQUIRED = ["Client Name", "Mobile"]
DECOR_CLIENT_ALIASES = {"Name": "Client Name", "Amount": "Contract Amount", "Date": "Start Date", "Client Code": "Client ID"}
DECOR_CLIENT_EXAMPLES = [
    ["EXAMPLE C001", "EXAMPLE Sharma Residence", "9811000001", "B-12 Pitampura", "Living room wallpaper", "120000", "10-09-2026", "", "Active", "", "", "Wallpaper"],
    ["", "EXAMPLE Gupta Villa", "9811000002", "", "", "85000", "", "Two bedrooms", "Active", "example.ravi", "", "Paint Work"]]
DECOR_CLIENT_HELP = ("One client per row, in the same column order as the old VT Decor Master sheet. Client Name and Mobile are required. "
                     "Client ID (C001, C002 ...) is the old sheet's ID: give it to keep that ID - the payments, expenses and items you "
                     "upload afterwards refer to it. Leave it empty and the next free code is given. An ID that is already used is rejected. "
                     "Product Type must exist under Setup > Dropdown values > VT Decor - product types; empty = Wallpaper (as the old sheet did). "
                     "Contract Amount is the real rupee amount (it is not divided by 100 like typing on the VT Decor screen); it already "
                     "includes any items, so do not add the items again. Start Date: dd-mm-yyyy, today if empty. Status: Active (default), "
                     "Completed or Hidden. Assigned To: a staff username (as shown under Setup > Members), or owner; empty = Added By, "
                     "else the owner. Added By: whoever created the client (free text, lower case; that person can see the client). Without a "
                     "Client ID, a client with the same name and mobile as an existing one is rejected as a duplicate. Rows marked EXAMPLE "
                     "are ignored.")


def decor_client_lists() -> dict:
    return {"Product Type": _list_names("dim_decor_product_type", "type_key", "type_name"), "Status": ["Active", "Completed", "Hidden"]}


def _client_id(raw: str):
    """'C012' / 'c12' / '12' -> 12; None when it is not an ID."""
    m = re.fullmatch(r"[Cc]?\s*0*(\d{1,9})", raw.strip())
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def validate_decor_clients(rows: list[dict]) -> dict:
    types = _names("dim_decor_product_type", "type_key", "type_name")
    with db.cursor() as cur:
        cur.execute("SELECT lower(username) AS u FROM dim_user WHERE password_hash <> %s", (auth.DISABLED_HASH,))
        staff = {r["u"] for r in cur.fetchall()} | {"owner"}
        cur.execute("SELECT lower(btrim(client_name)) AS n, btrim(mobile) AS m FROM dim_decor_client")
        existing = {(r["n"], r["m"]) for r in cur.fetchall()}
        cur.execute("SELECT client_key FROM dim_decor_client")
        used_ids = {r["client_key"] for r in cur.fetchall()}
    seen_rows: dict = {}
    seen_ids: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_CLIENT_REQUIRED if not g(c)]
        n: dict = {}
        name, mobile = " ".join(g("Client Name").split()), g("Mobile")
        if len(name) > 150:
            errs.append("Client Name is longer than 150 characters")
        if len(mobile) > 30:
            errs.append("Mobile is longer than 30 characters")
        n.update(name=name, mobile=mobile, address=g("Address") or None, product=g("Product") or None, notes=g("Notes") or None)
        n["client_key"] = None
        if g("Client ID"):
            cid = _client_id(g("Client ID"))
            if cid is None:
                errs.append(f"Client ID '{g('Client ID')}' must look like C001")
            elif cid in used_ids:
                errs.append(f"Client ID C{cid:03d} is already used by another client")
            elif cid in seen_ids:
                errs.append(f"Client ID C{cid:03d} is also on row {seen_ids[cid]} of this file")
            else:
                n["client_key"] = cid
                seen_ids[cid] = r.get("_row")
        k, e = _pick(types, g("Product Type") or "Wallpaper", "Product Type")
        if e:
            errs.append(e)
        else:
            n["type_key"] = k
        n["amount"] = _money(g, "Contract Amount", errs) or Decimal("0.00")
        n["start"] = _day_only(g, "Start Date", errs, mc.today_ist())
        added = g("Added By").lower()
        if len(added) > 60:
            errs.append("Added By is longer than 60 characters")
        assigned = g("Assigned To").lower() or added
        if assigned and assigned not in staff:
            errs.append(f"Assigned To '{assigned}' is not a username under Setup > Members (add the member first, or use owner)")
        n["assigned"] = assigned or "owner"
        n["added_by"] = added or "owner"
        status = g("Status").title() or "Active"
        if status not in ("Active", "Completed", "Hidden"):
            errs.append("Status must be Active, Completed or Hidden")
        n["status"] = status
        if name and mobile and not g("Client ID"):          # an old ID already tells two clients apart
            dup = (name.lower(), mobile)
            if dup in existing:
                errs.append(f"{name} ({mobile}) is already a client")
            elif dup in seen_rows:
                errs.append(f"Duplicate of row {seen_rows[dup]} in this file (same name and mobile)")
            else:
                seen_rows[dup] = r.get("_row")
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


_MOVE_SEQUENCE = ("SELECT setval(pg_get_serial_sequence('dim_decor_client', 'client_key'), "
                  "GREATEST((SELECT COALESCE(max(client_key), 0) FROM dim_decor_client), 1))")


def insert_decor_clients(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    cols = ("client_name, mobile, address, product, contract_amount, start_date, notes, status, assigned_to, added_by, "
            "product_type_key, import_batch_id")
    # clients that bring their old ID go first; the sequence is then moved past them so the others get free codes
    for n in sorted(normalised, key=lambda x: x["client_key"] is None):
        vals = (n["name"], n["mobile"], n["address"], n["product"], n["amount"], n["start"], n["notes"], n["status"], n["assigned"],
                n["added_by"], n["type_key"], batch_id)
        if n["client_key"] is None:
            cur.execute(_MOVE_SEQUENCE)
            cur.execute(f"INSERT INTO dim_decor_client ({cols}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", vals)
        else:
            cur.execute(f"INSERT INTO dim_decor_client (client_key, {cols}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (n["client_key"], *vals))
    cur.execute(_MOVE_SEQUENCE)


# ------------------------------------------------------------------ decor payments
# Column order follows the old PaymentsLog sheet. Its Timestamp and Client Name columns are not needed: if they are in the file they are ignored.
DECOR_PAYMENT_COLUMNS = ["Client Code", "Stage", "Amount", "Mode", "Date", "Reference No", "Remarks"]
DECOR_PAYMENT_REQUIRED = ["Client Code", "Amount"]
DECOR_PAYMENT_ALIASES = {"Client ID": "Client Code", "PNum": "Reference No", "Payment No": "Reference No"}
DECOR_PAYMENT_EXAMPLES = [["EXAMPLE C001", "Stage 1 - Visit", "48000", "UPI", "12-09-2026", "UTR123456", "Advance"],
                          ["EXAMPLE C002", "", "20000", "Cash", "", "", ""]]
DECOR_PAYMENT_HELP = ("One payment received from a client per row (the old PaymentsLog sheet: Client ID, Stage, Amount, Mode, Date, PNum, "
                      "Remarks; its Timestamp and Client Name columns can stay in the file, they are ignored). Client Code (C001 ...) and "
                      "Amount are required; the client must already exist (import the clients first, with their old Client ID). Amount is the "
                      "real rupee amount (not divided by 100). Mode must exist under Setup > Dropdown values > Payment modes, empty = Cash; "
                      "an old mode that is not in the list is reported - add it to the list or correct the file. Date: dd-mm-yyyy, today if "
                      "empty (a time after the date is fine). Stage: a stage of that client's product type; an old stage name that is not "
                      "in the list is kept as plain text. Rows marked EXAMPLE are ignored.")


def decor_payment_lists() -> dict:
    return {"Mode": _list_names("dim_payment_mode", "mode_key", "mode_name")}


def validate_decor_payments(rows: list[dict]) -> dict:
    codes = _client_codes()
    modes = _names("dim_payment_mode", "mode_key", "mode_name")
    with db.cursor() as cur:
        cur.execute("SELECT s.product_type_key AS t, s.stage_key AS k, lower(btrim(s.stage_name)) AS n FROM dim_decor_stage s")
        stages = {(r["t"], r["n"]): r["k"] for r in cur.fetchall()}
        cur.execute("SELECT upper(client_code) AS c, product_type_key FROM dim_decor_client")
        ctype = {r["c"]: r["product_type_key"] for r in cur.fetchall()}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_PAYMENT_REQUIRED if not g(c)]
        n: dict = {}
        code = _code(g("Client Code"))
        if code:
            if code in codes:
                n["client_key"] = codes[code]
            else:
                errs.append(f"Client Code '{g('Client Code')}' not found")
        n["amount"] = _money(g, "Amount", errs, positive=True)
        value = g("Mode") or "Cash"
        k, e = _pick(modes, value, "Mode")
        if e:
            errs.append(e)
        n["mode_key"] = k
        n["date"] = _day_only(g, "Date", errs, mc.today_ist())
        stage = g("Stage")
        n["stage_key"], n["stage_text"] = None, None
        if stage:
            sk = stages.get((ctype.get(code), stage.strip().lower()))
            n["stage_key"], n["stage_text"] = (sk, None) if sk else (None, stage[:80])
        if len(g("Reference No")) > 80:
            errs.append("Reference No is longer than 80 characters")
        n["ref"], n["remarks"] = g("Reference No") or None, g("Remarks") or None
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_decor_payments(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_decor_payment (client_key, stage_key, stage_text, amount, mode_key, pay_date, ref_no, remarks, "
                    "created_by_user_key, import_batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (n["client_key"], n["stage_key"], n["stage_text"], n["amount"], n["mode_key"], n["date"], n["ref"], n["remarks"],
                     admin["user_key"], batch_id))


# ------------------------------------------------------------------ decor expenses
# Column order follows the old ExpensesLog sheet (Timestamp and Client Name are ignored if present).
DECOR_EXPENSE_COLUMNS = ["Client Code", "Expense Type", "Amount", "Date", "Notes"]
DECOR_EXPENSE_REQUIRED = ["Client Code", "Expense Type", "Amount"]
DECOR_EXPENSE_ALIASES = {"Client ID": "Client Code", "Type": "Expense Type"}
DECOR_EXPENSE_EXAMPLES = [["EXAMPLE C001", "Materials", "15000", "13-09-2026", "Adhesive and tools"],
                          ["EXAMPLE C001", "Labour", "6000", "", ""]]
DECOR_EXPENSE_HELP = ("One expense on a client's job per row (the old ExpensesLog sheet: Client ID, Type, Amount, Date, Notes; its Timestamp and "
                      "Client Name columns can stay, they are ignored). Client Code (C001 ...), Expense Type and Amount are required; the "
                      "client must already exist. Expense Type is free text (the Setup list only offers suggestions). Amount is the real "
                      "rupee amount (not divided by 100). Date: dd-mm-yyyy, today if empty (a time after the date is fine). Rows marked "
                      "EXAMPLE are ignored.")


def decor_expense_lists() -> dict:
    return {"Expense Type": _list_names("dim_decor_expense_type", "expense_type_key", "expense_type_name")}


def validate_decor_expenses(rows: list[dict]) -> dict:
    codes = _client_codes()
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_EXPENSE_REQUIRED if not g(c)]
        n: dict = {}
        code = _code(g("Client Code"))
        if code:
            if code in codes:
                n["client_key"] = codes[code]
            else:
                errs.append(f"Client Code '{g('Client Code')}' not found")
        if len(g("Expense Type")) > 100:
            errs.append("Expense Type is longer than 100 characters")
        n["type"] = g("Expense Type")
        n["amount"] = _money(g, "Amount", errs, positive=True)
        n["date"] = _day_only(g, "Date", errs, mc.today_ist())
        n["notes"] = g("Notes") or None
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_decor_expenses(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_decor_expense (client_key, expense_type, amount, exp_date, notes, created_by_user_key, import_batch_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", (n["client_key"], n["type"], n["amount"], n["date"], n["notes"], admin["user_key"], batch_id))


# ------------------------------------------------------------------ decor items
# The old ItemsLog sheet: the products of one client's job. Items are the detail of the contract: the client's Contract Amount
# (Master sheet) already includes them, so importing items never changes it.
DECOR_ITEM_COLUMNS = ["Client Code", "Product", "Amount", "Date", "Notes", "Item No"]
DECOR_ITEM_REQUIRED = ["Client Code", "Product"]
DECOR_ITEM_ALIASES = {"Client ID": "Client Code", "Item Index": "Item No"}
DECOR_ITEM_EXAMPLES = [["EXAMPLE C001", "Wallpaper - living room", "90000", "10-09-2026", "", "1"],
                       ["EXAMPLE C001", "Wallpaper - bedroom", "30000", "", "", ""]]
DECOR_ITEM_HELP = ("One product of a client's job per row (the old ItemsLog sheet: Client ID, Product, Amount, Date, Notes, ItemIndex; its "
                   "Timestamp and Client Name columns can stay, they are ignored). Client Code (C001 ...) and Product are required; the client "
                   "must already exist. Amount is the real rupee amount (not divided by 100). It is only recorded on the item: the client's "
                   "Contract Amount is NOT changed (the old Master amount already includes its items). Item No: 1, 2, 3 ... within the "
                   "client; empty = the next number. Date: dd-mm-yyyy, today if empty. Rows marked EXAMPLE are ignored.")


def validate_decor_items(rows: list[dict]) -> dict:
    codes = _client_codes()
    with db.cursor() as cur:
        cur.execute("SELECT client_key, COALESCE(max(item_index), 0) AS m FROM fact_decor_item GROUP BY client_key")
        last = {r["client_key"]: r["m"] for r in cur.fetchall()}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_ITEM_REQUIRED if not g(c)]
        n: dict = {}
        code = _code(g("Client Code"))
        if code:
            if code in codes:
                n["client_key"] = codes[code]
            else:
                errs.append(f"Client Code '{g('Client Code')}' not found")
        n["product"] = g("Product")
        n["amount"] = _money(g, "Amount", errs) or Decimal("0.00")
        n["date"] = _day_only(g, "Date", errs, mc.today_ist())
        n["notes"] = g("Notes") or None
        n["index"] = None
        if g("Item No"):
            if g("Item No").isdigit() and int(g("Item No")) > 0:
                n["index"] = int(g("Item No"))
            else:
                errs.append(f"Item No '{g('Item No')}' must be a whole number from 1")
        results.append((r, "; ".join(errs) or None, None if errs else n))
    rep = _report(rows, results)
    # numbers for the rows without one: after the client's highest (existing or in this file), in file order
    top = dict(last)
    for n in rep["_normalised"]:
        if n["index"]:
            top[n["client_key"]] = max(top.get(n["client_key"], 0), n["index"])
    for n in rep["_normalised"]:
        if not n["index"]:
            top[n["client_key"]] = top.get(n["client_key"], 0) + 1
            n["index"] = top[n["client_key"]]
    return rep


def insert_decor_items(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_decor_item (client_key, item_index, product, amount, item_date, notes, created_by_user_key, import_batch_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (n["client_key"], n["index"], n["product"], n["amount"], n["date"], n["notes"], admin["user_key"], batch_id))


# ------------------------------------------------------------------ decor photos
# The old PhotosLog sheet: site photos and expense challans that live in Google Drive. Only the link is carried over (legacy_url);
# the picture stays in Drive, so that Drive file must stay shared. Photos uploaded in the portal are stored in the portal itself.
DECOR_PHOTO_COLUMNS = ["Client Code", "Stage", "Caption", "Photo URL", "Kind"]
DECOR_PHOTO_REQUIRED = ["Client Code", "Photo URL"]
DECOR_PHOTO_ALIASES = {"Client ID": "Client Code", "PhotoURL": "Photo URL", "URL": "Photo URL"}
DECOR_PHOTO_EXAMPLES = [["EXAMPLE C001", "Expense Challan", "[Challan] Material", "https://lh3.googleusercontent.com/d/EXAMPLEFILEID", "challan"],
                        ["EXAMPLE C002", "Stage 2 - Measurement", "Bedroom wall", "https://drive.google.com/file/d/EXAMPLEFILEID/view", "photo"]]
DECOR_PHOTO_HELP = ("One photo or challan link per row (the old PhotosLog sheet: Client ID, Stage, Caption, Photo URL; its Timestamp, Client Name "
                    "and Drive File Id columns can stay, they are ignored). Client Code (C001 ...) and Photo URL (https://...) are required; the "
                    "client must already exist. Only the link is saved - the picture stays in Google Drive and must stay shared. Kind: photo or "
                    "challan; empty = challan when the caption starts with [Challan], else photo. Rows marked EXAMPLE are ignored.")


def validate_decor_photos(rows: list[dict]) -> dict:
    codes = _client_codes()
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_PHOTO_REQUIRED if not g(c)]
        n: dict = {}
        code = _code(g("Client Code"))
        if code:
            if code in codes:
                n["client_key"] = codes[code]
            else:
                errs.append(f"Client Code '{g('Client Code')}' not found")
        url = g("Photo URL")
        if url and not url.lower().startswith(("http://", "https://")):
            errs.append("Photo URL must start with https://")
        if len(url) > 1000:
            errs.append("Photo URL is longer than 1000 characters")
        n["url"] = url
        if len(g("Stage")) > 80:
            errs.append("Stage is longer than 80 characters")
        n["stage"], n["caption"] = g("Stage") or None, g("Caption") or None
        kind = g("Kind").lower() or ("challan" if g("Caption").lower().startswith("[challan]") else "photo")
        if kind not in ("photo", "challan"):
            errs.append("Kind must be photo or challan")
        n["kind"] = kind
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_decor_photos(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_decor_photo (client_key, stage_text, caption, kind, legacy_url, created_by_user_key, import_batch_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", (n["client_key"], n["stage"], n["caption"], n["kind"], n["url"], admin["user_key"], batch_id))


# ------------------------------------------------------------------ decor quotations
# The old QuotationsLog sheet: Timestamp, Performa No, Created By, Client, Mobile, Address, GSTIN, Date, Subtotal, CGST %, SGST %, Grand Total, Items.
DECOR_QUOTE_COLUMNS = ["Performa No", "Created By", "Client", "Mobile", "Address", "GSTIN", "Date", "Subtotal", "CGST %", "SGST %",
                       "Grand Total", "Items JSON"]
DECOR_QUOTE_REQUIRED = ["Performa No", "Client"]
DECOR_QUOTE_ALIASES = {"quot_id": "Performa No", "performa_no": "Performa No", "addr": "Address", "cgst_pct": "CGST %", "sgst_pct": "SGST %",
                       "grand": "Grand Total", "items": "Items JSON", "items_json": "Items JSON"}
DECOR_QUOTE_EXAMPLES = [["EXAMPLE VT-0001", "owner", "Sharma Residence", "9811000001", "B-12 Pitampura", "", "20-09-2026", "10000", "9", "9", "11800",
                         '[{"desc":"Wallpaper","area":"W-101","qty":2,"mrp":5000,"disc":0,"discAmt":0,"amt":10000}]']]
DECOR_QUOTE_HELP = ("One saved quotation per row (the old QuotationsLog sheet). Performa No (VT-0001 ...) and Client are required; a number that "
                    "already exists is rejected, so each quotation keeps its number. Created By: who made it (free text). Date: dd-mm-yyyy. "
                    "Subtotal and Grand Total are real rupee amounts; CGST % / SGST % are percentages. Items JSON is the list of lines exactly as "
                    "the old sheet kept it ([{\"desc\":..., \"area\":..., \"qty\":..., \"mrp\":..., \"disc\":..., \"discAmt\":..., \"amt\":...}]). "
                    "Rows marked EXAMPLE are ignored.")


def validate_decor_quotations(rows: list[dict]) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT upper(performa_no) AS p FROM fact_decor_quotation")
        existing = {r["p"] for r in cur.fetchall()}
    seen: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in DECOR_QUOTE_REQUIRED if not g(c)]
        n: dict = {}
        no = g("Performa No").upper()
        if len(no) > 20:
            errs.append("Performa No is longer than 20 characters")
        elif no in existing:
            errs.append(f"Performa No {no} already exists")
        elif no in seen:
            errs.append(f"Performa No {no} is also on row {seen[no]} of this file")
        elif no:
            seen[no] = r.get("_row")
        n["no"] = no
        for col, lim in (("Created By", 60), ("Client", 150), ("Mobile", 30), ("GSTIN", 30)):
            if len(g(col)) > lim:
                errs.append(f"{col} is longer than {lim} characters")
        n.update(created_by=g("Created By") or None, client=g("Client"), mobile=g("Mobile") or None, address=g("Address") or None,
                 gstin=g("GSTIN") or None)
        day = _day_only(g, "Date", errs, None)
        n["date"] = day.isoformat() if day else None
        n["subtotal"] = _money(g, "Subtotal", errs) or Decimal("0.00")
        n["grand"] = _money(g, "Grand Total", errs) or Decimal("0.00")
        for col, key in (("CGST %", "cgst"), ("SGST %", "sgst")):
            v = parse_money(g(col)) if g(col) else Decimal(0)
            if v is None or v < 0 or v > 100:
                errs.append(f"{col} '{g(col)}' must be a percentage from 0 to 100")
                v = Decimal(0)
            n[key] = v
        items: list = []
        if g("Items JSON"):
            try:
                items = json.loads(g("Items JSON"))
            except ValueError:
                items = None
            if not isinstance(items, list):
                errs.append("Items JSON must be a list like [{\"desc\":\"...\",\"qty\":1,\"mrp\":100,\"amt\":100}]")
                items = []
        n["items"] = items
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_decor_quotations(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_decor_quotation (performa_no, created_by, client_name, mobile, address, gstin, quote_date, subtotal, "
                    "cgst_pct, sgst_pct, grand_total, items, import_batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (n["no"], n["created_by"], n["client"], n["mobile"], n["address"], n["gstin"], n["date"], n["subtotal"], n["cgst"],
                     n["sgst"], n["grand"], Json(n["items"]), batch_id))


# ================================================================== Attendance
def _employees() -> dict:
    """{lower name: (employee_key, name)}, active or not (past attendance may belong to someone who left)."""
    return _names("dim_att_employee", "employee_key", "employee_name")


def _time_field(g, col: str, errs: list, required: bool = False):
    """'09:05', '9:05 AM', '18:30:00' -> datetime.time. Returns None when empty or wrong (the error is recorded)."""
    raw = g(col)
    if not raw:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(raw.upper(), fmt).time().replace(second=0, microsecond=0)
        except ValueError:
            continue
    errs.append(f"{col} '{raw}' is not a time (use hh:mm, 24-hour, like 09:05 or 18:30)")
    return None


# ------------------------------------------------------------------ attendance: employees
ATT_EMPLOYEE_COLUMNS = ["Employee Name", "Designation", "Monthly Salary", "No WhatsApp"]
ATT_EMPLOYEE_REQUIRED = ["Employee Name"]
ATT_EMPLOYEE_EXAMPLES = [["EXAMPLE Ramesh Kumar", "Godown Executive", "18000", "No"],
                         ["EXAMPLE Sunita", "Billing Executive", "", "Yes"]]
ATT_EMPLOYEE_HELP = ("One employee per row. Employee Name is required and must not already be on the employee list (spelling "
                     "differences in capital letters or spaces count as the same name). Designation is free text. Monthly Salary is optional "
                     "(rupees, above 0). No WhatsApp = Yes means the check-in is recorded but no WhatsApp message is sent for this person. "
                     "PINs are not part of the upload - set them from the Attendance owner dashboard. Rows marked EXAMPLE are ignored.")


def validate_att_employees(rows: list[dict]) -> dict:
    existing = _employees()
    seen_rows: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in ATT_EMPLOYEE_REQUIRED if not g(c)]
        name = " ".join(g("Employee Name").split())
        if len(name) > 100:
            errs.append("Employee Name is longer than 100 characters")
        if len(g("Designation")) > 100:
            errs.append("Designation is longer than 100 characters")
        if name:
            if name.lower() in existing:
                errs.append(f"{name} is already an employee")
            elif name.lower() in seen_rows:
                errs.append(f"Duplicate of row {seen_rows[name.lower()]} in this file")
            else:
                seen_rows[name.lower()] = r.get("_row")
        salary = _money(g, "Monthly Salary", errs, positive=True)
        n = {"name": name, "role": g("Designation") or None, "salary": salary, "skip_wa": _yes_no(g, "No WhatsApp", errs)}
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_att_employees(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO dim_att_employee (employee_name, role_title, skip_whatsapp, monthly_salary, salary_set_at, "
                    "salary_set_by_user_key, import_batch_id) VALUES (%s, %s, %s, %s, CASE WHEN %s::numeric IS NULL THEN NULL ELSE now() END, "
                    "CASE WHEN %s::numeric IS NULL THEN NULL ELSE %s END, %s)",
                    (n["name"], n["role"], n["skip_wa"], n["salary"], n["salary"], n["salary"], admin["user_key"], batch_id))


# ------------------------------------------------------------------ attendance: daily records
ATT_RECORD_COLUMNS = ["Employee Name", "Date", "Check In", "Check Out", "Location", "Status"]
ATT_RECORD_REQUIRED = ["Employee Name", "Date", "Check In"]
ATT_RECORD_EXAMPLES = [["EXAMPLE Ramesh Kumar", "15-09-2026", "09:05", "18:30", "Godown", ""],
                       ["EXAMPLE Sunita", "15-09-2026", "10:20", "", "Shop", "Late"]]
ATT_RECORD_HELP = ("One employee-day per row, for attendance that happened before the portal (or was missed). Employee Name (must exist - "
                   "import or add the employees first), Date (dd-mm-yyyy) and Check In (24-hour hh:mm, like 09:05) are required. Check Out "
                   "is optional; with no Check Out the day shows as Incomplete. Location must be one of the places under Setup > Dropdown "
                   "values > Attendance - work places (optional). Status is optional: leave it empty and it is worked out from the times with "
                   "the same rules as live check-ins (Setup > Module settings); or write Present, Late, Half Day or Absent. A day that "
                   "already has attendance for that employee is rejected. No WhatsApp message is sent for imported days. "
                   "Rows marked EXAMPLE are ignored.")


def att_record_lists() -> dict:
    return {"Location": _list_names("dim_att_location", "location_key", "location_name"),
            "Status": ["Present", "Late", "Half Day", "Absent"]}


def validate_att_records(rows: list[dict]) -> dict:
    from . import attendance
    emps = _employees()
    places = _names("dim_att_location", "location_key", "location_name")
    rules = attendance.Rules()
    with db.cursor() as cur:
        cur.execute("SELECT employee_key, work_date FROM fact_attendance")
        existing = {(r["employee_key"], r["work_date"]) for r in cur.fetchall()}
    today = mc.today_ist()
    seen_rows: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in ATT_RECORD_REQUIRED if not g(c)]
        n: dict = {}
        if g("Employee Name"):
            k, e = _pick(emps, g("Employee Name"), "Employee Name")
            if e:
                errs.append(e)
            else:
                n["employee_key"] = k
        seen: list = []
        day = _date_field(g, "Date", seen, errs)
        if day and day > today:
            errs.append("Date is in the future")
        t_in = _time_field(g, "Check In", errs)
        t_out = _time_field(g, "Check Out", errs)
        if t_in and t_out and t_out <= t_in:
            errs.append("Check Out must be after Check In")
        n["place_key"] = None
        if g("Location"):
            k, e = _pick(places, g("Location"), "Location")
            if e:
                errs.append(e)
            n["place_key"] = k
        status = g("Status").title() if g("Status") else ""
        if status and status not in ("Present", "Late", "Half Day", "Absent"):
            errs.append("Status must be Present, Late, Half Day or Absent")
        if t_in and not status:
            i, o = attendance._mins(t_in), attendance._mins(t_out)
            status = rules.final_status(i, o) if o is not None else "Incomplete"
        n.update(day=day, t_in=t_in, t_out=t_out, status=status or None)
        n["minutes"] = attendance._hours(attendance._mins(t_in), attendance._mins(t_out))[1] if t_in and t_out else None
        if n.get("employee_key") and day:
            dup = (n["employee_key"], day)
            if dup in existing:
                errs.append(f"{g('Employee Name')} already has attendance on {day:%d-%m-%Y}")
            elif dup in seen_rows:
                errs.append(f"Duplicate of row {seen_rows[dup]} in this file (same employee and date)")
            else:
                seen_rows[dup] = r.get("_row")
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_att_records(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time, location_key, check_out_time, checkout_source, "
                    "hours_worked_min, final_status, wa_status, import_batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Done',%s)",
                    (n["employee_key"], n["day"], n["t_in"], n["place_key"], n["t_out"], "Manual (Owner)" if n["t_out"] else None,
                     n["minutes"], n["status"], batch_id))


# ------------------------------------------------------------------ attendance: advances
ATT_ADVANCE_COLUMNS = ["Employee Name", "Date", "Amount", "Note"]
ATT_ADVANCE_REQUIRED = ["Employee Name", "Date", "Amount"]
ATT_ADVANCE_EXAMPLES = [["EXAMPLE Ramesh Kumar", "05-09-2026", "5000", "Advance for festival"]]
ATT_ADVANCE_HELP = ("One salary advance per row. Employee Name (must exist), Date (dd-mm-yyyy) and Amount (rupees, above 0) are required. "
                    "Advances are deducted in that month's salary report. Rows marked EXAMPLE are ignored.")


def validate_att_advances(rows: list[dict]) -> dict:
    emps = _employees()
    today = mc.today_ist()
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in ATT_ADVANCE_REQUIRED if not g(c)]
        n: dict = {}
        if g("Employee Name"):
            k, e = _pick(emps, g("Employee Name"), "Employee Name")
            if e:
                errs.append(e)
            else:
                n["employee_key"] = k
        seen: list = []
        n["day"] = _date_field(g, "Date", seen, errs)
        if n["day"] and n["day"] > today:
            errs.append("Date is in the future")
        n["amount"] = _money(g, "Amount", errs, positive=True)
        n["note"] = g("Note") or None
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_att_advances(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        cur.execute("INSERT INTO fact_att_advance (employee_key, advance_date, amount, note, added_by_user_key, import_batch_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (n["employee_key"], n["day"], n["amount"], n["note"], admin["user_key"], batch_id))


ENTITIES["parties"] = {"label": "payment parties", "columns": PARTY_COLUMNS, "required": PARTY_REQUIRED, "examples": PARTY_EXAMPLES,
                       "help": PARTY_HELP, "validate": validate_parties, "insert": insert_parties, "lists": lambda: {},
                       "table": "dim_party", "edited": "EXISTS (SELECT 1 FROM fact_payment_txn x WHERE x.party_key = dim_party.party_key)"}
ENTITIES.update({
    "decor_clients": {"label": "VT Decor clients", "columns": DECOR_CLIENT_COLUMNS, "required": DECOR_CLIENT_REQUIRED,
                      "examples": DECOR_CLIENT_EXAMPLES, "help": DECOR_CLIENT_HELP, "validate": validate_decor_clients,
                      "insert": insert_decor_clients, "lists": decor_client_lists, "table": "dim_decor_client", "aliases": DECOR_CLIENT_ALIASES,
                      "edited": "EXISTS (SELECT 1 FROM fact_decor_payment x WHERE x.client_key = dim_decor_client.client_key) "
                                "OR EXISTS (SELECT 1 FROM fact_decor_expense x WHERE x.client_key = dim_decor_client.client_key) "
                                "OR EXISTS (SELECT 1 FROM fact_decor_item x WHERE x.client_key = dim_decor_client.client_key) "
                                "OR EXISTS (SELECT 1 FROM fact_decor_photo x WHERE x.client_key = dim_decor_client.client_key) "
                                "OR EXISTS (SELECT 1 FROM fact_decor_revision x WHERE x.client_key = dim_decor_client.client_key)"},
    "decor_payments": {"label": "VT Decor payments", "columns": DECOR_PAYMENT_COLUMNS, "required": DECOR_PAYMENT_REQUIRED,
                       "examples": DECOR_PAYMENT_EXAMPLES, "help": DECOR_PAYMENT_HELP, "validate": validate_decor_payments,
                       "insert": insert_decor_payments, "lists": decor_payment_lists, "table": "fact_decor_payment", "edited": "FALSE",
                       "aliases": DECOR_PAYMENT_ALIASES},
    "decor_expenses": {"label": "VT Decor expenses", "columns": DECOR_EXPENSE_COLUMNS, "required": DECOR_EXPENSE_REQUIRED,
                       "examples": DECOR_EXPENSE_EXAMPLES, "help": DECOR_EXPENSE_HELP, "validate": validate_decor_expenses,
                       "insert": insert_decor_expenses, "lists": decor_expense_lists, "table": "fact_decor_expense", "edited": "FALSE",
                       "aliases": DECOR_EXPENSE_ALIASES},
    "decor_items": {"label": "VT Decor items", "columns": DECOR_ITEM_COLUMNS, "required": DECOR_ITEM_REQUIRED,
                    "examples": DECOR_ITEM_EXAMPLES, "help": DECOR_ITEM_HELP, "validate": validate_decor_items,
                    "insert": insert_decor_items, "lists": lambda: {}, "table": "fact_decor_item", "edited": "FALSE",
                    "aliases": DECOR_ITEM_ALIASES},
    "decor_photos": {"label": "VT Decor photos and challans", "columns": DECOR_PHOTO_COLUMNS, "required": DECOR_PHOTO_REQUIRED,
                     "examples": DECOR_PHOTO_EXAMPLES, "help": DECOR_PHOTO_HELP, "validate": validate_decor_photos,
                     "insert": insert_decor_photos, "lists": lambda: {}, "table": "fact_decor_photo", "edited": "FALSE",
                     "aliases": DECOR_PHOTO_ALIASES},
    "decor_quotations": {"label": "VT Decor quotations", "columns": DECOR_QUOTE_COLUMNS, "required": DECOR_QUOTE_REQUIRED,
                         "examples": DECOR_QUOTE_EXAMPLES, "help": DECOR_QUOTE_HELP, "validate": validate_decor_quotations,
                         "insert": insert_decor_quotations, "lists": lambda: {}, "table": "fact_decor_quotation", "edited": "FALSE",
                         "aliases": DECOR_QUOTE_ALIASES},
    "att_employees": {"label": "attendance employees", "columns": ATT_EMPLOYEE_COLUMNS, "required": ATT_EMPLOYEE_REQUIRED,
                      "examples": ATT_EMPLOYEE_EXAMPLES, "help": ATT_EMPLOYEE_HELP, "validate": validate_att_employees,
                      "insert": insert_att_employees, "lists": lambda: {}, "table": "dim_att_employee",
                      "edited": "pin_hash IS NOT NULL OR EXISTS (SELECT 1 FROM fact_attendance x WHERE x.employee_key = dim_att_employee.employee_key) "
                                "OR EXISTS (SELECT 1 FROM fact_att_advance x WHERE x.employee_key = dim_att_employee.employee_key)"},
    "att_attendance": {"label": "attendance records", "columns": ATT_RECORD_COLUMNS, "required": ATT_RECORD_REQUIRED,
                       "examples": ATT_RECORD_EXAMPLES, "help": ATT_RECORD_HELP, "validate": validate_att_records,
                       "insert": insert_att_records, "lists": att_record_lists, "table": "fact_attendance",
                       "edited": "updated_at IS NOT NULL"},
    "att_advances": {"label": "salary advances", "columns": ATT_ADVANCE_COLUMNS, "required": ATT_ADVANCE_REQUIRED,
                     "examples": ATT_ADVANCE_EXAMPLES, "help": ATT_ADVANCE_HELP, "validate": validate_att_advances,
                     "insert": insert_att_advances, "lists": lambda: {}, "table": "fact_att_advance", "edited": "FALSE"},
})


# ================================================================== Checklist
CHECKLIST_COLUMNS = ["Title", "Description", "Category", "Frequency", "Day", "Month", "Start Date", "End Date", "Assigned To"]
CHECKLIST_REQUIRED = ["Title", "Frequency", "Assigned To"]
CHECKLIST_EXAMPLES = [
    ["EXAMPLE File GSTR-3B return", "Upload before 20th", "GST", "Monthly", "20", "", "", "", "example.ravi"],
    ["EXAMPLE Open shop and switch on lights", "", "Shop / Godown routine", "Daily", "", "", "", "", "example.ravi;example.priya"],
    ["EXAMPLE Renew trade licence", "", "Licences & Renewals", "Yearly", "31", "March", "", "", "example.ravi"]]
CHECKLIST_HELP = ("One checklist per row; it comes back on its own schedule and the assigned person marks each time Done or Not Done. "
                  "Title, Frequency and Assigned To are required. Frequency: Daily (not on the weekly off day), Weekly, Monthly, "
                  "Quarterly, Yearly or Once. Day: for Weekly the day name (Mon ... Sun); for Monthly, Quarterly and Yearly the date "
                  "of the month (1 to 31, or Last for the last day). Month: for Yearly the month; for Quarterly the first month the "
                  "quarters count from (Jan, Feb ... or 1 to 12). Start Date (dd-mm-yyyy, today if empty, not in the past) and End Date "
                  "are optional; a Once checklist happens on its Start Date. Assigned To: staff usernames (as shown under Setup > "
                  "Members) - separate several with ; and each gets their own copy. The person needs the 'Checklist - My checklist' page "
                  "in Setup > Access. Category must exist under Setup > Dropdown values > Checklist - categories (optional). "
                  "Rows marked EXAMPLE are ignored.")
_WEEKDAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def checklist_lists() -> dict:
    return {"Frequency": ["Daily", "Weekly", "Monthly", "Quarterly", "Yearly", "Once"],
            "Category": _list_names("dim_checklist_category", "category_key", "category_name"),
            "Day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Last"],
            "Month": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]}


def validate_checklists(rows: list[dict]) -> dict:
    from fastapi import HTTPException
    from . import checklist
    cats = _names("dim_checklist_category", "category_key", "category_name")
    with db.cursor() as cur:
        people = {u["id"].lower(): u for u in checklist.assignable_users(cur)}
        cur.execute("SELECT lower(btrim(c.title)) AS t, c.assignee_user_key, c.frequency FROM dim_checklist c WHERE c.is_active")
        existing = {(r["t"], r["assignee_user_key"], r["frequency"]) for r in cur.fetchall()}
    cal = _calendar(parse_date((r.get(c) or "").strip()) for r in rows for c in ("Start Date", "End Date"))
    today = mc.today_ist()
    seen_rows: dict = {}
    results = []
    for r in rows:
        g = _row_getter(r)
        errs: list[str] = []
        errs += [f"{c} is required" for c in CHECKLIST_REQUIRED if not g(c)]
        n: dict = {"title": " ".join(g("Title").split()), "description": g("Description")[:1000] or None, "category_key": None}
        if len(n["title"]) > 200:
            errs.append("Title is longer than 200 characters")
        if g("Category"):
            k, e = _pick(cats, g("Category"), "Category")
            if e:
                errs.append(e)
            n["category_key"] = k
        freq = g("Frequency").strip().title()
        dow = day = month = None
        raw_day, raw_month = g("Day").strip().upper(), g("Month").strip().upper()
        if raw_day:
            if raw_day[:3] in _WEEKDAYS and not raw_day.isdigit():
                dow = _WEEKDAYS[raw_day[:3]]
            elif raw_day in ("LAST", "L"):
                day = 31
            elif raw_day.isdigit():
                day = int(raw_day)
            else:
                errs.append(f"Day '{g('Day')}' is not a weekday (Mon ... Sun), a date (1 to 31) or Last")
        if raw_month:
            if raw_month[:3] in _MONTHS and not raw_month.isdigit():
                month = _MONTHS[raw_month[:3]]
            elif raw_month.isdigit() and 1 <= int(raw_month) <= 12:
                month = int(raw_month)
            else:
                errs.append(f"Month '{g('Month')}' is not a month (Jan ... Dec or 1 to 12)")
        if freq and freq not in checklist.FREQUENCIES:
            errs.append("Frequency must be one of " + ", ".join(checklist.FREQUENCIES))
        elif freq:
            try:
                dow, day, month = checklist.check_schedule(freq, dow, day, month)
            except HTTPException as exc:
                errs.append(str(exc.detail))
        seen: list = []
        n["start"] = _date_field(g, "Start Date", seen, errs) or today
        n["end"] = _date_field(g, "End Date", seen, errs)
        if g("Start Date") and n["start"] < today:
            errs.append("Start Date cannot be in the past")
        if n["end"] and n["end"] < n["start"]:
            errs.append("End Date is before the Start Date")
        if freq == "Once":
            n["end"] = None
        _calendar_errors(seen, cal, errs)
        assignees = []
        for name in [p.strip() for p in g("Assigned To").replace("|", ";").split(";") if p.strip()]:
            u = people.get(name.lower())
            if not u:
                errs.append(f"Assigned To '{name}' not found, or has no 'Checklist - My checklist' page")
            elif u["user_key"] not in assignees:
                assignees.append(u["user_key"])
                if (n["title"].lower(), u["user_key"], freq) in existing:
                    errs.append(f"{name} already has an active {freq} checklist called '{n['title']}'")
                dup = (n["title"].lower(), u["user_key"], freq)
                if dup in seen_rows:
                    errs.append(f"Duplicate of row {seen_rows[dup]} in this file ({name})")
                else:
                    seen_rows[dup] = r.get("_row")
        n.update(frequency=freq, dow=dow, day=day, month=month, assignees=assignees)
        results.append((r, "; ".join(errs) or None, None if errs else n))
    return _report(rows, results)


def insert_checklists(cur, normalised: list[dict], batch_id: int, admin: dict) -> None:
    for n in normalised:
        for user_key in n["assignees"]:
            cur.execute("INSERT INTO dim_checklist (title, description, category_key, assignee_user_key, frequency, due_dow, due_day, due_month, "
                        "start_date_key, end_date_key, created_by_user_key, import_batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (n["title"], n["description"], n["category_key"], user_key, n["frequency"], n["dow"], n["day"], n["month"],
                         mc.date_key(n["start"]), mc.date_key(n["end"]), admin["user_key"], batch_id))


ENTITIES["checklists"] = {
    "label": "checklists", "columns": CHECKLIST_COLUMNS, "required": CHECKLIST_REQUIRED, "examples": CHECKLIST_EXAMPLES,
    "help": CHECKLIST_HELP, "validate": validate_checklists, "insert": insert_checklists, "lists": checklist_lists,
    "table": "dim_checklist",
    # a checklist someone has already marked an item on is history: undoing the upload is refused rather than erasing it
    "edited": "EXISTS (SELECT 1 FROM fact_checklist_occurrence x WHERE x.checklist_key = dim_checklist.checklist_key AND x.status <> 'Pending')"
              " OR updated_at IS NOT NULL"}
