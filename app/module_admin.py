"""Admin side of the Payments, Delegation and Purchase modules:
  - the dropdown lists they use (companies, payment modes, transaction types, parties, vendors) - add, rename, set the flags
    that drive the calculations, delete when unused (merging look-alikes goes through Setup > Dropdown values like every list);
  - the settings that used to be constants in the scripts (delegation scoring rules, first day of the week);
  - the KPI blocks the admin overview shows for each module.
"""
import re
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict

from . import access, config, db, modcommon as mc
from .admin import ADMIN, audit
from .reconcile import _references

router = APIRouter(prefix="/admin", tags=["module-admin"])

YES_NO = [(True, "Yes"), (False, "No")]


def _options(cur, table: str, key: str, name: str, blank: str | None = None) -> list[list]:
    cur.execute(f'SELECT "{key}" AS k, "{name}" AS n FROM "{table}" ORDER BY sort_order, lower("{name}")')
    return ([[None, blank]] if blank else []) + [[r["k"], r["n"]] for r in cur.fetchall()]


# id -> definition. Column names are fixed here (never taken from the request), so they are safe to put in SQL.
MASTERS: dict[str, dict[str, Any]] = {
    "company": {"label": "Companies (payments)", "table": "dim_company", "key": "company_key", "name": "company_name",
                "kind": "company", "role": None, "where": "TRUE", "defaults": {},
                "fields": [{"name": "is_sales_company", "label": "Its cash sales count in the Cash Sales card", "type": "bool"}]},
    "payment_mode": {"label": "Payment modes", "table": "dim_payment_mode", "key": "mode_key", "name": "mode_name",
                     "kind": "payment_mode", "role": None, "where": "TRUE", "defaults": {},
                     "fields": [{"name": "is_cash", "label": "Counts as cash in hand", "type": "bool"},
                                {"name": "needs_approval", "label": "A cashier's entry waits for approval", "type": "bool"}
                                ]},
    "txn_type": {"label": "Payment transaction types", "table": "dim_txn_type", "key": "txn_type_key", "name": "type_name",
                 "kind": "txn_type", "role": None, "where": "TRUE", "defaults": {},
                 "fields": [{"name": "direction", "label": "Money", "type": "select", "options": [["in", "In (received)"], ["out", "Out (paid)"]]},
                            {"name": "affects_bank", "label": "Moves the bank balance", "type": "bool"},
                            {"name": "sales_effect", "label": "Cash sales", "type": "select",
                             "options": [[1, "Adds to cash sales"], [0, "No effect"], [-1, "Takes away (return)"]]},
                            {"name": "entry_allowed", "label": "Offered in the New Entry form", "type": "bool"},
                            {"name": "counts_as_received", "label": "Adds to Total Received", "type": "bool"},
                            {"name": "counts_as_paid", "label": "Adds to Total Paid", "type": "bool"}
                            ]},
    "party": {"label": "Payment parties", "table": "dim_party", "key": "party_key", "name": "party_name", "kind": "party",
              "role": "payment", "where": "party_kind = 'payment'", "defaults": {"party_kind": "payment"},
              "fields": [{"name": "company_key", "label": "Company", "type": "select", "source": "company", "required": True},
                         {"name": "txn_type_key", "label": "Offered for", "type": "select", "source": "txn_type", "blank": "Every type"}]},
    "decor_type": {"label": "VT Decor - product types", "table": "dim_decor_product_type", "key": "type_key", "name": "type_name",
                   "kind": "decor_type", "role": None, "where": "TRUE", "defaults": {},
                   "fields": [{"name": "percent_stages", "label": "Each stage brings a share of the contract (like Wallpaper)", "type": "bool"}]},
    "decor_stage": {"label": "VT Decor - stages of each product type", "table": "dim_decor_stage", "key": "stage_key", "name": "stage_name",
                    "kind": "decor_stage", "role": None, "where": "TRUE", "defaults": {},
                    "fields": [{"name": "product_type_key", "label": "Product type", "type": "select", "source": "decor_type", "required": True},
                               {"name": "target_pct", "label": "Share of the contract, % (only for types that split by percentage)", "type": "number"}]},
    "decor_expense_type": {"label": "VT Decor - expense types (suggestions)", "table": "dim_decor_expense_type", "key": "expense_type_key",
                           "name": "expense_type_name", "kind": "decor_expense_type", "role": None, "where": "TRUE", "defaults": {}, "fields": []},
    "att_location": {"label": "Attendance - work places (GPS spot)", "table": "dim_att_location", "key": "location_key", "name": "location_name",
                     "kind": "att_location", "role": None, "where": "TRUE", "defaults": {},
                     "fields": [{"name": "latitude", "label": "Latitude (e.g. 28.7004323)", "type": "decimal", "min": -90, "max": 90, "required": True},
                                {"name": "longitude", "label": "Longitude (e.g. 77.1288457)", "type": "decimal", "min": -180, "max": 180, "required": True},
                                {"name": "radius_m", "label": "Allowed distance in metres (check-in works within this)", "type": "number", "min": 5, "max": 5000},
                                {"name": "icon", "label": "Icon shown in the WhatsApp message", "type": "text", "max": 8}]},
    "att_employee": {"label": "Attendance - employees", "table": "dim_att_employee", "key": "employee_key", "name": "employee_name",
                     "kind": "att_employee", "role": None, "where": "TRUE", "defaults": {},
                     "fields": [{"name": "role_title", "label": "Designation", "type": "text", "max": 100},
                                {"name": "is_active", "label": "Active (shown on the check-in page)", "type": "bool"},
                                {"name": "skip_whatsapp", "label": "No WhatsApp message for this person's check-in / check-out", "type": "bool"}]},
    "vendor": {"label": "Vendors (purchase)", "table": "dim_party", "key": "party_key", "name": "party_name", "kind": "party",
               "role": "vendor", "where": "party_kind = 'vendor'", "defaults": {"party_kind": "vendor"}, "fields": []},
}
SOURCES = {"company": ("dim_company", "company_key", "company_name"), "txn_type": ("dim_txn_type", "txn_type_key", "type_name"),
           "decor_type": ("dim_decor_product_type", "type_key", "type_name")}


def _spec(master: str) -> dict:
    spec = MASTERS.get(master)
    if not spec:
        raise HTTPException(404, "Unknown list")
    return spec


def _fields_with_options(cur, spec) -> list[dict]:
    out = []
    for f in spec["fields"]:
        f = dict(f)
        if f.get("source"):
            f["options"] = _options(cur, *SOURCES[f["source"]], blank=f.get("blank"))
        out.append(f)
    return out


REVIEWED = {"company", "party", "vendor"}   # lists a module screen can add to (the others are only ever set up in Setup)


def _pending(cur, master: str) -> int:
    if master not in REVIEWED:
        return 0
    spec = MASTERS[master]
    cur.execute(f"SELECT count(*) AS n FROM \"{spec['table']}\" WHERE review_status = 'pending' AND {spec['where']}")
    return cur.fetchone()["n"]


@router.get("/masters")
def masters(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        return [{"id": mid, "label": s["label"], "kind": s["kind"], "role": s["role"], "fields": _fields_with_options(cur, s),
                 "reviewed": mid in REVIEWED, "pending": _pending(cur, mid)}
                for mid, s in MASTERS.items()]


@router.get("/masters-pending")
def masters_pending(admin=Depends(ADMIN)):
    """How many values typed on the module screens still wait for review (the badge on Setup > Dropdown values)."""
    with db.cursor() as cur:
        by = {mid: _pending(cur, mid) for mid in REVIEWED}
    return {"total": sum(by.values()), "by": by}


@router.get("/masters/{master}")
def master_rows(master: str, admin=Depends(ADMIN)):
    spec = _spec(master)
    cols = ", ".join(f't."{f["name"]}"' for f in spec["fields"])
    reviewed = master in REVIEWED
    review = ", t.review_status, t.created_at, cu.display_name AS created_by" if reviewed else ""
    join = "LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key" if reviewed else ""
    with db.cursor() as cur:
        cur.execute(f'SELECT t."{spec["key"]}" AS key, t."{spec["name"]}" AS name{", " + cols if cols else ""}{review} '
                    f'FROM "{spec["table"]}" t {join} WHERE {spec["where"]} ORDER BY (t.review_status = \'pending\') DESC, lower(t."{spec["name"]}")'
                    if reviewed else
                    f'SELECT t."{spec["key"]}" AS key, t."{spec["name"]}" AS name{", " + cols if cols else ""} FROM "{spec["table"]}" t '
                    f'WHERE {spec["where"]} ORDER BY t.sort_order, lower(t."{spec["name"]}")')
        rows = cur.fetchall()
        used: dict[int, int] = {}
        for tbl, col in _references(cur, spec["table"]):
            cur.execute(f'SELECT "{col}" AS k, count(*) AS n FROM "{tbl}" WHERE "{col}" IS NOT NULL GROUP BY 1')
            for r in cur.fetchall():
                used[r["k"]] = used.get(r["k"], 0) + r["n"]
        fields = _fields_with_options(cur, spec)
    for r in rows:
        r["used"] = used.get(r["key"], 0)
    return {"id": master, "label": spec["label"], "fields": fields, "rows": rows}


class MasterIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str


def _values(spec, body: MasterIn, cur) -> dict:
    """Name + the flags of this list, checked against what the list allows."""
    name = " ".join(body.name.split())
    if not name or len(name) > 150:
        raise HTTPException(422, "Name must be 1 to 150 characters.")
    vals: dict[str, Any] = {spec["name"]: name}
    extra = body.model_extra or {}
    for f in spec["fields"]:
        if f["name"] not in extra:
            continue
        v = extra[f["name"]]
        if f["type"] == "bool":
            v = bool(v)
        elif f["type"] == "number":
            try:
                v = int(v or 0)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{f['label']} must be a number.")
            if not f.get("min", v) <= v <= f.get("max", v):
                raise HTTPException(422, f"{f['label']} must be between {f['min']} and {f['max']}.")
        elif f["type"] == "decimal":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{f['label']} must be a number.")
            if not f.get("min", v) <= v <= f.get("max", v):
                raise HTTPException(422, f"{f['label']} must be between {f['min']} and {f['max']}.")
        elif f["type"] == "text":
            v = " ".join(str(v or "").split())[: f.get("max", 100)] or None
        elif f["type"] == "select":
            allowed = {o[0] for o in (_options(cur, *SOURCES[f["source"]], blank=f.get("blank")) if f.get("source") else f["options"])}
            v = None if v in ("", None) else v
            if v not in allowed and not (v is None and f.get("blank")):
                raise HTTPException(422, f"Choose a valid value for {f['label']}.")
            if v is None and f.get("required"):
                raise HTTPException(422, f"{f['label']} is required.")
        vals[f["name"]] = v
    for f in spec["fields"]:   # a required choice must be present on create
        if f.get("required") and vals.get(f["name"]) is None and f["name"] not in vals:
            raise HTTPException(422, f"{f['label']} is required.")
    return vals


@router.post("/masters/{master}", status_code=201)
def master_add(master: str, body: MasterIn, admin=Depends(ADMIN)):
    spec = _spec(master)
    try:
        with db.cursor() as cur:
            vals = {**spec["defaults"], **_values(spec, body, cur)}
            cur.execute(f'INSERT INTO "{spec["table"]}" ({", ".join(chr(34) + c + chr(34) for c in vals)}) '
                        f'VALUES ({", ".join(["%s"] * len(vals))}) RETURNING "{spec["key"]}" AS key', list(vals.values()))
            key = cur.fetchone()["key"]
            audit(cur, admin, f"master.add.{master}", vals[spec["name"]])
    except pgerr.UniqueViolation:
        raise HTTPException(409, "That value already exists (case/spacing-insensitive).")
    return {"key": key}


@router.put("/masters/{master}/{key}")
def master_edit(master: str, key: int, body: MasterIn, admin=Depends(ADMIN)):
    spec = _spec(master)
    try:
        with db.cursor() as cur:
            vals = _values(spec, body, cur)
            sets = ", ".join(f'"{c}" = %s' for c in vals)
            if master in REVIEWED:
                sets += ", review_status = 'approved'"
            cur.execute(f'UPDATE "{spec["table"]}" SET {sets} WHERE "{spec["key"]}" = %s AND {spec["where"]} RETURNING "{spec["key"]}"',
                        list(vals.values()) + [key])
            if not cur.fetchone():
                raise HTTPException(404, "Value not found")
            audit(cur, admin, f"master.edit.{master}", str(key), {k: v for k, v in vals.items()})
    except pgerr.UniqueViolation:
        raise HTTPException(409, "That value already exists (case/spacing-insensitive). Use Merge to combine them.")
    return {"ok": True}


@router.post("/masters/{master}/{key}/approve")
def master_approve(master: str, key: int, admin=Depends(ADMIN)):
    """Review done: keep the value as it is. (To fix a spelling, edit it; to fold it into another value, merge it.)"""
    spec = _spec(master)
    if master not in REVIEWED:
        raise HTTPException(422, "This list has nothing to review.")
    with db.cursor() as cur:
        cur.execute(f"UPDATE \"{spec['table']}\" SET review_status = 'approved' WHERE \"{spec['key']}\" = %s AND {spec['where']} "
                    f"RETURNING \"{spec['name']}\" AS name", (key,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Value not found")
        audit(cur, admin, f"master.approve.{master}", row["name"])
    return {"ok": True}


@router.post("/masters/{master}/approve-all")
def master_approve_all(master: str, admin=Depends(ADMIN)):
    spec = _spec(master)
    if master not in REVIEWED:
        raise HTTPException(422, "This list has nothing to review.")
    with db.cursor() as cur:
        cur.execute(f"UPDATE \"{spec['table']}\" SET review_status = 'approved' WHERE review_status = 'pending' AND {spec['where']}")
        n = cur.rowcount
        audit(cur, admin, f"master.approve_all.{master}", str(n))
    return {"ok": True, "approved": n}


@router.delete("/masters/{master}/{key}")
def master_delete(master: str, key: int, admin=Depends(ADMIN)):
    spec = _spec(master)
    with db.cursor() as cur:
        refs = 0
        for tbl, col in _references(cur, spec["table"]):
            cur.execute(f'SELECT count(*) AS n FROM "{tbl}" WHERE "{col}" = %s', (key,))
            refs += cur.fetchone()["n"]
        if refs:
            raise HTTPException(409, f"Can't delete: {refs} record(s) use this value. Merge it into another value instead.")
        cur.execute(f'DELETE FROM "{spec["table"]}" WHERE "{spec["key"]}" = %s AND {spec["where"]} RETURNING "{spec["name"]}" AS name', (key,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Value not found")
        audit(cur, admin, f"master.delete.{master}", row["name"])
    return {"ok": True}


# ------------------------------------------------------------------ module settings
SETTINGS = [
    {"key": "deleg_on_time_score", "group": "Delegation", "label": "Points for a task done on time", "default": 10, "min": 0, "max": 1000},
    {"key": "deleg_late_score", "group": "Delegation", "label": "Points for a task done late (usually negative)", "default": -5, "min": -1000, "max": 1000},
    {"key": "deleg_revise_penalty", "group": "Delegation", "label": "Points taken each time a manager sends a task back", "default": -3, "min": -1000, "max": 0},
    {"key": "deleg_extend_penalty", "group": "Delegation", "label": "Points taken each time a missed deadline is moved", "default": -5, "min": -1000, "max": 0},
    {"key": "deleg_week_start_dow", "group": "Delegation", "label": "First day of the working week (0 = Sunday ... 6 = Saturday)", "default": 6, "min": 0, "max": 6},
    {"key": "att_late_after", "group": "Attendance", "type": "time", "default": "10:30", "label": "Check-in at or after this time counts as Late"},
    {"key": "att_half_day_checkin", "group": "Attendance", "type": "time", "default": "14:00", "label": "Check-in at or after this time counts as Half Day"},
    {"key": "att_absent_before", "group": "Attendance", "type": "time", "default": "14:00", "label": "Check-out before this time makes the day Absent"},
    {"key": "att_half_day_before", "group": "Attendance", "type": "time", "default": "17:00", "label": "Check-out before this time (but after the one above) makes it a Half Day"},
    {"key": "att_full_day_until", "group": "Attendance", "type": "time", "default": "20:00", "label": "Check-out up to this time is a normal day (later is also a normal day)"},
    {"key": "att_auto_checkout_time", "group": "Attendance", "type": "time", "default": "20:00", "label": "Time recorded for someone who forgot to check out"},
    {"key": "att_auto_run_time", "group": "Attendance", "type": "time", "default": "21:00", "label": "The auto check-out is done each day after this time"},
    {"key": "att_weekly_off_day", "group": "Attendance", "default": 1, "min": 0, "max": 6, "label": "Weekly off day (0 = Sunday ... 6 = Saturday; paid, not counted absent)"},
    {"key": "att_salary_base_days", "group": "Attendance", "default": 30, "min": 20, "max": 31, "label": "Days a month's salary is divided by"},
    {"key": "att_employee_link_on", "group": "Attendance", "default": 1, "min": 0, "max": 1, "label": "Check-in page open to anyone with the link (1) or only to people signed in to the portal (0)"},
    {"key": "att_pin_max_attempts", "group": "Attendance", "default": 5, "min": 3, "max": 20, "label": "Wrong salary PINs allowed before the PIN is locked"},
    {"key": "att_pin_lock_minutes", "group": "Attendance", "default": 15, "min": 1, "max": 1440, "label": "Minutes a locked PIN stays locked"},
    {"key": "pay_merge_req_company", "group": "Payments - from O2D Cashier", "default": 1, "min": 0, "max": 1,
     "label": "Company must be filled before a Sales / Sales Return row is merged (1 = required, 0 = optional)"},
    {"key": "pay_merge_req_party", "group": "Payments - from O2D Cashier", "default": 0, "min": 0, "max": 1,
     "label": "Party must be filled before a row is merged (1 = required, 0 = optional)"},
    {"key": "pay_merge_req_remarks", "group": "Payments - from O2D Cashier", "default": 0, "min": 0, "max": 1,
     "label": "Remarks must be filled before a row is merged (1 = required, 0 = optional)"},
    {"key": "pay_merge_default_company", "group": "Payments - from O2D Cashier", "type": "text", "max": 100, "default": "",
     "label": "Company filled in automatically on every new row (a company name from the Payments list; blank = none)"},
    {"key": "pay_merge_sales_type", "group": "Payments - from O2D Cashier", "type": "text", "max": 50, "default": "Sales",
     "label": "Payments transaction type a Sales row is merged as"},
    {"key": "pay_merge_return_type", "group": "Payments - from O2D Cashier", "type": "text", "max": 50, "default": "Sale Return",
     "label": "Payments transaction type a Sales Return row is merged as"},
    {"key": "pay_merge_cash_mode", "group": "Payments - from O2D Cashier", "type": "text", "max": 50, "default": "Cash",
     "label": "Payment mode used for the Cash part"},
    {"key": "pay_merge_bank_mode", "group": "Payments - from O2D Cashier", "type": "text", "max": 50, "default": "Bank",
     "label": "Payment mode used for the Bank part"},
    {"key": "decor_statement_note", "group": "VT Decor", "type": "text", "max": 300,
     "default": "Computer-generated statement. UPI: 9811354356 | Bank: City Union Bank | A/C: 512020010025966 | IFSC: CIUB0000244",
     "label": "Line printed at the bottom of a client's PDF statement (e.g. your UPI / bank details)"},
    {"key": "decor_company_address", "group": "VT Decor", "type": "text", "max": 200, "default": "Shop No.134-135 Shiva Market Pitampura, Delhi 110034",
     "label": "Quotation header - address"},
    {"key": "decor_company_contact", "group": "VT Decor", "type": "text", "max": 200,
     "default": "M.NO: 9958179979, 9811354356  |  vt_decorpaints@yahoo.com", "label": "Quotation header - phone and email"},
    {"key": "decor_bank_details", "group": "VT Decor", "type": "textarea", "max": 600,
     "default": "Bank: City Union Bank, Sector-8, Rohini\nA/C No.: 512020010025966\nIFSC: CIUB0000244\nUPI: 9811354356 / E/6ED155H117@CUB",
     "label": "Quotation bank details (one per line, as Label: value)"},
    {"key": "decor_quote_terms", "group": "VT Decor", "type": "textarea", "max": 800,
     "default": "Order once placed, no return, no exchange.\nPlease check product code & quantity before order.\nDelivery within 07 to 15 days.",
     "label": "Quotation terms of sale (one per line)"},
]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def setting_value(s: dict):
    """The stored value of a setting in the type the screen shows it (int, decimal, time or text)."""
    kind = s.get("type", "int")
    raw = config.get(s["key"])
    if raw is None or raw == "":
        return s["default"]
    if kind == "int":
        return mc.int_setting(s["key"], s["default"])
    if kind == "number":
        try:
            return float(raw)
        except ValueError:
            return s["default"]
    return str(raw)


def check_setting(s: dict, value):
    """The value to store (as text) for a setting, or a plain 422 saying what is wrong."""
    kind = s.get("type", "int")
    if kind in ("int", "number"):
        try:
            v = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{s['label']} must be a number.")
        if not s["min"] <= v <= s["max"]:
            raise HTTPException(422, f"{s['label']} must be between {s['min']} and {s['max']}.")
        return str(v)
    v = str(value if value is not None else "").strip()
    if kind == "time" and not TIME_RE.match(v):
        raise HTTPException(422, f"{s['label']} must be a time like 20:00.")
    if kind in ("text", "textarea") and len(v) > s.get("max", 300):
        raise HTTPException(422, f"{s['label']} can be at most {s.get('max', 300)} characters.")
    return v


@router.get("/module-settings")
def get_settings(admin=Depends(ADMIN)):
    return [{**s, "type": s.get("type", "int"), "value": setting_value(s)} for s in SETTINGS]


class SettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: int | float | str


@router.put("/module-settings")
def put_setting(body: SettingIn, admin=Depends(ADMIN)):
    spec = next((s for s in SETTINGS if s["key"] == body.key), None)
    if not spec:
        raise HTTPException(422, "Unknown setting.")
    stored = check_setting(spec, body.value)
    config.set_value(body.key, stored, admin["username"])
    with db.cursor() as cur:
        audit(cur, admin, "module.setting", body.key, {"value": body.value})
    return {"ok": True}


# ------------------------------------------------------------------ KPIs for the admin overview
def _month_start(today: date) -> date:
    return today.replace(day=1)


@router.get("/module-kpis")
def module_kpis(viewer=Depends(access.require_page("dashboard_overview"))):
    """One block per module. Nothing here is computed from a role name or a typed-in company: it all follows the lists."""
    today = mc.today_ist()
    ms, d30 = _month_start(today), today - timedelta(days=29)
    out: dict[str, Any] = {}
    with db.cursor() as cur:
        sign = "CASE WHEN tt.direction = 'in' THEN 1 ELSE -1 END"
        cur.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN m.is_cash AND t.status = 'Approved' THEN {sign} * t.amount END), 0) AS cash,
                   COALESCE(SUM(CASE WHEN NOT m.is_cash AND tt.affects_bank AND t.status = 'Approved' THEN {sign} * t.amount END), 0) AS bank,
                   COALESCE(SUM(CASE WHEN t.status = 'Approved' AND tt.counts_as_received AND d.full_date >= %s THEN t.amount END), 0) AS received,
                   COALESCE(SUM(CASE WHEN t.status = 'Approved' AND tt.counts_as_paid AND d.full_date >= %s THEN t.amount END), 0) AS paid,
                   COALESCE(SUM(CASE WHEN t.status = 'Approved' AND c.is_sales_company AND m.is_cash AND d.full_date >= %s
                                     THEN tt.sales_effect * t.amount END), 0) AS cash_sales,
                   COUNT(*) FILTER (WHERE t.status = 'Pending') AS pending_n,
                   COALESCE(SUM(t.amount) FILTER (WHERE t.status = 'Pending'), 0) AS pending_amt,
                   COUNT(*) FILTER (WHERE d.full_date = %s) AS today_n
            FROM fact_payment_txn t JOIN dim_date d ON d.date_key = t.txn_date_key
            JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key JOIN dim_company c ON c.company_key = t.company_key
            JOIN dim_payment_mode m ON m.mode_key = t.mode_key""", (ms, ms, ms, today))
        p = cur.fetchone()
        cur.execute("""SELECT c.company_name AS name, COALESCE(SUM(CASE WHEN tt.direction = 'in' THEN t.amount ELSE -t.amount END), 0) AS net
                       FROM fact_payment_txn t JOIN dim_date d ON d.date_key = t.txn_date_key
                       JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key JOIN dim_company c ON c.company_key = t.company_key
                       WHERE t.status = 'Approved' AND d.full_date >= %s GROUP BY c.company_name ORDER BY abs(SUM(CASE WHEN tt.direction = 'in'
                       THEN t.amount ELSE -t.amount END)) DESC LIMIT 5""", (ms,))
        by_company = [{"name": r["name"], "net": float(r["net"])} for r in cur.fetchall()]
        out["payments"] = {"cashInHand": float(p["cash"]), "bankBalance": float(p["bank"]), "monthReceived": float(p["received"]),
                           "monthPaid": float(p["paid"]), "monthCashSales": float(p["cash_sales"]), "pendingCount": p["pending_n"],
                           "pendingAmount": float(p["pending_amt"]), "entriesToday": p["today_n"], "monthNetByCompany": by_company}

        cur.execute("""SELECT COUNT(*) FILTER (WHERE t.status = 'Pending') AS open_n,
                              COUNT(*) FILTER (WHERE t.status = 'Pending' AND dd.full_date < %s) AS overdue,
                              COUNT(*) FILTER (WHERE t.status = 'Pending' AND dd.full_date = %s) AS due_today,
                              COUNT(*) FILTER (WHERE t.status = 'Completed By Staff') AS review,
                              COUNT(*) FILTER (WHERE t.score IS NOT NULL AND cd.full_date >= %s) AS done30,
                              COUNT(*) FILTER (WHERE t.score > 0 AND cd.full_date >= %s) AS on_time30
                       FROM fact_task t JOIN dim_date dd ON dd.date_key = t.due_date_key
                       LEFT JOIN dim_date cd ON cd.date_key = t.completed_date_key""", (today, today, d30, d30))
        t = cur.fetchone()
        from .delegation import scoreboard_rows
        top = scoreboard_rows(cur)[:3]
        out["delegation"] = {"openTasks": t["open_n"], "overdue": t["overdue"], "dueToday": t["due_today"], "awaitingReview": t["review"],
                             "completed30": t["done30"], "onTimeRate30": round(100 * t["on_time30"] / t["done30"], 1) if t["done30"] else None,
                             "top": [{"name": r["name"], "score": r["totalScore"]} for r in top]}

        cur.execute("""SELECT s.site_name, COUNT(*) AS n, COALESCE(SUM(e.invoice_amount), 0) AS amt,
                              COUNT(*) FILTER (WHERE e.invoice_no IS NULL OR e.invoice_amount IS NULL) AS incomplete
                       FROM fact_purchase_entry e JOIN dim_purchase_site s ON s.site_key = e.site_key
                       LEFT JOIN dim_date md ON md.date_key = e.material_received_date_key
                       WHERE md.full_date >= %s OR e.material_received_date_key IS NULL AND e.created_at >= %s GROUP BY s.site_name ORDER BY 1""", (ms, ms))
        sites = [{"site": r["site_name"], "entries": r["n"], "amount": float(r["amt"]), "incomplete": r["incomplete"]} for r in cur.fetchall()]
        cur.execute("""SELECT p.party_name AS vendor, SUM(e.invoice_amount) AS amt, COUNT(*) AS n FROM fact_purchase_entry e
                       JOIN dim_party p ON p.party_key = e.vendor_party_key JOIN dim_date md ON md.date_key = e.material_received_date_key
                       WHERE md.full_date >= %s AND e.invoice_amount IS NOT NULL GROUP BY p.party_name ORDER BY amt DESC LIMIT 5""", (d30,))
        out["purchase"] = {"sites": sites, "monthEntries": sum(s["entries"] for s in sites), "monthAmount": sum(s["amount"] for s in sites),
                           "monthIncomplete": sum(s["incomplete"] for s in sites),
                           "topVendors30": [{"vendor": r["vendor"], "amount": float(r["amt"]), "entries": r["n"]} for r in cur.fetchall()]}
    out["asOf"] = today.isoformat()
    return out
