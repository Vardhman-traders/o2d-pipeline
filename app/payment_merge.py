"""Cashier dashboard -> Payments: the "Sales" and "Sales Return" tabs of the Payments page ("awaiting merge").

When the cashier confirms an order on the O2D Cashier dashboard, a row is staged here (table payment_push) with
everything O2D knows: date, DC / Inv no., address, the amount, and the Cash / Bank split its payment status implies.
Whoever works the tab adds what O2D does not know (company, party, remarks - which of them are required is Setup >
Module settings) and merges it: that creates the Payments entries (a Cash and / or a Bank entry, Pending for the
accountant's approval, exactly like a typed entry).

An order edited after it was merged comes back (through the cashier) tagged "already merged"; merging again UPDATES
the entries it created, never adds new ones, and an approved entry is not pulled back to Pending - it is flagged with
a warning instead. Which transaction types / modes are used, and the rest, are settings (module_admin.SETTINGS), not
names typed into this code.
"""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import access, auth, config, db, payments, roles
from . import modcommon as mc

router = APIRouter(prefix="/payments/merge", tags=["payments-merge"])
PAGES = payments.PAGES
ANY = auth.require_roles(*roles.ALL_ROLES)
KINDS = {"sales": "pay_merge_sales_type", "return": "pay_merge_return_type"}
CENT = Decimal("0.01")
CHANGED = "Updated from O2D after the order changed - please review"
BECAME_ZERO = "This part became 0 in O2D after the order changed"


# ------------------------------------------------------------------ settings (Setup > Module settings)
def _text(key: str, default: str) -> str:
    return " ".join(str(config.get(key) or default).split())


def required_fields() -> list[str]:
    flags = (("company", "pay_merge_req_company", 1), ("party", "pay_merge_req_party", 0),
             ("remarks", "pay_merge_req_remarks", 0))
    return [field for field, key, dflt in flags if mc.int_setting(key, dflt)]


def _default_company_key(cur) -> int | None:
    name = _text("pay_merge_default_company", "")
    return mc.lookup_key(cur, "dim_company", "company_key", "company_name", name) if name else None


# ------------------------------------------------------------------ called by the Cashier dashboard
def _money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT)


def _prefill(split: str | None, amount: Decimal | None) -> tuple[Decimal, Decimal]:
    """(cash, bank) a payment status implies: all cash, all bank, or nothing yet (Cash + Bank / not set)."""
    amount = amount or Decimal(0)
    return {"cash": (amount, Decimal(0)), "bank": (Decimal(0), amount)}.get(split or "", (Decimal(0), Decimal(0)))


def push_order(cur, order: dict, user: dict, kind: str, amount: Decimal | None):
    """Stage (or re-stage) a confirmed order for the Payments tabs (`order` is a row of o2d_cashier._select())."""
    cash, bank = _prefill(order.get("payments_split"), amount)
    day = order.get("date_of_receiving") or mc.today_ist()
    cur.execute("SELECT * FROM payment_push WHERE order_key = %s FOR UPDATE", (order["order_key"],))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO payment_push (order_key, kind, txn_date, company_key, cash_amount, bank_amount, "
                    "order_amount, pushed_by_user_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (order["order_key"], kind, day, _default_company_key(cur), cash, bank, amount, user["user_key"]))
        return
    same_total = (row["cash_amount"] or 0) + (row["bank_amount"] or 0) == (amount or 0)
    if row["status"] == "merged" and same_total and cash == bank == 0:
        cash, bank = row["cash_amount"], row["bank_amount"]       # Cash + Bank, same total: keep the split
    cur.execute("UPDATE payment_push SET kind = %s, status = 'awaiting', cash_amount = %s, bank_amount = %s, "
                "order_amount = %s, pushed_by_user_key = %s, pushed_at = now() WHERE push_key = %s",
                (kind, cash, bank, amount, user["user_key"], row["push_key"]))


def withdraw_order(cur, order_key: int) -> str:
    """The cashier takes a confirmation back. Returns '' when done, or why it cannot be (already merged, unchanged)."""
    cur.execute("SELECT push_key, status, merged_at FROM payment_push WHERE order_key = %s FOR UPDATE", (order_key,))
    row = cur.fetchone()
    if not row:
        return ""
    if row["merged_at"] is None:
        cur.execute("DELETE FROM payment_push WHERE push_key = %s", (row["push_key"],))
        return ""
    if row["status"] == "awaiting":      # confirmed again after a change, not merged yet: back to "order changed"
        cur.execute("UPDATE payment_push SET status = 'merged', edited_after_merge = true WHERE push_key = %s",
                    (row["push_key"],))
        return ""
    return "This order is already merged into Payments - correct it there (or edit the order, then confirm again)."


# ------------------------------------------------------------------ the tabs
PUSH_SELECT = """
SELECT p.push_key, p.order_key, p.company_key, p.cash_txn_key, p.bank_txn_key, p.kind, p.status, p.txn_date,
       p.party_name, p.cash_amount, p.bank_amount, p.remarks, p.order_amount, p.pushed_at, p.merged_at,
       p.edited_after_merge, p.merged_kind, p.merged_cash, p.merged_bank,
       c.company_name, o.sl_no, o.dc_inv_no, o.shipping_location, ps.status_name AS payment_status,
       ps.payments_split, pu.display_name AS pushed_by, mu.display_name AS merged_by,
       ct.txn_no AS cash_txn_no, bt.txn_no AS bank_txn_no
FROM payment_push p
JOIN fact_orders o ON o.order_key = p.order_key
LEFT JOIN dim_payment_status ps ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_company c ON c.company_key = p.company_key
LEFT JOIN dim_user pu ON pu.user_key = p.pushed_by_user_key
LEFT JOIN dim_user mu ON mu.user_key = p.merged_by_user_key
LEFT JOIN fact_payment_txn ct ON ct.txn_key = p.cash_txn_key
LEFT JOIN fact_payment_txn bt ON bt.txn_key = p.bank_txn_key
"""


def _num(v):
    return None if v is None else float(v)


def _item(r: dict) -> dict:
    return {"key": r["push_key"], "slNo": r["sl_no"], "kind": r["kind"], "status": r["status"],
            "date": mc.iso(r["txn_date"]) or "", "dcNo": r["dc_inv_no"] or "", "address": r["shipping_location"] or "",
            "paymentStatus": r["payment_status"] or "", "split": r["payments_split"] or "both",
            "orderAmount": _num(r["order_amount"]), "company": r["company_name"] or "", "party": r["party_name"] or "",
            "remarks": r["remarks"] or "", "cash": float(r["cash_amount"]), "bank": float(r["bank_amount"]),
            "pushedBy": r["pushed_by"] or "", "pushedAt": mc.iso(r["pushed_at"]) or "",
            "mergedBy": r["merged_by"] or "", "mergedAt": mc.iso(r["merged_at"]) or "",
            "alreadyMerged": r["merged_at"] is not None, "editedAfterMerge": bool(r["edited_after_merge"]),
            "wasKind": r["merged_kind"] or "", "wasCash": _num(r["merged_cash"]), "wasBank": _num(r["merged_bank"]),
            "txnNos": [n for n in (r["cash_txn_no"], r["bank_txn_no"]) if n]}


@router.get("")
def queue(status: str = Query(default="awaiting"), user=Depends(ANY)):
    """Rows for the Sales / Sales Return tabs (both kinds; the page splits them) and the waiting count per kind."""
    payments._user_gate(user)
    if status not in ("awaiting", "merged"):
        raise HTTPException(422, "status must be awaiting or merged")
    with db.cursor() as cur:
        cur.execute("SELECT kind, count(*) AS n FROM payment_push WHERE status = 'awaiting' GROUP BY kind")
        counts = {r["kind"]: r["n"] for r in cur.fetchall()}
        cur.execute(PUSH_SELECT + " WHERE p.status = %s AND (%s = 'awaiting' "
                    "OR p.merged_at > now() - interval '30 days') ORDER BY p.pushed_at, p.push_key LIMIT 1000",
                    (status, status))
        rows = [_item(r) for r in cur.fetchall()]
    return {"ok": True, "rows": rows, "counts": {"sales": counts.get("sales", 0), "return": counts.get("return", 0)},
            "required": required_fields(), "canWrite": not access.is_view_only(user, *PAGES)}


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str = ""
    company: str = ""
    party: str = ""
    cashAmount: float | str = 0
    bankAmount: float | str = 0
    remarks: str = ""


def _amounts(body: MergeIn) -> tuple[Decimal, Decimal]:
    try:
        cash, bank = _money(body.cashAmount), _money(body.bankAmount)
    except Exception:
        raise HTTPException(422, "Cash and Bank must be amounts.")
    if cash < 0 or bank < 0:
        raise HTTPException(422, "Cash and Bank cannot be negative.")
    return cash, bank


def _load(cur, push_key: int) -> dict:
    cur.execute(PUSH_SELECT + " WHERE p.push_key = %s FOR UPDATE OF p", (push_key,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "This row is no longer waiting - it may have been merged or taken back. "
                                 "Please refresh.")
    return row


def _check(row: dict, body: MergeIn, cash: Decimal, bank: Decimal):
    """Raises 422 with a sentence the tab shows as is."""
    need = {"company": body.company.strip(), "party": body.party.strip(), "remarks": body.remarks.strip()}
    missing = [f for f in required_fields() if not need[f]]
    if missing:
        raise HTTPException(422, "Please fill in: " + ", ".join(missing) + ".")
    if not body.date:
        raise HTTPException(422, "Please choose the date.")
    if cash + bank <= 0:
        raise HTTPException(422, "Enter the Cash and / or Bank amount.")
    split = row["payments_split"]
    if split == "cash" and bank > 0:
        raise HTTPException(422, f"This order's payment is {row['payment_status']}: the whole amount is Cash.")
    if split == "bank" and cash > 0:
        raise HTTPException(422, f"This order's payment is {row['payment_status']}: the whole amount is Bank.")
    if row["order_amount"] is not None and abs(cash + bank - row["order_amount"]) > CENT:
        raise HTTPException(422, f"Cash + Bank must add up to the order amount ({row['order_amount']:,.2f}); "
                                 f"it adds up to {cash + bank:,.2f}.")


@router.put("/{push_key}")
def save_row(push_key: int, body: MergeIn, user=Depends(ANY)):
    """Keep what has been typed so far without merging."""
    payments._user_gate(user)
    payments._can_write(user)
    cash, bank = _amounts(body)
    with db.cursor() as cur:
        _load(cur, push_key)
        ck = mc.lookup_key(cur, "dim_company", "company_key", "company_name", body.company) if body.company else None
        cur.execute("UPDATE payment_push SET txn_date = %s, company_key = %s, party_name = %s, cash_amount = %s, "
                    "bank_amount = %s, remarks = %s WHERE push_key = %s",
                    (mc.parse_date(body.date), ck, body.party.strip()[:150] or None, cash, bank,
                     body.remarks.strip() or None, push_key))
    return {"ok": True, "success": True}


def _txn_fields(cur, row: dict, body: MergeIn, user: dict) -> dict:
    kind = row["kind"]
    type_name = _text(KINDS[kind], "Sales" if kind == "sales" else "Sale Return")
    modes = ("dim_payment_mode", "mode_key", "mode_name")
    out = {"type_key": mc.lookup_key(cur, "dim_txn_type", "txn_type_key", "type_name", type_name),
           "company_key": mc.lookup_key(cur, "dim_company", "company_key", "company_name", body.company),
           "cash_mode": mc.lookup_key(cur, *modes, _text("pay_merge_cash_mode", "Cash")),
           "bank_mode": mc.lookup_key(cur, *modes, _text("pay_merge_bank_mode", "Bank"))}
    if out["type_key"] is None:
        raise HTTPException(422, f"The transaction type '{type_name}' does not exist. "
                                 "An admin can set it in Setup > Module settings.")
    if out["company_key"] is None:
        raise HTTPException(422, "Please choose a company from the list.")
    if None in (out["cash_mode"], out["bank_mode"]):
        raise HTTPException(422, "The Cash / Bank payment modes are not set up. "
                                 "An admin can set them in Setup > Module settings.")
    out["party_key"] = payments._party_key(cur, body.party.strip(), out["company_key"], out["type_key"], user)
    return out


def _write_leg(cur, existing_key, amount: Decimal, mode_key: int, f: dict, row: dict, day: date, body: MergeIn,
               user: dict, today: date):
    """Create, update or (when the amount is now nothing) retire one Payments entry. Returns its txn_key (or None)."""
    account = (row["shipping_location"] or "").strip()[:150] or None
    invoice = (row["dc_inv_no"] or "").strip()[:80] or None
    remarks = body.remarks.strip() or None
    if existing_key is None:
        if amount <= 0:
            return None
        cur.execute("INSERT INTO fact_payment_txn (txn_date_key, txn_type_key, company_key, party_key, mode_key, "
                    "amount, account_name, invoice_no, remarks, status, is_backdated, created_by_user_key) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending',%s,%s) RETURNING txn_key",
                    (mc.date_key(day), f["type_key"], f["company_key"], f["party_key"], mode_key, amount, account,
                     invoice, remarks, day < today, user["user_key"]))
        return cur.fetchone()["txn_key"]
    cur.execute("SELECT status FROM fact_payment_txn WHERE txn_key = %s FOR UPDATE", (existing_key,))
    t = cur.fetchone()
    if t is None:
        return None
    if amount <= 0:      # this part is now nothing: a waiting entry is withdrawn, an approved one is flagged
        if t["status"] == "Pending":
            cur.execute("UPDATE fact_payment_txn SET status = 'Rejected', approved_by_user_key = %s, "
                        "approved_at = now(), order_warning = %s, updated_at = now() WHERE txn_key = %s",
                        (user["user_key"], BECAME_ZERO, existing_key))
        else:
            cur.execute("UPDATE fact_payment_txn SET order_warning = %s, updated_at = now() WHERE txn_key = %s",
                        (BECAME_ZERO + " - please reverse it", existing_key))
        return existing_key
    cur.execute("UPDATE fact_payment_txn SET txn_date_key = %s, txn_type_key = %s, company_key = %s, party_key = %s, "
                "mode_key = %s, amount = %s, account_name = %s, invoice_no = %s, remarks = %s, is_backdated = %s, "
                "updated_at = now(), order_warning = CASE WHEN status = 'Pending' THEN NULL ELSE %s END "
                "WHERE txn_key = %s",
                (mc.date_key(day), f["type_key"], f["company_key"], f["party_key"], mode_key, amount, account,
                 invoice, remarks, day < today, CHANGED, existing_key))
    return existing_key


@router.post("/{push_key}/merge")
def merge_row(push_key: int, body: MergeIn, user=Depends(ANY)):
    payments._user_gate(user)
    payments._can_write(user)
    cash, bank = _amounts(body)
    today = mc.today_ist()
    try:
        with db.cursor() as cur:
            row = _load(cur, push_key)
            _check(row, body, cash, bank)
            day = mc.parse_date(body.date)
            f = _txn_fields(cur, row, body, user)
            cash_key = _write_leg(cur, row["cash_txn_key"], cash, f["cash_mode"], f, row, day, body, user, today)
            bank_key = _write_leg(cur, row["bank_txn_key"], bank, f["bank_mode"], f, row, day, body, user, today)
            cur.execute("UPDATE payment_push SET status = 'merged', txn_date = %s, company_key = %s, party_name = %s, "
                        "cash_amount = %s, bank_amount = %s, remarks = %s, merged_by_user_key = %s, merged_at = now(), "
                        "cash_txn_key = %s, bank_txn_key = %s, merged_kind = %s, merged_cash = %s, merged_bank = %s, "
                        "edited_after_merge = false WHERE push_key = %s",
                        (day, f["company_key"], body.party.strip()[:150] or None, cash, bank,
                         body.remarks.strip() or None, user["user_key"], cash_key, bank_key, row["kind"], cash, bank,
                         push_key))
            cur.execute("SELECT txn_no FROM fact_payment_txn WHERE txn_key = ANY(%s) ORDER BY txn_key",
                        ([k for k in (cash_key, bank_key) if k],))
            nos = [r["txn_no"] for r in cur.fetchall()]
    except HTTPException as e:
        return {"ok": True, "success": False, "message": e.detail}
    return {"ok": True, "success": True, "txnIds": nos}
