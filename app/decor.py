"""VT Decor module: site-work clients (wallpaper, texture, paint), what was contracted, what was received stage by stage,
what was spent, photos and challans, quotations, and the GP (profit) picture.

How it works (this replaces the Google Apps Script "Code.gs"; the page is the original one):
  - the page makes one call per button, always POST /decor/rpc {action, payload}; each action below is the script's function of
    the same name;
  - who the person is comes from the sign-in only; the role the page sends is ignored. DATA VISIBILITY: an administrator sees
    everything; every other person (Owner page or Staff page alike) sees only what they created themselves - the clients they
    added or were assigned (with those clients' payments, expenses, items, photos) and the quotations they saved. Nobody sees anyone else's.
    The Owner page only adds the edit/delete/revise buttons on the person's own clients. Business-wide screens (ledger, monthly
    overheads, stage percentages) are administrator-only;
  - money is stored in rupees (the page divides what is typed by 100, as before);
  - product types, their stages and the share each stage should bring (Wallpaper 40/30/20/10), payment modes and the expense
    suggestions are Setup lists, not code;
  - photos / challans go to private storage (R2); without storage the expense is still saved and the photo is skipped.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import Json
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, db, module_admin, modcommon as mc, roles

router = APIRouter(prefix="/decor", tags=["decor"])
ANY = auth.require_roles(*roles.ALL_ROLES)
OWNER_PAGE, STAFF_PAGE = "decor_owner", "decor_staff"
MONEY_FIELDS = ("rent", "salaries", "lift", "electricity", "marketing", "software", "ca_fees", "director_salaries", "guard",
                "hidden", "other")
READ_ACTIONS = {"getConfig", "getClients", "getClientDetail", "getItems", "getPaymentsList", "getExpensesList", "getAgingReport",
                "getMonthlyCollection", "getGPData", "getFixedExp", "getStagePcts", "getQuotations", "getNextPerformaNo",
                "getPhotos", "getAllPhotos", "getAllBusinessLedger", "getPartyLedger"}
OWNER_ONLY = {"deletePayment", "editPayment", "deleteExpense", "editExpense", "deleteItem", "reviseAmount", "markCompleted",
              "markActive", "hideClient", "deleteClient", "deletePhoto"}
ADMIN_ONLY = {"getAllBusinessLedger", "saveFixedExp", "getFixedExp", "saveStagePcts"}   # whole-business data: administrators only


# ------------------------------------------------------------------ who is calling
def _who(user) -> dict:
    """{'role': 'owner' | <sign-in name> (which buttons the page shows), 'me': this person's own key in added_by / created_by,
    'admin': sees everything, 'edit': bool} - or a plain refusal when the person holds neither page."""
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    held = access.effective_access(user)
    admin = user["role"] == "admin"
    uname = user["username"].strip().lower()
    me = "owner" if admin else (uname if uname != "owner" else "owner~")   # "owner" is reserved for administrators
    if admin or OWNER_PAGE in held:
        return {"role": "owner", "me": me, "admin": admin, "edit": admin or held.get(OWNER_PAGE) == "edit", "name": user["display_name"]}
    if STAFF_PAGE in held:
        return {"role": uname, "me": me, "admin": False, "edit": held[STAFF_PAGE] == "edit", "name": user["display_name"]}
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _d(v) -> str:
    return v.strftime("%d-%m-%Y") if v else ""


def _dt(v) -> str:
    return v.astimezone(mc.IST).strftime("%d-%m-%Y %H:%M") if v else ""


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(v) -> date:
    """The page sends 'yyyy-mm-dd' (or nothing = today)."""
    return mc.parse_date(v, "Date") or mc.today_ist()


def _visible(who: dict, client: dict) -> bool:
    """An administrator sees every client; anyone else only the clients they added or that were assigned to them."""
    return who["admin"] or who["me"] in ((client["added_by"] or "").strip().lower(), (client["assigned_to"] or "").strip().lower())


def _client_row(cur, code: str) -> dict | None:
    cur.execute("""SELECT c.*, t.type_name, t.percent_stages FROM dim_decor_client c
                   JOIN dim_decor_product_type t ON t.type_key = c.product_type_key WHERE c.client_code = %s""", (str(code or "").strip(),))
    return cur.fetchone()


def _need_client(cur, who, code) -> dict:
    row = _client_row(cur, code)
    if not row:
        raise HTTPException(404, f"Client not found: {code}")
    if not _visible(who, row):
        raise HTTPException(403, "Access denied")
    return row


def _mode_key(cur, name) -> int | None:
    return mc.lookup_key(cur, "dim_payment_mode", "mode_key", "mode_name", name or "Cash")


def _stage_key(cur, type_key: int, name: str | None) -> int | None:
    if not name:
        return None
    cur.execute("SELECT stage_key FROM dim_decor_stage WHERE product_type_key = %s AND lower(btrim(stage_name)) = lower(btrim(%s))",
                (type_key, name))
    r = cur.fetchone()
    return r["stage_key"] if r else None


# ------------------------------------------------------------------ the one entry point
class RpcIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    payload: dict = {}


@router.post("/rpc")
def rpc(body: RpcIn, user=Depends(ANY)):
    who = _who(user)
    fn = ACTIONS.get(body.action)
    if not fn:
        return {"ok": False, "msg": "Unknown action: " + body.action}
    if body.action not in READ_ACTIONS and not who["edit"]:
        return {"ok": False, "msg": "You have view-only access to this page, so you cannot save changes here."}
    if (body.action in OWNER_ONLY and who["role"] != "owner") or (body.action in ADMIN_ONLY and not who["admin"]):
        return {"ok": False, "msg": "Not authorized"}
    try:
        return fn(user, who, body.payload or {})
    except HTTPException as e:
        return {"ok": False, "msg": e.detail}


@router.get("/me")
def me(user=Depends(ANY)):
    who = _who(user)
    return {"ok": True, "role": who["role"], "name": who["name"], "edit": who["edit"], "admin": who["admin"]}


# ------------------------------------------------------------------ setup lists the page needs
def _setting(key: str) -> dict:
    return next(s for s in module_admin.SETTINGS if s["key"] == key)


def get_config(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT type_key, type_name, percent_stages FROM dim_decor_product_type ORDER BY sort_order, lower(type_name)")
        types = cur.fetchall()
        cur.execute("SELECT product_type_key, stage_name, target_pct FROM dim_decor_stage ORDER BY sort_order, stage_key")
        stages = cur.fetchall()
        cur.execute("SELECT mode_name FROM dim_payment_mode ORDER BY sort_order, lower(mode_name)")
        modes = [r["mode_name"] for r in cur.fetchall()]
        cur.execute("SELECT expense_type_name FROM dim_decor_expense_type ORDER BY sort_order, lower(expense_type_name)")
        exp = [r["expense_type_name"] for r in cur.fetchall()]
    staff = [{"id": "owner", "name": "Owner"}] if who["admin"] else [{"id": who["me"], "name": who["name"]}]   # nobody else's name
    ids = (set(access.users_with_page(OWNER_PAGE)) | set(access.users_with_page(STAFF_PAGE))) if who["admin"] else set()
    if ids:
        with db.cursor() as cur:
            cur.execute("SELECT username, display_name FROM dim_user WHERE user_key = ANY(%s) ORDER BY lower(display_name)", (list(ids),))
            staff += [{"id": r["username"].strip().lower(), "name": r["display_name"]} for r in cur.fetchall()
                      if r["username"].strip().lower() != "owner"]
    return {"ok": True, "role": who["role"], "name": who["name"], "admin": who["admin"],
            "productTypes": [{"name": t["type_name"], "percent": t["percent_stages"],
                              "stages": [{"name": s["stage_name"], "pct": s["target_pct"]} for s in stages
                                         if s["product_type_key"] == t["type_key"]]} for t in types],
            "paymentModes": modes, "expenseTypes": exp, "staff": staff, "photosEnabled": attachments.configured(),
            **{out: str(module_admin.setting_value(_setting(key))) for out, key in (
                ("statementNote", "decor_statement_note"), ("companyAddress", "decor_company_address"),
                ("companyContact", "decor_company_contact"), ("bankDetails", "decor_bank_details"), ("quoteTerms", "decor_quote_terms"))}}


# ------------------------------------------------------------------ clients
def _client_obj(c: dict, paid: float) -> dict:
    amount = _f(c["contract_amount"])
    return {"clientId": c["client_code"], "name": c["client_name"], "mobile": c["mobile"], "address": c["address"] or "",
            "product": c["product"] or "", "amount": amount, "date": _d(c["start_date"]), "notes": c["notes"] or "",
            "status": c["status"], "assignedTo": c["assigned_to"] or "", "addedBy": c["added_by"] or "",
            "productType": c["type_name"], "paid": paid, "balance": amount - paid}


def _paid_map(cur) -> dict[int, float]:
    cur.execute("SELECT client_key, sum(amount) AS s FROM fact_decor_payment GROUP BY client_key")
    return {r["client_key"]: _f(r["s"]) for r in cur.fetchall()}


CLIENT_SQL = ("SELECT c.*, t.type_name, t.percent_stages FROM dim_decor_client c "
              "JOIN dim_decor_product_type t ON t.type_key = c.product_type_key ORDER BY c.client_key")


def get_clients(user, who, p):
    with db.cursor() as cur:
        cur.execute(CLIENT_SQL)
        rows = cur.fetchall()
        paid = _paid_map(cur)
    out = {"Active": [], "Completed": [], "Hidden": []}
    for c in rows:
        if _visible(who, c):
            out[c["status"]].append(_client_obj(c, paid.get(c["client_key"], 0.0)))
    return {"ok": True, "active": out["Active"], "completed": out["Completed"], "hidden": out["Hidden"]}


def add_client(user, who, p):
    name, mobile = str(p.get("name") or "").strip(), str(p.get("mobile") or "").strip()
    if not name or not mobile:
        return {"ok": False, "msg": "Name and mobile required"}
    with db.cursor() as cur:
        cur.execute("SELECT type_key FROM dim_decor_product_type WHERE lower(btrim(type_name)) = lower(btrim(%s))", (p.get("productType") or "",))
        t = cur.fetchone()
        if not t:
            cur.execute("SELECT type_key FROM dim_decor_product_type ORDER BY sort_order, type_key LIMIT 1")
            t = cur.fetchone()
        if not t:
            return {"ok": False, "msg": "No product types are set up yet. Ask the administrator (Setup > Dropdown values)."}
        cur.execute("""INSERT INTO dim_decor_client (client_name, mobile, address, product, contract_amount, start_date, notes,
                       assigned_to, added_by, product_type_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING client_code""",
                    (name[:150], mobile[:30], p.get("address") or None, p.get("product") or None, round(_f(p.get("amount")), 2),
                     _day(p.get("date")), p.get("notes") or None, who["me"], who["me"], t["type_key"]))
        return {"ok": True, "clientId": cur.fetchone()["client_code"]}


def _set_status(status):
    def run(user, who, p):
        with db.cursor() as cur:
            c = _need_client(cur, who, p.get("clientId"))
            cur.execute("UPDATE dim_decor_client SET status = %s WHERE client_key = %s", (status, c["client_key"]))
        return {"ok": True}
    return run


def delete_client(user, who, p):
    code = str(p.get("clientId") or "").strip()
    with db.cursor() as cur:
        c = _need_client(cur, who, code)
        cur.execute("SELECT attachment_key, object_key FROM file_attachment WHERE module = 'decor' AND record_key = %s", (c["client_key"],))
        files = cur.fetchall()
        cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) VALUES (%s, %s, 'decor.delete_client', %s, %s)",
                    (user["user_key"], user["username"], code, Json({"name": c["client_name"], "contract": _f(c["contract_amount"])})))
        cur.execute("DELETE FROM file_attachment WHERE module = 'decor' AND record_key = %s", (c["client_key"],))
        cur.execute("DELETE FROM dim_decor_client WHERE client_key = %s", (c["client_key"],))
    _drop_objects(files)
    return {"ok": True}


def _drop_objects(files):
    if not files or not attachments.configured():
        return
    for f in files:
        try:
            attachments.storage().delete_object(Bucket=attachments._bucket(), Key=f["object_key"])
        except Exception:   # the row is gone either way; an orphaned object is harmless
            pass


# ------------------------------------------------------------------ one client in full
def _stage_rows(cur, c: dict, payments: list[dict], amount: float) -> list[dict]:
    cur.execute("SELECT stage_name, target_pct FROM dim_decor_stage WHERE product_type_key = %s ORDER BY sort_order, stage_key", (c["product_type_key"],))
    got: dict[str, float] = {}
    for pm in payments:
        got[pm["stage"]] = got.get(pm["stage"], 0.0) + pm["amount"]
    out = []
    for s in cur.fetchall():
        target = amount * s["target_pct"] / 100 if c["percent_stages"] else 0.0
        received = got.get(s["stage_name"], 0.0)
        if received == 0:
            status = "Pending"
        elif c["percent_stages"] and target > 0:
            status = "Received" if received >= target else "Partial"
        else:
            status = "Received"
        out.append({"stage": s["stage_name"], "target": target, "received": received, "status": status})
    return out


def _payments(cur, client_key: int) -> list[dict]:
    cur.execute("""SELECT p.payment_key, p.created_at, c.client_code, c.client_name, COALESCE(s.stage_name, p.stage_text, '') AS stage,
                          p.amount, COALESCE(m.mode_name, '') AS mode, p.pay_date, p.ref_no, p.remarks
                   FROM fact_decor_payment p JOIN dim_decor_client c ON c.client_key = p.client_key
                   LEFT JOIN dim_decor_stage s ON s.stage_key = p.stage_key LEFT JOIN dim_payment_mode m ON m.mode_key = p.mode_key
                   WHERE p.client_key = %s ORDER BY p.payment_key""", (client_key,))
    return [{"rowIndex": r["payment_key"], "timestamp": _dt(r["created_at"]), "clientId": r["client_code"], "clientName": r["client_name"],
             "stage": r["stage"], "amount": _f(r["amount"]), "mode": r["mode"], "date": _d(r["pay_date"]), "pnum": r["ref_no"] or "",
             "remarks": r["remarks"] or ""} for r in cur.fetchall()]


def _expenses(cur, client_key: int) -> list[dict]:
    cur.execute("""SELECT e.expense_key, e.created_at, c.client_code, c.client_name, e.expense_type, e.amount, e.exp_date, e.notes
                   FROM fact_decor_expense e JOIN dim_decor_client c ON c.client_key = e.client_key
                   WHERE e.client_key = %s ORDER BY e.expense_key""", (client_key,))
    return [{"rowIndex": r["expense_key"], "timestamp": _dt(r["created_at"]), "clientId": r["client_code"], "clientName": r["client_name"],
             "type": r["expense_type"], "amount": _f(r["amount"]), "date": _d(r["exp_date"]), "notes": r["notes"] or ""} for r in cur.fetchall()]


def _items(cur, client_key: int) -> list[dict]:
    cur.execute("SELECT item_key, item_index, product, amount, item_date, notes FROM fact_decor_item WHERE client_key = %s ORDER BY item_key", (client_key,))
    return [{"rowIndex": r["item_key"], "product": r["product"], "amount": _f(r["amount"]), "date": _d(r["item_date"]),
             "notes": r["notes"] or "", "itemIndex": r["item_index"]} for r in cur.fetchall()]


def get_client_detail(user, who, p):
    code = str(p.get("clientId") or "").strip()
    if not code:
        return {"ok": False, "msg": "Client ID required"}
    with db.cursor() as cur:
        c = _need_client(cur, who, code)
        pays, exps, items = _payments(cur, c["client_key"]), _expenses(cur, c["client_key"]), _items(cur, c["client_key"])
        amount = _f(c["contract_amount"])
        stages = _stage_rows(cur, c, pays, amount)
    paid, spent = sum(x["amount"] for x in pays), sum(x["amount"] for x in exps)
    client = _client_obj(c, paid)
    client.update({"totalPaid": paid, "totalExpenses": spent, "balance": amount - paid, "grossProfit": paid - spent})
    return {"ok": True, "client": client, "payments": pays, "expenses": exps, "items": items, "stages": stages}


# ------------------------------------------------------------------ payments / expenses / items
def add_payment(user, who, p):
    amount = _f(p.get("amount"))
    if not p.get("clientId") or not amount:
        return {"ok": False, "msg": "clientId and amount required"}
    with db.cursor() as cur:
        c = _need_client(cur, who, p["clientId"])
        sk = _stage_key(cur, c["product_type_key"], p.get("stage"))
        cur.execute("INSERT INTO fact_decor_payment (client_key, stage_key, stage_text, amount, mode_key, pay_date, ref_no, remarks, created_by_user_key) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (c["client_key"], sk, None if sk else (p.get("stage") or None), round(amount, 2), _mode_key(cur, p.get("mode")),
                     _day(p.get("date")), p.get("pnum") or None, p.get("remarks") or None, user["user_key"]))
    return {"ok": True}


def get_payments_list(user, who, p):
    with db.cursor() as cur:
        c = _need_client(cur, who, p.get("clientId"))
        return {"ok": True, "list": [{k: x[k] for k in ("rowIndex", "stage", "amount", "mode", "date", "pnum", "remarks")}
                                     for x in _payments(cur, c["client_key"])]}


def _own_row(cur, who, table: str, key_col: str, row_id, code) -> dict:
    cur.execute(f'SELECT t.*, c.client_code, c.added_by, c.assigned_to FROM "{table}" t JOIN dim_decor_client c ON c.client_key = t.client_key WHERE t."{key_col}" = %s',
                (int(row_id or 0),))
    r = cur.fetchone()
    if not r or not _visible(who, r):   # somebody else's row looks exactly like a missing one
        raise HTTPException(404, "Row not found")
    if r["client_code"] != str(code or "").strip():
        raise HTTPException(422, "Row does not belong to this client")
    return r


def delete_payment(user, who, p):
    with db.cursor() as cur:
        _own_row(cur, who, "fact_decor_payment", "payment_key", p.get("rowIndex"), p.get("clientId"))
        cur.execute("DELETE FROM fact_decor_payment WHERE payment_key = %s", (int(p["rowIndex"]),))
    return {"ok": True}


def edit_payment(user, who, p):
    amount = _f(p.get("amount"))
    if not amount:
        return {"ok": False, "msg": "Amount required"}
    with db.cursor() as cur:
        cur.execute("SELECT p.payment_key, c.product_type_key, c.added_by, c.assigned_to FROM fact_decor_payment p JOIN dim_decor_client c ON c.client_key = p.client_key "
                    "WHERE p.payment_key = %s", (int(p.get("rowIndex") or 0),))
        r = cur.fetchone()
        if not r or not _visible(who, r):
            return {"ok": False, "msg": "Invalid row index"}
        sk = _stage_key(cur, r["product_type_key"], p.get("stage"))
        cur.execute("UPDATE fact_decor_payment SET stage_key = %s, stage_text = %s, amount = %s, mode_key = %s, pay_date = %s, ref_no = %s, remarks = %s "
                    "WHERE payment_key = %s", (sk, None if sk else (p.get("stage") or None), round(amount, 2), _mode_key(cur, p.get("mode")),
                                               _day(p.get("date")), p.get("pnum") or None, p.get("remarks") or None, r["payment_key"]))
    return {"ok": True}


def add_expense(user, who, p):
    amount = _f(p.get("amount"))
    if not p.get("clientId") or not amount:
        return {"ok": False, "msg": "clientId and amount required"}
    return _save_expense(user, who, p, amount)


def _save_expense(user, who, p, amount) -> dict:
    with db.cursor() as cur:
        c = _need_client(cur, who, p["clientId"])
        cur.execute("INSERT INTO fact_decor_expense (client_key, expense_type, amount, exp_date, notes, created_by_user_key) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING expense_key",
                    (c["client_key"], str(p.get("type") or "")[:100], round(amount, 2), _day(p.get("date")), p.get("notes") or None, user["user_key"]))
    return {"ok": True, "clientKey": c["client_key"]}


def add_expense_with_photo(user, who, p):
    if not p.get("clientId"):
        return {"ok": False, "msg": "clientId required"}
    if not str(p.get("type") or "").strip():
        return {"ok": False, "msg": "Expense type required"}
    amount = _f(p.get("amount"))
    if not amount:
        return {"ok": False, "msg": "Amount required"}
    res = _save_expense(user, who, p, amount)
    if not (p.get("base64") and p.get("mimeType")):
        return {"ok": True}
    if not attachments.configured():
        return {"ok": True, "photoSkipped": True, "msg": "Expense saved. Photo skipped: photo storage is not set up yet."}
    try:
        caption = "[Challan] " + p["type"] + (" - " + p["notes"] if p.get("notes") else "")
        _store_photo(user, res["clientKey"], "Expense Challan", caption, "challan", p["base64"], p["mimeType"])
    except HTTPException as e:
        return {"ok": True, "photoSkipped": True, "msg": f"Expense saved. Photo upload failed: {e.detail}"}
    return {"ok": True}


def _store_photo(user, client_key, stage, caption, kind, b64, mime) -> int:
    aid = attachments.put_data_url("decor", client_key, kind, user, f"data:{mime};base64,{b64}")
    with db.cursor() as cur:
        cur.execute("INSERT INTO fact_decor_photo (client_key, stage_text, caption, kind, attachment_key, created_by_user_key) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING photo_key", (client_key, stage or None, caption or None, kind, aid, user["user_key"]))
        return cur.fetchone()["photo_key"]


def get_expenses_list(user, who, p):
    with db.cursor() as cur:
        c = _need_client(cur, who, p.get("clientId"))
        return {"ok": True, "list": [{k: x[k] for k in ("rowIndex", "type", "amount", "date", "notes")} for x in _expenses(cur, c["client_key"])]}


def delete_expense(user, who, p):
    with db.cursor() as cur:
        _own_row(cur, who, "fact_decor_expense", "expense_key", p.get("rowIndex"), p.get("clientId"))
        cur.execute("DELETE FROM fact_decor_expense WHERE expense_key = %s", (int(p["rowIndex"]),))
    return {"ok": True}


def edit_expense(user, who, p):
    amount = _f(p.get("amount"))
    if not amount:
        return {"ok": False, "msg": "Amount required"}
    with db.cursor() as cur:
        cur.execute("SELECT e.expense_key, c.added_by, c.assigned_to FROM fact_decor_expense e JOIN dim_decor_client c ON c.client_key = e.client_key "
                    "WHERE e.expense_key = %s", (int(p.get("rowIndex") or 0),))
        r = cur.fetchone()
        if not r or not _visible(who, r):
            return {"ok": False, "msg": "Invalid row index"}
        cur.execute("UPDATE fact_decor_expense SET expense_type = %s, amount = %s, exp_date = %s, notes = %s WHERE expense_key = %s",
                    (str(p.get("type") or "")[:100], round(amount, 2), _day(p.get("date")), p.get("notes") or None, r["expense_key"]))
    return {"ok": True}


def add_item(user, who, p):
    if not p.get("clientId") or not str(p.get("product") or "").strip():
        return {"ok": False, "msg": "clientId and product required"}
    amount = round(_f(p.get("amount")), 2)
    with db.cursor() as cur:
        c = _need_client(cur, who, p["clientId"])
        cur.execute("SELECT COALESCE(max(item_index), 0) + 1 AS n FROM fact_decor_item WHERE client_key = %s", (c["client_key"],))
        cur.execute("INSERT INTO fact_decor_item (client_key, item_index, product, amount, item_date, notes, created_by_user_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (c["client_key"], cur.fetchone()["n"], p["product"], amount, _day(p.get("date")), p.get("notes") or None, user["user_key"]))
        cur.execute("UPDATE dim_decor_client SET contract_amount = contract_amount + %s WHERE client_key = %s", (amount, c["client_key"]))
    return {"ok": True}


def delete_item(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT i.item_key, c.added_by, c.assigned_to FROM fact_decor_item i JOIN dim_decor_client c ON c.client_key = i.client_key "
                    "WHERE i.item_key = %s", (int(p.get("rowIndex") or 0),))
        r = cur.fetchone()
        if not r or not _visible(who, r):
            return {"ok": False, "msg": "Invalid row index"}
        cur.execute("DELETE FROM fact_decor_item WHERE item_key = %s", (r["item_key"],))
    return {"ok": True}


def get_items(user, who, p):
    with db.cursor() as cur:
        c = _need_client(cur, who, p.get("clientId"))
        return {"ok": True, "items": _items(cur, c["client_key"])}


def revise_amount(user, who, p):
    new = round(_f(p.get("newAmount")), 2)
    if not p.get("clientId") or not new:
        return {"ok": False, "msg": "clientId and newAmount required"}
    with db.cursor() as cur:
        c = _need_client(cur, who, p["clientId"])
        cur.execute("INSERT INTO fact_decor_revision (client_key, old_amount, new_amount, reason, created_by_user_key) VALUES (%s,%s,%s,%s,%s)",
                    (c["client_key"], c["contract_amount"], new, p.get("reason") or None, user["user_key"]))
        cur.execute("UPDATE dim_decor_client SET contract_amount = %s WHERE client_key = %s", (new, c["client_key"]))
    return {"ok": True}


# ------------------------------------------------------------------ photos
def _signed(attachment_keys: list[int]) -> dict[int, str]:
    if not attachment_keys or not attachments.configured():
        return {}
    with db.cursor() as cur:
        cur.execute("SELECT attachment_key, object_key FROM file_attachment WHERE attachment_key = ANY(%s)", (attachment_keys,))
        rows = cur.fetchall()
    return {r["attachment_key"]: attachments.storage().generate_presigned_url(
        "get_object", Params={"Bucket": attachments._bucket(), "Key": r["object_key"]}, ExpiresIn=attachments.LINK_SECONDS) for r in rows}


def _photo_url(r: dict, urls: dict) -> str:
    return urls.get(r["attachment_key"]) or r["legacy_url"] or ""


def save_photo_link(user, who, p):
    if not p.get("clientId") or not p.get("photoUrl"):
        return {"ok": False, "msg": "clientId and photoUrl required"}
    with db.cursor() as cur:
        c = _need_client(cur, who, p["clientId"])
        cur.execute("INSERT INTO fact_decor_photo (client_key, stage_text, caption, legacy_url, created_by_user_key) VALUES (%s,%s,%s,%s,%s)",
                    (c["client_key"], p.get("stage") or None, p.get("caption") or None, str(p["photoUrl"])[:1000], user["user_key"]))
    return {"ok": True}


def upload_photo(user, who, p):
    if not attachments.configured():
        return {"ok": False, "msg": "Photo storage is not set up yet. Ask the administrator."}
    if not (p.get("base64") and p.get("mimeType")):
        return {"ok": False, "msg": "No photo received."}
    with db.cursor() as cur:
        c = _need_client(cur, who, p.get("clientId"))
    pk = _store_photo(user, c["client_key"], p.get("stage"), p.get("caption"), "photo", p["base64"], p["mimeType"])
    with db.cursor() as cur:
        cur.execute("SELECT attachment_key, legacy_url FROM fact_decor_photo WHERE photo_key = %s", (pk,))
        r = cur.fetchone()
    return {"ok": True, "photoUrl": _photo_url(r, _signed([r["attachment_key"]])), "fileId": str(pk)}


PHOTO_SQL = """SELECT f.photo_key, f.client_key, f.created_at, c.client_code, c.client_name, c.assigned_to, c.added_by, c.status, t.type_name,
                      f.stage_text, f.caption, f.attachment_key, f.legacy_url
               FROM fact_decor_photo f JOIN dim_decor_client c ON c.client_key = f.client_key
               JOIN dim_decor_product_type t ON t.type_key = c.product_type_key"""


def get_photos(user, who, p):
    with db.cursor() as cur:
        c = _need_client(cur, who, p.get("clientId"))
        cur.execute(PHOTO_SQL + " WHERE f.client_key = %s ORDER BY f.photo_key", (c["client_key"],))
        rows = cur.fetchall()
    urls = _signed([r["attachment_key"] for r in rows if r["attachment_key"]])
    return {"ok": True, "photos": [{"rowIndex": r["photo_key"], "stage": r["stage_text"] or "", "caption": r["caption"] or "",
                                    "url": _photo_url(r, urls), "fileId": str(r["photo_key"])} for r in rows]}


def get_all_photos(user, who, p):
    with db.cursor() as cur:
        cur.execute(PHOTO_SQL + " ORDER BY f.photo_key DESC")
        rows = [r for r in cur.fetchall() if _visible(who, r)]
    urls = _signed([r["attachment_key"] for r in rows if r["attachment_key"]])
    photos = [{"rowIndex": r["photo_key"], "timestamp": _dt(r["created_at"]), "clientId": r["client_code"], "clientName": r["client_name"],
               "stage": r["stage_text"] or "", "caption": r["caption"] or "", "url": _photo_url(r, urls), "fileId": str(r["photo_key"]),
               "assignedTo": (r["assigned_to"] or "").strip().lower(), "productType": r["type_name"], "status": r["status"]}
              for r in rows if _photo_url(r, urls)]
    return {"ok": True, "photos": photos, "total": len(photos)}


def delete_photo(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT f.photo_key, f.attachment_key, c.added_by, c.assigned_to FROM fact_decor_photo f JOIN dim_decor_client c ON c.client_key = f.client_key "
                    "WHERE f.photo_key = %s AND c.client_code = %s", (int(p.get("fileId") or 0), str(p.get("clientId") or "").strip()))
        r = cur.fetchone()
        if not r or not _visible(who, r):
            return {"ok": False, "msg": "Photo not found"}
        cur.execute("DELETE FROM fact_decor_photo WHERE photo_key = %s", (r["photo_key"],))
        files = []
        if r["attachment_key"]:
            cur.execute("DELETE FROM file_attachment WHERE attachment_key = %s RETURNING object_key", (r["attachment_key"],))
            files = cur.fetchall()
    _drop_objects(files)
    return {"ok": True}


# ------------------------------------------------------------------ reports
def get_aging_report(user, who, p):
    today = mc.today_ist()
    buckets = {"b0": [], "b30": [], "b60": [], "b90": []}
    with db.cursor() as cur:
        cur.execute(CLIENT_SQL)
        rows = cur.fetchall()
        paid = _paid_map(cur)
    for c in rows:
        if c["status"] != "Active" or not _visible(who, c):
            continue
        got = paid.get(c["client_key"], 0.0)
        balance = _f(c["contract_amount"]) - got
        if balance <= 0:
            continue
        days = (today - c["start_date"]).days
        obj = {"clientId": c["client_code"], "name": c["client_name"], "mobile": c["mobile"], "amount": _f(c["contract_amount"]),
               "paid": got, "balance": balance, "days": days, "assignedTo": c["assigned_to"] or ""}
        buckets["b0" if days <= 30 else "b30" if days <= 60 else "b60" if days <= 90 else "b90"].append(obj)
    return {"ok": True, "buckets": buckets}


def get_monthly_collection(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT to_char(p.pay_date, 'YYYY-MM') AS ym, p.amount, c.added_by, c.assigned_to FROM fact_decor_payment p "
                    "JOIN dim_decor_client c ON c.client_key = p.client_key")
        rows = cur.fetchall()
    monthly: dict[str, float] = {}
    for r in rows:
        if not _visible(who, r):
            continue
        monthly[r["ym"]] = monthly.get(r["ym"], 0.0) + _f(r["amount"])
    return {"ok": True, "monthly": monthly}


def get_gp_data(user, who, p):
    month, year = int(p.get("month") or 0), int(p.get("year") or 0)
    with db.cursor() as cur:
        cur.execute(CLIENT_SQL)
        rows = cur.fetchall()
        paid = _paid_map(cur)
        cur.execute("SELECT client_key, sum(amount) AS s FROM fact_decor_expense GROUP BY client_key")
        spent = {r["client_key"]: _f(r["s"]) for r in cur.fetchall()}
    sites = []
    for c in rows:
        if not _visible(who, c):
            continue
        if month and year and (c["start_date"].month != month or c["start_date"].year != year):
            continue
        got, out = paid.get(c["client_key"], 0.0), spent.get(c["client_key"], 0.0)
        gp = got - out
        sites.append({"clientId": c["client_code"], "name": c["client_name"], "revenue": _f(c["contract_amount"]), "paid": got,
                      "expenses": out, "gp": gp, "gpPct": f"{gp / got * 100:.1f}" if got > 0 else "0.0", "productType": c["type_name"]})
    return {"ok": True, "sites": sites}


def get_business_ledger(user, who, p):
    with db.cursor() as cur:
        cur.execute(CLIENT_SQL)
        rows = cur.fetchall()
        paid = _paid_map(cur)
        cur.execute("SELECT client_key, sum(amount) AS s FROM fact_decor_expense GROUP BY client_key")
        spent = {r["client_key"]: _f(r["s"]) for r in cur.fetchall()}
        cur.execute("""SELECT p.client_key, p.pay_date, COALESCE(s.stage_name, p.stage_text, '') AS stage, p.amount, COALESCE(m.mode_name, '') AS mode
                       FROM fact_decor_payment p LEFT JOIN dim_decor_stage s ON s.stage_key = p.stage_key
                       LEFT JOIN dim_payment_mode m ON m.mode_key = p.mode_key ORDER BY p.payment_key""")
        last: dict[int, list] = {}
        for r in cur.fetchall():
            last.setdefault(r["client_key"], []).append({"date": _d(r["pay_date"]), "stage": r["stage"], "amount": _f(r["amount"]), "mode": r["mode"]})
    sites, tc, tr, te = [], 0.0, 0.0, 0.0
    for c in rows:
        if c["status"] == "Hidden":
            continue
        contract, got, out = _f(c["contract_amount"]), paid.get(c["client_key"], 0.0), spent.get(c["client_key"], 0.0)
        balance = contract - got
        tc, tr, te = tc + contract, tr + got, te + out
        sites.append({"clientId": c["client_code"], "name": c["client_name"], "mobile": c["mobile"], "address": c["address"] or "",
                      "productType": c["type_name"], "date": _d(c["start_date"]), "status": c["status"], "assignedTo": c["assigned_to"] or "",
                      "contract": contract, "received": got, "expenses": out, "balance": balance, "profit": got - out,
                      "payStatus": "Unpaid" if got == 0 else ("Paid" if balance <= 0 else "Partial"),
                      "last3": last.get(c["client_key"], [])[-3:]})
    return {"ok": True, "sites": sites, "totalContract": tc, "totalReceived": tr, "totalExpenses": te, "totalBalance": tc - tr, "totalProfit": tr - te}


# ------------------------------------------------------------------ fixed expenses, stage percentages
def save_fixed_exp(user, who, p):
    month, year = int(p.get("month") or 0), int(p.get("year") or 0)
    if not (1 <= month <= 12 and year):
        return {"ok": False, "msg": "Month and year required"}
    amounts = {k: round(_f(p.get(k)), 2) for k in MONEY_FIELDS}
    with db.cursor() as cur:
        cur.execute("INSERT INTO decor_fixed_expense (month, year, amounts, notes) VALUES (%s,%s,%s,%s) ON CONFLICT (month, year) "
                    "DO UPDATE SET amounts = EXCLUDED.amounts, notes = EXCLUDED.notes, updated_at = now()",
                    (month, year, Json(amounts), p.get("notes") or None))
    return {"ok": True}


def get_fixed_exp(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT amounts, notes FROM decor_fixed_expense WHERE month = %s AND year = %s", (int(p.get("month") or 0), int(p.get("year") or 0)))
        r = cur.fetchone()
    if not r:
        return {"ok": True, "data": None}
    return {"ok": True, "data": {"key": f"{p.get('month')}-{p.get('year')}", **{k: r["amounts"].get(k, 0) for k in MONEY_FIELDS}, "notes": r["notes"] or ""}}


def _pct_stages(cur) -> list[dict]:
    """The first four stages of the first type that splits the contract by percentage (Wallpaper)."""
    cur.execute("""SELECT s.stage_key, s.target_pct FROM dim_decor_stage s JOIN dim_decor_product_type t ON t.type_key = s.product_type_key
                   WHERE t.type_key = (SELECT type_key FROM dim_decor_product_type WHERE percent_stages ORDER BY sort_order, type_key LIMIT 1)
                   ORDER BY s.sort_order, s.stage_key LIMIT 4""")
    return cur.fetchall()


def get_stage_pcts(user, who, p):
    with db.cursor() as cur:
        st = _pct_stages(cur)
    vals = [s["target_pct"] for s in st] + [0] * 4
    return {"ok": True, "p1": vals[0], "p2": vals[1], "p3": vals[2], "p4": vals[3]}


def save_stage_pcts(user, who, p):
    vals = [int(_f(p.get(k))) for k in ("p1", "p2", "p3", "p4")]
    if any(v < 0 or v > 100 for v in vals) or sum(vals) > 100:
        return {"ok": False, "msg": "Percentages must be between 0 and 100 and add up to at most 100."}
    with db.cursor() as cur:
        for s, v in zip(_pct_stages(cur), vals):
            cur.execute("UPDATE dim_decor_stage SET target_pct = %s WHERE stage_key = %s", (v, s["stage_key"]))
    return {"ok": True}


# ------------------------------------------------------------------ quotations
def _next_no(cur) -> str:
    cur.execute("SELECT COALESCE(max(substring(performa_no from '^VT-(\\d+)$')::int), 0) + 1 AS n FROM fact_decor_quotation WHERE performa_no ~ '^VT-\\d+$'")
    return "VT-" + str(cur.fetchone()["n"]).zfill(4)


def get_next_performa_no(user, who, p):
    with db.cursor() as cur:
        return {"ok": True, "next": _next_no(cur)}


def save_quotation(user, who, p):
    items = p.get("items") if isinstance(p.get("items"), list) else []
    with db.cursor() as cur:
        no = str(p.get("performa_no") or "").strip()
        cur.execute("SELECT 1 FROM fact_decor_quotation WHERE performa_no = %s", (no,))
        if not no or cur.fetchone():   # two people saving at once: the later one gets the next free number
            no = _next_no(cur)
        cur.execute("""INSERT INTO fact_decor_quotation (performa_no, created_by, client_name, mobile, address, gstin, quote_date, subtotal,
                       cgst_pct, sgst_pct, grand_total, items) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (no, who["me"], p.get("client") or None, p.get("mobile") or None, p.get("addr") or None, p.get("gstin") or None,
                     str(p.get("date") or "")[:20] or None, round(_f(p.get("subtotal")), 2), _f(p.get("cgst_pct")), _f(p.get("sgst_pct")),
                     round(_f(p.get("grand")), 2), Json(items)))
    return {"ok": True, "performaNo": no}


def get_quotations(user, who, p):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM fact_decor_quotation ORDER BY quotation_key")
        rows = cur.fetchall()
    return {"ok": True, "list": [{"timestamp": _d(r["created_at"].astimezone(mc.IST)), "performaNo": r["performa_no"], "createdBy": r["created_by"] or "",
                                  "client": r["client_name"] or "", "mobile": r["mobile"] or "", "addr": r["address"] or "", "gstin": r["gstin"] or "",
                                  "date": r["quote_date"] or "", "subtotal": _f(r["subtotal"]), "cgstPct": _f(r["cgst_pct"]),
                                  "sgstPct": _f(r["sgst_pct"]), "grand": _f(r["grand_total"]), "rowIndex": r["quotation_key"]}
                                 for r in rows if who["admin"] or (r["created_by"] or "") == who["me"]]}


ACTIONS = {
    "getConfig": get_config, "getClients": get_clients, "addClient": add_client, "addPayment": add_payment,
    "addExpense": add_expense, "addExpenseWithPhoto": add_expense_with_photo, "getClientDetail": get_client_detail,
    "reviseAmount": revise_amount, "addItem": add_item, "deleteItem": delete_item, "getItems": get_items,
    "deletePayment": delete_payment, "editPayment": edit_payment, "getPaymentsList": get_payments_list,
    "getExpensesList": get_expenses_list, "deleteExpense": delete_expense, "editExpense": edit_expense,
    "getAgingReport": get_aging_report, "getMonthlyCollection": get_monthly_collection, "getGPData": get_gp_data,
    "saveFixedExp": save_fixed_exp, "getFixedExp": get_fixed_exp, "getStagePcts": get_stage_pcts, "saveStagePcts": save_stage_pcts,
    "saveQuotation": save_quotation, "getQuotations": get_quotations, "getNextPerformaNo": get_next_performa_no,
    "markCompleted": _set_status("Completed"), "markActive": _set_status("Active"), "hideClient": _set_status("Hidden"),
    "deleteClient": delete_client, "savePhotoLink": save_photo_link, "uploadPhoto": upload_photo, "getPhotos": get_photos,
    "getAllPhotos": get_all_photos, "deletePhoto": delete_photo, "getAllBusinessLedger": get_business_ledger,
    "getPartyLedger": get_client_detail, "fixDuplicates": lambda u, w, p: {"ok": True, "fixed": 0},
}
