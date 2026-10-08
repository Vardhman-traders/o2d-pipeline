"""Payments module: cash / bank entries by company and party, with an approval step for the account team.

The rules live in the data, not in code:
  - companies, payment modes, transaction types and parties are master lists (Setup > Dropdown values);
  - which types count as money in or out, which move cash<->bank, which companies' cash counts as "sales cash" and which
    payment mode leaves a cashier's entry waiting for approval are flags on those lists (no 'VT', 'ASIAN', 'Cash' or
    'PENDING' typed into the code);
  - the old Cashier / Account / Admin logins are simply the pages a person holds (Setup > Access).
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict

from . import access, auth, db, modcommon as mc, roles

router = APIRouter(prefix="/payments", tags=["payments"])
PAGES = ("pay_cashier", "pay_account")
ANY = auth.require_roles(*roles.ALL_ROLES)

SELECT = """
SELECT t.txn_key, t.txn_no, d.full_date AS txn_date, tt.type_name, tt.direction, c.company_name, p.party_name,
       m.mode_name, t.amount, t.account_name, t.invoice_no, t.remarks, t.status, t.is_backdated,
       cu.username AS created_by, t.created_at, au.username AS approved_by, t.approved_at
FROM fact_payment_txn t
JOIN dim_date d ON d.date_key = t.txn_date_key
JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key
JOIN dim_company c ON c.company_key = t.company_key
JOIN dim_payment_mode m ON m.mode_key = t.mode_key
LEFT JOIN dim_party p ON p.party_key = t.party_key
LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key
LEFT JOIN dim_user au ON au.user_key = t.approved_by_user_key
"""


def pay_role(user) -> str:
    """Admin / Account / Cashier - worked out from the pages the person holds, the way the old login handed it out."""
    if user["role"] == "admin":
        return "Admin"
    held = access.effective_access(user)
    if "pay_account" in held:
        return "Account"
    if "pay_cashier" in held:
        return "Cashier"
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _user_gate(user) -> str:
    role = pay_role(user)
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    return role


def _can_write(user):
    access.require_edit(user, *PAGES)   # "View only" on the Payments page: look, never save


def _row(r: dict, today: date) -> dict:
    return {"TxnID": r["txn_no"], "Date": r["txn_date"].isoformat(), "Type": r["type_name"], "Company": r["company_name"],
            "Party": r["party_name"] or "", "Mode": r["mode_name"], "Amount": float(r["amount"]),
            "Invoice": r["invoice_no"] or "", "Remarks": r["remarks"] or "", "CreatedBy": r["created_by"] or "",
            "CreatedAt": r["created_at"].isoformat() if r["created_at"] else "", "Status": r["status"],
            "IsBackdated": "Yes" if r["is_backdated"] else "No", "ApprovedBy": r["approved_by"] or "",
            "ApprovedAt": r["approved_at"].isoformat() if r["approved_at"] else "", "IsToday": r["txn_date"] == today}


# ------------------------------------------------------------------ lists the screen fills its dropdowns from
@router.get("/bootstrap")
def bootstrap(user=Depends(ANY)):
    role = _user_gate(user)
    with db.cursor() as cur:
        cur.execute("SELECT company_name, is_sales_company FROM dim_company ORDER BY sort_order, lower(company_name)")
        comps = cur.fetchall()
        companies = [r["company_name"] for r in comps]
        cur.execute("SELECT mode_name, is_cash, needs_approval FROM dim_payment_mode ORDER BY sort_order, lower(mode_name)")
        modes = cur.fetchall()
        cur.execute("SELECT type_name, direction, affects_bank, sales_effect, entry_allowed, counts_as_received, counts_as_paid "
                    "FROM dim_txn_type ORDER BY sort_order, lower(type_name)")
        types = cur.fetchall()
    return {"ok": True, "role": role, "username": user["username"], "fullName": user["display_name"],
            "canWrite": not access.is_view_only(user, *PAGES),
            "companies": companies, "paymentModes": [m["mode_name"] for m in modes],
            "pendingModes": [m["mode_name"] for m in modes if m["needs_approval"]],
            "txnTypes": [t["type_name"] for t in types],
            "entryTypes": [t["type_name"] for t in types if t["entry_allowed"]],
            # what the card drill-downs need to pick the same entries the totals add up (no names typed into the page)
            "typeMeta": {t["type_name"]: {"direction": t["direction"], "affectsBank": t["affects_bank"], "salesEffect": t["sales_effect"],
                                          "received": t["counts_as_received"], "paid": t["counts_as_paid"]} for t in types},
            "modeMeta": {m["mode_name"]: {"isCash": m["is_cash"], "needsApproval": m["needs_approval"]} for m in modes},
            "companyMeta": {c["company_name"]: {"isSales": c["is_sales_company"]} for c in comps}}


@router.get("/parties")
def parties(company: str = Query(default=""), txn_type: str = Query(default=""), user=Depends(ANY)):
    """Parties for a company; with a transaction type, those offered for that type plus the ones offered for every type."""
    _user_gate(user)
    if not company or company == "All Companies":
        return []
    with db.cursor() as cur:
        ck = mc.lookup_key(cur, "dim_company", "company_key", "company_name", company)
        if ck is None:
            return []
        tk = mc.lookup_key(cur, "dim_txn_type", "txn_type_key", "type_name", txn_type) if txn_type and txn_type != "all" else None
        cur.execute("SELECT p.party_name FROM dim_party p WHERE p.party_kind = 'payment' AND p.company_key = %s "
                    "AND (%s::int IS NULL OR p.txn_type_key IS NULL OR p.txn_type_key = %s) "
                    "GROUP BY p.party_name ORDER BY MIN(p.sort_order), lower(p.party_name)", (ck, tk, tk))
        return [r["party_name"] for r in cur.fetchall()]


class PartyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    company: str = ""
    txnType: str = ""


def _add_party(cur, name: str, company_key: int, type_key: int | None, user=None) -> bool:
    cur.execute("SELECT 1 FROM dim_party WHERE party_kind = 'payment' AND lower(btrim(party_name)) = lower(btrim(%s)) "
                "AND company_key = %s AND txn_type_key IS NOT DISTINCT FROM %s", (name, company_key, type_key))
    if cur.fetchone():
        return False
    # a party typed on this page is usable at once and waits in Setup > Dropdown values for an admin to review it
    cur.execute("INSERT INTO dim_party (party_name, party_kind, company_key, txn_type_key, review_status, created_by_user_key) "
                "VALUES (%s, 'payment', %s, %s, 'pending', %s)",
                (" ".join(name.split()), company_key, type_key, user["user_key"] if user else None))
    return True


@router.post("/parties")
def add_party(body: PartyIn, user=Depends(ANY)):
    _user_gate(user)
    _can_write(user)
    name, company, txn_type = body.name.strip(), body.company.strip(), body.txnType.strip()
    if not name or not company:
        return {"success": False, "message": "Enter both a party name and a company."}
    with db.cursor() as cur:
        ck = mc.lookup_key(cur, "dim_company", "company_key", "company_name", company)
        tk = mc.lookup_key(cur, "dim_txn_type", "txn_type_key", "type_name", txn_type) if txn_type else None
        if ck is None or (txn_type and tk is None):
            return {"success": False, "message": "Unknown company or type. Please refresh and try again."}
        added = _add_party(cur, name, ck, tk, user)
    label = f" ({txn_type})" if txn_type else ""
    return ({"success": True, "message": f'"{name}" added under {company}{label}.'} if added
            else {"success": False, "message": f'"{name}" already exists under {company}{label}.'})


class CompanyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


@router.post("/companies")
def add_company(body: CompanyIn, user=Depends(ANY)):
    """A company typed on the page: usable straight away, waiting in Setup > Dropdown values for an admin to review."""
    _user_gate(user)
    _can_write(user)
    name = " ".join(body.name.split())
    if not name or len(name) > 100:
        return {"success": False, "message": "Enter a company name (up to 100 characters)."}
    with db.cursor() as cur:
        existing = mc.lookup_key(cur, "dim_company", "company_key", "company_name", name)
        if existing:
            cur.execute("SELECT company_name FROM dim_company WHERE company_key = %s", (existing,))
            return {"success": False, "message": f'"{name}" already exists.', "company": cur.fetchone()["company_name"]}
        cur.execute("INSERT INTO dim_company (company_name, review_status, created_by_user_key, sort_order) "
                    "VALUES (%s, 'pending', %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM dim_company))", (name, user["user_key"]))
    return {"success": True, "message": f'"{name}" added. An admin will review it in Setup.', "company": name}


# ------------------------------------------------------------------ reading
def _scope(role: str, user, company: str | None, date_from, date_to, *, rows_for_list: bool):
    """(where, params) for the transactions a person may see. List: cashier sees own entries of today (not rejected)."""
    where, params = ["TRUE"], []
    if company and company != "All Companies":
        where.append("c.company_name = %s"); params.append(company)
    if rows_for_list and role == "Cashier":
        where += ["lower(cu.username) = lower(%s)", "d.full_date = %s", "t.status <> 'Rejected'"]
        params += [user["username"], mc.today_ist()]
    if role in ("Admin", "Account") and (date_from or date_to):
        if date_from:
            where.append("d.full_date >= %s"); params.append(date_from)
        if date_to:
            where.append("d.full_date <= %s"); params.append(date_to)
    return where, params


@router.get("/transactions")
def transactions(company: str = Query(default=""), date_from: str = Query(default=""), date_to: str = Query(default=""),
                 user=Depends(ANY)):
    role = _user_gate(user)
    df, dt = mc.parse_date(date_from, "From date"), mc.parse_date(date_to, "To date")
    where, params = _scope(role, user, company, df, dt, rows_for_list=True)
    with db.cursor() as cur:
        cur.execute(SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.created_at DESC, t.txn_key DESC LIMIT 5000", params)
        rows = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM fact_payment_txn")
        total = cur.fetchone()["n"]
    today = mc.today_ist()
    return {"ok": True, "version": "render", "rows": [_row(r, today) for r in rows], "rawCount": total}


@router.get("/dashboard")
def dashboard(company: str = Query(default=""), date_from: str = Query(default=""), date_to: str = Query(default=""),
              user=Depends(ANY)):
    role = _user_gate(user)
    df, dt = mc.parse_date(date_from, "From date"), mc.parse_date(date_to, "To date")
    today = mc.today_ist()
    scoped_today = role == "Cashier" or (role == "Account" and not df and not dt)
    where, params = ["t.status = 'Approved'"], []
    if company and company != "All Companies":
        where.append("c.company_name = %s"); params.append(company)
    if scoped_today:
        where.append("d.full_date = %s"); params.append(today)
    elif df or dt:
        if df:
            where.append("d.full_date >= %s"); params.append(df)
        if dt:
            where.append("d.full_date <= %s"); params.append(dt)
    sign = "CASE WHEN tt.direction = 'in' THEN 1 ELSE -1 END"
    with db.cursor() as cur:
        cur.execute(f"""
            SELECT
              COALESCE(SUM(CASE WHEN m.is_cash THEN {sign} * t.amount END), 0) AS cash_in_hand,
              COALESCE(SUM(CASE WHEN NOT m.is_cash AND tt.affects_bank THEN {sign} * t.amount END), 0) AS bank_balance,
              COALESCE(SUM(CASE WHEN tt.counts_as_received THEN t.amount END), 0) AS received,
              COALESCE(SUM(CASE WHEN tt.counts_as_paid THEN t.amount END), 0) AS paid,
              COALESCE(SUM(CASE WHEN c.is_sales_company AND m.is_cash THEN tt.sales_effect * t.amount END), 0) AS cash_sales
            FROM fact_payment_txn t
            JOIN dim_date d ON d.date_key = t.txn_date_key JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key
            JOIN dim_company c ON c.company_key = t.company_key JOIN dim_payment_mode m ON m.mode_key = t.mode_key
            WHERE {' AND '.join(where)}""", params)
        s = cur.fetchone()
        pw, pp = ["t.status = 'Pending'"], []
        if company and company != "All Companies":
            pw.append("c.company_name = %s"); pp.append(company)
        if role == "Cashier":
            pw.append("lower(cu.username) = lower(%s)"); pp.append(user["username"])
        cur.execute(f"""SELECT tt.type_name, count(*) AS n FROM fact_payment_txn t
            JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key JOIN dim_company c ON c.company_key = t.company_key
            LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key WHERE {' AND '.join(pw)} GROUP BY 1""", pp)
        by_type = {r["type_name"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT type_name FROM dim_txn_type")
        for r in cur.fetchall():
            by_type.setdefault(r["type_name"], 0)
        cur.execute("SELECT count(*) AS n FROM fact_payment_txn")
        raw = cur.fetchone()["n"]
    pending = sum(by_type.values())
    if role == "Cashier":
        label = "Today's Summary"
    elif role == "Account":
        label = "Filtered Summary (Account)" if (df or dt) else "Today's Summary"
    else:
        label = "Filtered Summary" if (df or dt) else "Overall Summary"
    return {"cashInHand": float(s["cash_in_hand"]), "bankBalance": float(s["bank_balance"]),
            "totalReceived": float(s["received"]), "totalPaid": float(s["paid"]), "cashSales": float(s["cash_sales"]),
            "pendingEntries": pending, "pendingByType": {"all": pending, **by_type}, "scopeLabel": label, "rawCount": raw}


# ------------------------------------------------------------------ writing
class TxnIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str = ""
    type: str = ""
    company: str = ""
    party: str = ""
    mode: str = ""
    amount: float | str = 0
    accountName: str = ""
    invoice: str = ""
    remarks: str = ""


def _clean(body: TxnIn, cur) -> dict:
    """Validate the form and turn the names into master-list keys. Raises 422 with a message the screen shows as is."""
    try:
        amount = round(float(body.amount or 0), 2)
    except (TypeError, ValueError):
        amount = 0
    if not body.date or not body.type or not body.company or amount <= 0:
        raise HTTPException(422, "Date, Type, Company and Amount are required.")
    d = mc.parse_date(body.date)
    out = {"date": d, "amount": amount, "party": body.party.strip(), "invoice": body.invoice.strip()[:80] or None,
           "remarks": body.remarks.strip() or None, "account": body.accountName.strip()[:150] or None,
           "type_key": mc.lookup_key(cur, "dim_txn_type", "txn_type_key", "type_name", body.type),
           "company_key": mc.lookup_key(cur, "dim_company", "company_key", "company_name", body.company),
           "mode_key": mc.lookup_key(cur, "dim_payment_mode", "mode_key", "mode_name", body.mode or "Cash")}
    if None in (out["type_key"], out["company_key"], out["mode_key"]):
        raise HTTPException(422, "Unknown dropdown value selected. Please refresh and try again.")
    return out


def _party_key(cur, name: str, company_key: int, type_key: int, user=None) -> int | None:
    """The party an entry points at; a new name is added to the list (for this company and type), so it is there next time."""
    if not name:
        return None
    _add_party(cur, name, company_key, type_key, user)
    cur.execute("SELECT party_key FROM dim_party WHERE party_kind = 'payment' AND lower(btrim(party_name)) = lower(btrim(%s)) "
                "AND company_key = %s ORDER BY (txn_type_key = %s) DESC NULLS LAST, party_key LIMIT 1", (name, company_key, type_key))
    return cur.fetchone()["party_key"]


@router.post("/transactions")
def save_transaction(body: TxnIn, user=Depends(ANY)):
    role = _user_gate(user)
    _can_write(user)
    today = mc.today_ist()
    try:
        with db.cursor() as cur:
            c = _clean(body, cur)
            cur.execute("SELECT needs_approval FROM dim_payment_mode WHERE mode_key = %s", (c["mode_key"],))
            needs = cur.fetchone()["needs_approval"]
            auto = role in ("Account", "Admin") or (role == "Cashier" and not needs)
            party = _party_key(cur, c["party"], c["company_key"], c["type_key"], user)
            cur.execute(
                "INSERT INTO fact_payment_txn (txn_date_key, txn_type_key, company_key, party_key, mode_key, amount, account_name, "
                "invoice_no, remarks, status, is_backdated, created_by_user_key, approved_by_user_key, approved_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING txn_no",
                (mc.date_key(c["date"]), c["type_key"], c["company_key"], party, c["mode_key"], c["amount"], c["account"],
                 c["invoice"], c["remarks"], "Approved" if auto else "Pending", c["date"] < today, user["user_key"],
                 user["user_key"] if auto else None, None))
            txn_no = cur.fetchone()["txn_no"]
            if auto:
                cur.execute("UPDATE fact_payment_txn SET approved_at = now() WHERE txn_no = %s", (txn_no,))
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    except pgerr.ForeignKeyViolation as e:
        return {"success": False, "message": mc.date_fk_error(e).detail}
    return {"success": True, "txnId": txn_no, "isBackdated": c["date"] < today, "autoApproved": auto}


@router.put("/transactions/{txn_no}")
def update_transaction(txn_no: str, body: TxnIn, user=Depends(ANY)):
    role = _user_gate(user)
    _can_write(user)
    today = mc.today_ist()
    try:
        with db.cursor() as cur:
            c = _clean(body, cur)
            cur.execute("SELECT t.txn_key, t.status, lower(cu.username) AS owner, d.full_date FROM fact_payment_txn t "
                        "JOIN dim_date d ON d.date_key = t.txn_date_key LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key "
                        "WHERE t.txn_no = %s FOR UPDATE OF t", (txn_no,))
            row = cur.fetchone()
            if not row:
                return {"success": False, "message": "Entry not found — it may have been removed."}
            mine = row["owner"] == user["username"].lower()
            if role == "Account" and row["status"] != "Pending" and not mine:
                return {"success": False, "message": "This entry is Approved and belongs to another user. Only Admin can edit it."}
            if role == "Cashier":
                if not mine:
                    return {"success": False, "message": "You can only edit your own entries."}
                if row["full_date"] != today:
                    return {"success": False, "message": "You can only edit today's entries. Previous day entries are locked."}
            party = _party_key(cur, c["party"], c["company_key"], c["type_key"], user)
            cur.execute("UPDATE fact_payment_txn SET txn_date_key=%s, txn_type_key=%s, company_key=%s, party_key=%s, mode_key=%s, "
                        "amount=%s, invoice_no=%s, remarks=%s, is_backdated=%s, updated_at=now() WHERE txn_key=%s",
                        (mc.date_key(c["date"]), c["type_key"], c["company_key"], party, c["mode_key"], c["amount"],
                         c["invoice"], c["remarks"], c["date"] < today, row["txn_key"]))
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    except pgerr.ForeignKeyViolation as e:
        return {"success": False, "message": mc.date_fk_error(e).detail}
    return {"success": True, "txnId": txn_no, "isBackdated": c["date"] < today}


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: str = ""


@router.post("/transactions/{txn_no}/decision")
def decide(txn_no: str, body: DecisionIn, user=Depends(ANY)):
    role = _user_gate(user)
    _can_write(user)
    if role not in ("Account", "Admin"):
        return {"success": False, "message": "Only Account can approve or reject entries."}
    if body.decision not in ("Approved", "Rejected"):
        return {"success": False, "message": "Invalid decision."}
    with db.cursor() as cur:
        cur.execute("SELECT status FROM fact_payment_txn WHERE txn_no = %s FOR UPDATE", (txn_no,))
        row = cur.fetchone()
        if not row:
            return {"success": False, "message": "Entry not found — it may have been removed."}
        if row["status"] != "Pending":
            return {"success": False, "message": "This entry is not pending — it may have already been decided."}
        cur.execute("UPDATE fact_payment_txn SET status = %s, approved_by_user_key = %s, approved_at = now() WHERE txn_no = %s",
                    (body.decision, user["user_key"], txn_no))
    return {"success": True}
