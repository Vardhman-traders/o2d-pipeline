"""Regular expenses, in the Payments module: a running float of money (petty cash) that expenses are taken out of.

  balance = opening balance + money added (top-ups) - expenses, counting only entries that are not voided.

How it works:
  - the accounts (opening balance) and the expense categories are master lists (Setup > Dropdown values); the list of people
    a spend can be recorded against is the portal's own people (dim_user), dates are dim_date;
  - an entry is never edited or deleted: a wrong one is voided with a reason (who and when are kept), so the balance can
    always be explained. The person who entered it can void it the same day; a manager can void any;
  - recording an expense needs the page "Payments - Regular expenses"; adding money to the account and voiding other
    people's entries need "... manager" - so the balance cannot be topped up by the person spending from it;
  - an expense that takes the balance below zero is saved (the money was spent) and the screen says the account is
    overdrawn, so the missing top-up gets noticed rather than hidden.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import access, auth, db, modcommon as mc, roles
from .admin import audit

router = APIRouter(prefix="/payments/expenses", tags=["expenses"])
PAGES = ("pay_expense", "pay_expense_manage")
ANY = auth.require_roles(*roles.ALL_ROLES)
MAX_ROWS = 5000
MAX_AMOUNT = 99_999_999.99

ENTRY_SQL = """
SELECT e.entry_key, e.entry_no, e.kind, e.amount, e.paid_to, e.description, e.created_at, e.voided_at, e.void_reason,
       d.full_date AS entry_date, a.account_key, a.account_name, c.category_name,
       sb.username AS spent_by_id, sb.display_name AS spent_by, cb.display_name AS created_by, cb.user_key AS created_by_key,
       vb.display_name AS voided_by
FROM fact_expense_entry e
JOIN dim_date d ON d.date_key = e.entry_date_key
JOIN dim_expense_account a ON a.account_key = e.account_key
LEFT JOIN dim_expense_category c ON c.category_key = e.category_key
LEFT JOIN dim_user sb ON sb.user_key = e.spent_by_user_key
LEFT JOIN dim_user cb ON cb.user_key = e.created_by_user_key
LEFT JOIN dim_user vb ON vb.user_key = e.voided_by_user_key
"""


def _level(user) -> str:
    """'manage' | 'enter' - or a plain refusal."""
    if user["role"] == "admin":
        return "manage"
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    held = access.effective_access(user)
    if "pay_expense_manage" in held:
        return "manage"
    if "pay_expense" in held:
        return "enter"
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _can_write(user):
    access.require_edit(user, *PAGES)


def _signed(kind: str, amount) -> float:
    return float(amount) if kind == "Topup" else -float(amount)


def balances(cur, account_key: int | None = None) -> dict[int, dict]:
    """Opening, money in, money out and the balance right now, per account (voided entries left out)."""
    cur.execute("""SELECT a.account_key, a.account_name, a.opening_balance,
                          COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'Topup'), 0) AS topups,
                          COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'Expense'), 0) AS spent
                   FROM dim_expense_account a
                   LEFT JOIN fact_expense_entry e ON e.account_key = a.account_key AND e.voided_at IS NULL
                   WHERE (%s::int IS NULL OR a.account_key = %s)
                   GROUP BY a.account_key ORDER BY a.sort_order, lower(a.account_name)""", (account_key, account_key))
    return {r["account_key"]: {"key": r["account_key"], "name": r["account_name"], "opening": float(r["opening_balance"]),
                               "topups": float(r["topups"]), "spent": float(r["spent"]),
                               "balance": round(float(r["opening_balance"]) + float(r["topups"]) - float(r["spent"]), 2)}
            for r in cur.fetchall()}


def _account(cur, name: str | None) -> int:
    cur.execute("SELECT account_key, account_name FROM dim_expense_account ORDER BY sort_order, account_key")
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(409, "No expense account is set up yet (Setup > Dropdown values > Regular expenses - accounts).")
    if not name:
        return rows[0]["account_key"]
    for r in rows:
        if r["account_name"].strip().lower() == name.strip().lower():
            return r["account_key"]
    raise HTTPException(422, f"Account '{name}' not found.")


def _entry(r: dict, today: date, user, level: str, running: float | None = None) -> dict:
    mine_today = r["created_by_key"] == user["user_key"] and r["created_at"].astimezone(mc.IST).date() == today
    return {"key": r["entry_key"], "no": r["entry_no"], "date": r["entry_date"].isoformat(), "account": r["account_name"], "kind": r["kind"],
            "amount": float(r["amount"]), "category": r["category_name"] or "", "spentBy": r["spent_by"] or "", "paidTo": r["paid_to"] or "",
            "description": r["description"] or "", "enteredBy": r["created_by"] or "", "enteredAt": mc.iso(r["created_at"]),
            "voided": r["voided_at"] is not None, "voidReason": r["void_reason"] or "", "voidedBy": r["voided_by"] or "",
            "canVoid": r["voided_at"] is None and (level == "manage" or mine_today), "balanceAfter": running}


@router.get("/bootstrap")
def bootstrap(user=Depends(ANY)):
    level = _level(user)
    with db.cursor() as cur:
        bal = balances(cur)
        cur.execute("SELECT category_name FROM dim_expense_category ORDER BY sort_order, lower(category_name)")
        cats = [r["category_name"] for r in cur.fetchall()]
        keys = sorted(access.users_with_page("pay_expense") | access.users_with_page("pay_expense_manage"))
        cur.execute("SELECT username, display_name FROM dim_user WHERE (user_key = ANY(%s) OR role = 'admin' OR user_key = %s) "
                    "AND password_hash <> %s ORDER BY lower(display_name)", (keys, user["user_key"], auth.DISABLED_HASH))
        people = [{"id": r["username"], "name": r["display_name"]} for r in cur.fetchall()]
    return {"ok": True, "level": level, "canWrite": not access.is_view_only(user, *PAGES), "me": user["username"], "myName": user["display_name"],
            "today": mc.today_ist().isoformat(), "accounts": list(bal.values()), "categories": cats, "people": people}


@router.get("/summary")
def summary(account: str = Query(default=""), date_from: str = Query(default=""), date_to: str = Query(default=""),
            user=Depends(ANY)):
    """The balance right now, and for a period: opening, money added, spent, closing and where it went."""
    _level(user)
    today = mc.today_ist()
    d_to = mc.parse_date(date_to, "To date") or today
    d_from = mc.parse_date(date_from, "From date") or d_to.replace(day=1)
    if d_from > d_to:
        raise HTTPException(422, "The From date is after the To date.")
    with db.cursor() as cur:
        key = _account(cur, account)
        now = balances(cur, key)[key]
        cur.execute("""SELECT COALESCE(SUM(CASE WHEN e.kind = 'Topup' THEN e.amount ELSE -e.amount END)
                                FILTER (WHERE d.full_date < %s), 0) AS before_net,
                              COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'Topup' AND d.full_date BETWEEN %s AND %s), 0) AS topups,
                              COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'Expense' AND d.full_date BETWEEN %s AND %s), 0) AS spent,
                              COUNT(*) FILTER (WHERE e.kind = 'Expense' AND d.full_date BETWEEN %s AND %s) AS spend_count
                       FROM fact_expense_entry e JOIN dim_date d ON d.date_key = e.entry_date_key
                       WHERE e.account_key = %s AND e.voided_at IS NULL""",
                    (d_from, d_from, d_to, d_from, d_to, d_from, d_to, key))
        p = cur.fetchone()
        cur.execute("""SELECT COALESCE(c.category_name, 'Uncategorised') AS category, SUM(e.amount) AS amt, COUNT(*) AS n
                       FROM fact_expense_entry e JOIN dim_date d ON d.date_key = e.entry_date_key
                       LEFT JOIN dim_expense_category c ON c.category_key = e.category_key
                       WHERE e.account_key = %s AND e.voided_at IS NULL AND e.kind = 'Expense' AND d.full_date BETWEEN %s AND %s
                       GROUP BY 1 ORDER BY amt DESC""", (key, d_from, d_to))
        cats = [{"category": r["category"], "amount": float(r["amt"]), "entries": r["n"]} for r in cur.fetchall()]
        cur.execute("""SELECT COALESCE(u.display_name, '-') AS who, SUM(e.amount) AS amt FROM fact_expense_entry e
                       JOIN dim_date d ON d.date_key = e.entry_date_key LEFT JOIN dim_user u ON u.user_key = e.spent_by_user_key
                       WHERE e.account_key = %s AND e.voided_at IS NULL AND e.kind = 'Expense' AND d.full_date BETWEEN %s AND %s
                       GROUP BY 1 ORDER BY amt DESC""", (key, d_from, d_to))
        people = [{"name": r["who"], "amount": float(r["amt"])} for r in cur.fetchall()]
    opening = round(now["opening"] + float(p["before_net"]), 2)
    topups, spent = float(p["topups"]), float(p["spent"])
    return {"account": now["name"], "balance": now["balance"], "overdrawn": now["balance"] < 0,
            "period": {"from": d_from.isoformat(), "to": d_to.isoformat(), "opening": opening, "topups": topups, "spent": spent,
                       "closing": round(opening + topups - spent, 2), "spendCount": p["spend_count"]},
            "byCategory": cats, "byPerson": people}


@router.get("/entries")
def entries(account: str = Query(default=""), date_from: str = Query(default=""), date_to: str = Query(default=""),
            category: str = Query(default=""), kind: str = Query(default=""), include_voided: bool = Query(default=True),
            user=Depends(ANY)):
    """The ledger, oldest first, with the balance after each entry (worked out over the whole account, so it stays true when
    the list is filtered)."""
    level = _level(user)
    if kind not in ("", "Topup", "Expense"):
        raise HTTPException(422, "Kind must be Topup or Expense.")
    today = mc.today_ist()
    d_from, d_to = mc.parse_date(date_from, "From date"), mc.parse_date(date_to, "To date")
    with db.cursor() as cur:
        key = _account(cur, account)
        opening = balances(cur, key)[key]["opening"]
        cur.execute(ENTRY_SQL + " WHERE e.account_key = %s ORDER BY d.full_date, e.entry_key", (key,))
        rows = cur.fetchall()
    out, running, total = [], opening, 0
    for r in rows:
        if r["voided_at"] is None:
            running = round(running + _signed(r["kind"], r["amount"]), 2)
        item = _entry(r, today, user, level, None if r["voided_at"] else running)
        if d_from and r["entry_date"] < d_from or d_to and r["entry_date"] > d_to:
            continue
        if kind and r["kind"] != kind or category and (r["category_name"] or "").lower() != category.lower():
            continue
        if r["voided_at"] is not None and not include_voided:
            continue
        total += 1
        out.append(item)
    return {"account": next(iter([r["account_name"] for r in rows]), ""), "opening": opening, "count": total,
            "truncated": total > MAX_ROWS, "entries": out[-MAX_ROWS:]}


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = ""
    date: str = ""
    kind: str = "Expense"
    amount: float
    category: str = ""
    spentBy: str = ""
    paidTo: str = ""
    description: str = ""


@router.post("/entries", status_code=201)
def add_entry(body: EntryIn, user=Depends(ANY)):
    level = _level(user)
    _can_write(user)
    if body.kind not in ("Topup", "Expense"):
        raise HTTPException(422, "Kind must be Topup or Expense.")
    if body.kind == "Topup" and level != "manage":
        raise HTTPException(403, "Only the Regular expenses manager can add money to the account.")
    amount = round(body.amount, 2)
    if not 0 < amount <= MAX_AMOUNT:
        raise HTTPException(422, "Amount must be above 0 and below 100 crore.")
    today = mc.today_ist()
    day = mc.parse_date(body.date, "Date") or today
    if day > today:
        raise HTTPException(422, "The date cannot be in the future.")
    paid_to = " ".join(body.paidTo.split())[:150] or None
    description = body.description.strip()[:500] or None
    try:
        with db.cursor() as cur:
            account = _account(cur, body.account)
            cat_key = None
            if body.kind == "Expense":
                cat_key = mc.lookup_key(cur, "dim_expense_category", "category_key", "category_name", body.category)
                if cat_key is None:
                    raise HTTPException(422, "Choose what the money was spent on (the category).")
                if not (paid_to or description):
                    raise HTTPException(422, "Say who it was paid to or what it was for.")
            who = user["user_key"]
            if body.spentBy and body.spentBy.strip().lower() != user["username"].lower():
                cur.execute("SELECT user_key FROM dim_user WHERE lower(username) = lower(%s) AND password_hash <> %s",
                            (body.spentBy.strip(), auth.DISABLED_HASH))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(422, "Person not found.")
                who = row["user_key"]
            cur.execute("""INSERT INTO fact_expense_entry (account_key, entry_date_key, kind, amount, category_key, spent_by_user_key,
                                                           paid_to, description, created_by_user_key)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING entry_key, entry_no""",
                        (account, mc.date_key(day), body.kind, amount, cat_key, who, paid_to, description, user["user_key"]))
            row = cur.fetchone()
            bal = balances(cur, account)[account]
            audit(cur, user, "expense.add", row["entry_no"], {"kind": body.kind, "amount": amount, "balance": bal["balance"]})
    except HTTPException:
        raise
    except Exception as exc:
        raise mc.date_fk_error(exc)
    return {"key": row["entry_key"], "no": row["entry_no"], "balance": bal["balance"], "overdrawn": bal["balance"] < 0}


class VoidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


@router.post("/entries/{entry_key}/void")
def void_entry(entry_key: int, body: VoidIn, user=Depends(ANY)):
    level = _level(user)
    _can_write(user)
    reason = " ".join(body.reason.split())[:300]
    if len(reason) < 3:
        raise HTTPException(422, "Say why the entry is being cancelled.")
    today = mc.today_ist()
    with db.cursor() as cur:
        cur.execute("SELECT entry_key, entry_no, created_by_user_key, created_at, voided_at, account_key FROM fact_expense_entry "
                    "WHERE entry_key = %s FOR UPDATE", (entry_key,))
        e = cur.fetchone()
        if not e:
            raise HTTPException(404, "Entry not found.")
        if e["voided_at"] is not None:
            raise HTTPException(409, "That entry is already cancelled.")
        own_today = e["created_by_user_key"] == user["user_key"] and e["created_at"].astimezone(mc.IST).date() == today
        if level != "manage" and not own_today:
            raise HTTPException(403, "You can cancel your own entry on the day you made it. Ask the manager for anything older.")
        cur.execute("UPDATE fact_expense_entry SET voided_at = now(), voided_by_user_key = %s, void_reason = %s WHERE entry_key = %s",
                    (user["user_key"], reason, entry_key))
        bal = balances(cur, e["account_key"])[e["account_key"]]
        audit(cur, user, "expense.void", e["entry_no"], {"reason": reason, "balance": bal["balance"]})
    return {"ok": True, "balance": bal["balance"], "overdrawn": bal["balance"] < 0}
