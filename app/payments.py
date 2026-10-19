"""Payments module: cash / bank entries by company and party, with an approval step for the account team.

The rules live in the data, not in code:
  - companies, payment modes, transaction types and parties are master lists (Setup > Dropdown values);
  - which types count as money in or out, which move cash<->bank, which companies' cash counts as "sales cash" and which
    payment mode leaves a cashier's entry waiting for approval are flags on those lists (no 'VT', 'ASIAN', 'Cash' or
    'PENDING' typed into the code);
  - the old Cashier / Account / Admin logins are simply the pages a person holds (Setup > Access).
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict

from . import access, auth, config, db, modcommon as mc, roles

router = APIRouter(prefix="/payments", tags=["payments"])
PAGES = ("pay_cashier", "pay_account")
ANY = auth.require_roles(*roles.ALL_ROLES)

SELECT = """
SELECT t.txn_key, t.txn_no, d.full_date AS txn_date, tt.type_name, tt.direction, c.company_name, p.party_name,
       m.mode_name, t.amount, t.account_name, t.invoice_no, t.remarks, t.status, t.is_backdated, t.order_warning,
       cu.username AS created_by, t.created_at, au.username AS approved_by, t.approved_at,
       EXISTS (SELECT 1 FROM payment_push pp WHERE pp.cash_txn_key = t.txn_key OR pp.bank_txn_key = t.txn_key) AS from_o2d
FROM fact_payment_txn t
JOIN dim_date d ON d.date_key = t.txn_date_key
JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key
JOIN dim_company c ON c.company_key = t.company_key
JOIN dim_payment_mode m ON m.mode_key = t.mode_key
LEFT JOIN dim_party p ON p.party_key = t.party_key
LEFT JOIN dim_user cu ON cu.user_key = t.created_by_user_key
LEFT JOIN dim_user au ON au.user_key = t.approved_by_user_key
"""


EXPENSE_PAGES = ("pay_expense", "pay_expense_manage", "pay_challan")   # the extra tabs of the screen


def pay_role(user, allow_expenses_only: bool = False) -> str:
    """Admin / Account / Cashier - worked out from the pages the person holds, the way the old login handed it out.
    A person who holds only the Regular expenses pages is "Staff": they may open the screen (and so get the lists) but not
    the transaction entries - every other route keeps refusing them."""
    if user["role"] == "admin":
        return "Admin"
    held = access.effective_access(user)
    if "pay_account" in held:
        return "Account"
    if "pay_cashier" in held:
        return "Cashier"
    if allow_expenses_only and set(EXPENSE_PAGES) & set(held):
        return "Staff"
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def expense_level(user) -> str | None:
    """'manage' / 'enter' / None - what the person may do on the Regular expenses tab."""
    if user["role"] == "admin":
        return "manage"
    held = access.effective_access(user)
    return "manage" if "pay_expense_manage" in held else "enter" if "pay_expense" in held else None


def _user_gate(user, allow_expenses_only: bool = False) -> str:
    role = pay_role(user, allow_expenses_only)
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
            "ApprovedAt": r["approved_at"].isoformat() if r["approved_at"] else "", "IsToday": r["txn_date"] == today,
            "Warning": r.get("order_warning") or "", "Source": "O2D" if r.get("from_o2d") else "Direct"}


# ------------------------------------------------------------------ lists the screen fills its dropdowns from
@router.get("/bootstrap")
def bootstrap(user=Depends(ANY)):
    role = _user_gate(user, allow_expenses_only=True)
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
            "canWrite": not access.is_view_only(user, *PAGES), "expenseLevel": expense_level(user),
            "challan": user["role"] == "admin" or "pay_challan" in access.effective_access(user),
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
    """The one party list. (company and txn_type are still accepted so older pages keep working, but they no longer filter anything.)"""
    _user_gate(user)
    with db.cursor() as cur:
        cur.execute("SELECT party_name FROM dim_party WHERE party_kind = 'payment' ORDER BY sort_order, lower(party_name)")
        return [r["party_name"] for r in cur.fetchall()]


class PartyIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    company: str = ""     # no longer used: a party belongs to no company or type
    txnType: str = ""


def _add_party(cur, name: str, user=None) -> bool:
    """Add a name to the party list unless it is already there (any capitals / spacing). Returns True when it was new."""
    clean = " ".join(name.split())
    cur.execute("SELECT 1 FROM dim_party WHERE party_kind = 'payment' AND lower(btrim(party_name)) = lower(%s)", (clean,))
    if cur.fetchone():
        return False
    # a party typed on a screen is usable at once and waits in Setup > Dropdown values for an admin to review it
    cur.execute("INSERT INTO dim_party (party_name, party_kind, review_status, created_by_user_key) VALUES (%s, 'payment', 'pending', %s)",
                (clean, user["user_key"] if user else None))
    return True


@router.post("/parties")
def add_party(body: PartyIn, user=Depends(ANY)):
    _user_gate(user)
    _can_write(user)
    name = " ".join(body.name.split())
    if not name:
        return {"success": False, "message": "Enter the party name."}
    if len(name) > 150:
        return {"success": False, "message": "The party name is too long (150 characters at most)."}
    with db.cursor() as cur:
        added = _add_party(cur, name, user)
    return ({"success": True, "message": f'"{name}" added to the party list.'} if added
            else {"success": False, "message": f'"{name}" is already in the party list.'})


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
        # a cashier who enters sees the entries they typed today; someone who may only LOOK (View only) has typed none, so they see today's
        # entries of everyone - the same entries the cards above the table add up
        if not access.is_view_only(user, *PAGES):
            where.append("lower(cu.username) = lower(%s)"); params.append(user["username"])
        where += ["d.full_date = %s", "t.status <> 'Rejected'"]
        params.append(mc.today_ist())
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
              COALESCE(SUM(CASE WHEN NOT m.is_cash THEN {sign} * t.amount END), 0) AS bank_balance,
              COALESCE(SUM(CASE WHEN tt.counts_as_received AND m.is_cash AND NOT from_o2d THEN t.amount END), 0) AS received,
              COALESCE(SUM(CASE WHEN tt.counts_as_paid AND m.is_cash AND NOT from_o2d THEN t.amount END), 0) AS paid,
              COALESCE(SUM(CASE WHEN from_o2d THEN tt.sales_effect * t.amount END), 0) AS cash_sales
            FROM (SELECT t.*, EXISTS (SELECT 1 FROM payment_push pp WHERE pp.cash_txn_key = t.txn_key OR pp.bank_txn_key = t.txn_key) AS from_o2d
                  FROM fact_payment_txn t) t
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


AMOUNT_DIVISOR = config.AMOUNT_RECEIVED_DIVISOR


def _clean(body: TxnIn, cur) -> dict:
    """Validate the form and turn the names into master-list keys. Raises 422 with a message the screen shows as is."""
    try:   # what is typed is divided by 100, like every amount in the portal (type 10,000 -> saved and shown as 100)
        amount = round(float(body.amount or 0) / AMOUNT_DIVISOR, 2)
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


def _party_key(cur, name: str, user=None) -> int | None:
    """The party an entry points at. A name in the list is used as it is; a new name is added to the list once, for everyone."""
    if not name:
        return None
    _add_party(cur, name, user)
    cur.execute("SELECT party_key FROM dim_party WHERE party_kind = 'payment' AND lower(btrim(party_name)) = lower(btrim(%s)) "
                "ORDER BY party_key LIMIT 1", (name,))
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
            party = _party_key(cur, c["party"], user)
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
            party = _party_key(cur, c["party"], user)
            cur.execute("UPDATE fact_payment_txn SET txn_date_key=%s, txn_type_key=%s, company_key=%s, party_key=%s, mode_key=%s, "
                        "amount=%s, invoice_no=%s, remarks=%s, is_backdated=%s, updated_at=now() WHERE txn_key=%s",
                        (mc.date_key(c["date"]), c["type_key"], c["company_key"], party, c["mode_key"], c["amount"],
                         c["invoice"], c["remarks"], c["date"] < today, row["txn_key"]))
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    except pgerr.ForeignKeyViolation as e:
        return {"success": False, "message": mc.date_fk_error(e).detail}
    return {"success": True, "txnId": txn_no, "isBackdated": c["date"] < today}


def _admin_only(user) -> None:
    if _user_gate(user) != "Admin":
        raise HTTPException(403, "Only an admin can do this.")


@router.get("/transactions/{txn_no}/order-change")
def order_change(txn_no: str, user=Depends(ANY)):
    """Admin: what happened to the O2D order behind a merged entry (the entry carries a warning when it changed after the merge): the money
    that was merged against the order now, and every change made to the order since the merge."""
    from . import timeline
    _admin_only(user)
    with db.cursor() as cur:
        cur.execute(SELECT + " WHERE t.txn_no = %s", (txn_no,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "Entry not found - it may have been removed.")
        cur.execute("SELECT order_key, kind, merged_at, merged_kind, merged_cash, merged_bank, cash_amount, bank_amount, edited_after_merge "
                    "FROM payment_push WHERE cash_txn_key = %s OR bank_txn_key = %s", (t["txn_key"], t["txn_key"]))
        push = cur.fetchone()
        entry = {"txnId": t["txn_no"], "date": t["txn_date"].isoformat(), "type": t["type_name"], "company": t["company_name"],
                 "party": t["party_name"] or "", "mode": t["mode_name"], "amount": float(t["amount"]), "invoice": t["invoice_no"] or "",
                 "status": t["status"], "warning": t["order_warning"] or ""}
        if not push:
            return {"ok": True, "entry": entry, "order": None, "events": [], "merged": None}
        cur.execute("SELECT * FROM v_orders_archive WHERE order_key = %s", (push["order_key"],))
        o = cur.fetchone()
        if not o:
            return {"ok": True, "entry": entry, "order": None, "events": [], "merged": None}
        tl = timeline.timeline_for(cur, push["order_key"], o)
    since = push["merged_at"]
    events = []
    for e in tl["events"]:
        if e["type"] in ("whatsapp", "created") or e.get("reconstructed") or not e["at"]:
            continue
        if since is None or datetime.fromisoformat(e["at"]) >= since:
            events.append(e)
    return {"ok": True, "entry": entry, "events": events,
            "order": {"slNo": o["sl_no"], "dcNo": o["dc_inv_no"] or "", "address": o["shipping_location"] or "", "stage": o["stage"],
                      "deliveryStatus": o["delivery_status"] or "", "paymentStatus": o["payment_status"] or "",
                      "amount": float(o["amount_received"]) if o["amount_received"] is not None else None,
                      "cancelled": bool(o["is_cancelled"])},
            "merged": {"at": timeline._iso(since), "kind": push["merged_kind"] or push["kind"],
                       "cash": float(push["merged_cash"]) if push["merged_cash"] is not None else None,
                       "bank": float(push["merged_bank"]) if push["merged_bank"] is not None else None}}


@router.post("/transactions/{txn_no}/clear-warning")
def clear_warning(txn_no: str, user=Depends(ANY)):
    """Admin: the change behind the warning has been looked at - remove the warning sign."""
    _admin_only(user)
    with db.cursor() as cur:
        cur.execute("UPDATE fact_payment_txn SET order_warning = NULL WHERE txn_no = %s RETURNING txn_key", (txn_no,))
        if not cur.fetchone():
            raise HTTPException(404, "Entry not found - it may have been removed.")
        cur.execute("UPDATE payment_push SET edited_after_merge = false WHERE cash_txn_key = (SELECT txn_key FROM fact_payment_txn WHERE txn_no = %s) "
                    "OR bank_txn_key = (SELECT txn_key FROM fact_payment_txn WHERE txn_no = %s)", (txn_no, txn_no))
    return {"ok": True, "success": True}


@router.delete("/transactions/{txn_no}")
def delete_transaction(txn_no: str, user=Depends(ANY)):
    """Admin: remove an entry for good (an entry merged from O2D comes back on the Sales / Sales Return tab as "already merged", so it can be
    merged again). Recorded in the admin audit log."""
    from .admin import audit
    _admin_only(user)
    with db.cursor() as cur:
        cur.execute(SELECT + " WHERE t.txn_no = %s FOR UPDATE OF t", (txn_no,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "Entry not found - it may have been removed.")
        cur.execute("DELETE FROM fact_payment_txn WHERE txn_key = %s", (t["txn_key"],))
        audit(cur, user, "payment.delete", txn_no, {"type": t["type_name"], "amount": float(t["amount"]), "party": t["party_name"],
                                                    "company": t["company_name"], "status": t["status"], "date": t["txn_date"].isoformat()})
    return {"ok": True, "success": True}


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
