# ruff: noqa: F811  (the shared fixtures are imported by name, the usual pytest pattern in this repo)
"""Cashier dashboard -> Payments (app/payment_merge.py): confirmed orders wait in the Sales / Sales Return tabs, the
missing details are filled and merged into Payments (Pending, for the accountant), and an order edited afterwards
updates the same entries and is flagged - an approved entry is never pulled back to Pending."""
import uuid

from app import o2d_screens
from tests.test_o2d_cashier import _cashier, _confirm, _dc, _received
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures)


def _status(client, admin, split):
    """A cashier payment status whose money splits as `split` (cash / bank / both / '')."""
    name = f"PM-{split or 'x'} {uuid.uuid4().hex[:6]}"
    r = client.post("/admin/lookups/payment-statuses", headers=admin,
                    json={"name": name, "fields": {"cashier_review": "yes", "payments_split": split}})
    assert r.status_code == 201, r.text
    return name


def _queue(client, who, status="awaiting"):
    return client.get(f"/payments/merge?status={status}", headers=who).json()


def _row_for(client, who, sl, status="awaiting"):
    return next((r for r in _queue(client, who, status)["rows"] if r["slNo"] == sl), None)


def _merge(client, who, row, **over):
    body = {"date": row["date"], "company": "VT", "party": "", "cashAmount": row["cash"], "bankAmount": row["bank"],
            "remarks": "", **over}
    return client.post(f"/payments/merge/{row['key']}/merge", headers=who, json=body).json()


def _txn(db_conn, txn_no):
    with db_conn.cursor() as cur:
        cur.execute("SELECT t.status, t.amount, t.invoice_no, t.account_name, t.order_warning, tt.type_name, "
                    "m.mode_name "
                    "FROM fact_payment_txn t JOIN dim_txn_type tt ON tt.txn_type_key = t.txn_type_key "
                    "JOIN dim_payment_mode m ON m.mode_key = t.mode_key WHERE t.txn_no = %s", (txn_no,))
        return cur.fetchone()


def test_confirm_stages_the_row_and_merge_creates_a_pending_payment(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    sl, _ = _received(client, db_conn, _status(client, admin, "cash"))
    cashier = _cashier(client, db_conn)
    assert _row_for(client, cashier, sl) is None                       # not pushed until the cashier confirms
    assert _confirm(client, cashier, sl).json()["success"] is True
    row = _row_for(client, cashier, sl)
    assert row["kind"] == "sales" and row["cash"] == 50.0 and row["bank"] == 0 and row["split"] == "cash"
    assert row["orderAmount"] == 50.0 and row["alreadyMerged"] is False
    assert "company" in _queue(client, cashier)["required"]

    r = _merge(client, cashier, row, company="")                       # the company is required (Setup)
    assert r["success"] is False and "company" in r["message"]
    r = _merge(client, cashier, row, bankAmount=10, cashAmount=40)     # a cash status cannot carry a bank part
    assert r["success"] is False
    r = _merge(client, cashier, row, party="Test Party")
    assert r["success"] is True and len(r["txnIds"]) == 1
    t = _txn(db_conn, r["txnIds"][0])
    assert (t[0], float(t[1]), t[5], t[6]) == ("Pending", 50.0, "Sales", "Cash")
    assert t[2]                                                        # the DC / Inv no comes from O2D
    assert _row_for(client, cashier, sl) is None                       # gone from the waiting tab
    assert _row_for(client, cashier, sl, "merged")["alreadyMerged"] is True
    # merged and unchanged: the cashier cannot take the confirmation back from O2D
    assert _confirm(client, cashier, sl, confirmed=False).json()["success"] is False


def test_cash_plus_bank_is_two_payments_that_add_up(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    sl, _ = _received(client, db_conn, _status(client, admin, "both"))
    cashier = _cashier(client, db_conn)
    _confirm(client, cashier, sl)
    row = _row_for(client, cashier, sl)
    assert row["cash"] == 0 and row["bank"] == 0 and row["split"] == "both"
    assert _merge(client, cashier, row, cashAmount=30, bankAmount=10)["success"] is False       # 40 is not 50
    r = _merge(client, cashier, row, cashAmount=30, bankAmount=20)
    assert r["success"] is True and len(r["txnIds"]) == 2
    modes = sorted((_txn(db_conn, n)[6], float(_txn(db_conn, n)[1])) for n in r["txnIds"])
    assert modes == [("Bank", 20.0), ("Cash", 30.0)]


def test_cancelled_order_merges_as_a_sale_return(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    status = _status(client, admin, "cash")
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    sl = add(client, login(client, db_conn, "shop"), today, _dc())["slNo"]
    godown = login(client, db_conn, "godown")
    client.put(f"/o2d/orders/{sl}/godown", headers=godown, json={"deliveryStatus": "Cancelled"})
    recv = {"dateOfReceiving": today.isoformat(), "paymentStatus": status, "amountReceived": 5000}
    sd = login(client, db_conn, "shop_dispatch")
    assert client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json=recv).json()["success"]
    cashier = _cashier(client, db_conn)
    _confirm(client, cashier, sl)
    row = _row_for(client, cashier, sl)
    assert row["kind"] == "return" and row["cash"] == 50.0
    r = _merge(client, cashier, row)
    assert _txn(db_conn, r["txnIds"][0])[5] == "Sale Return"


def test_order_edited_after_merge_updates_the_same_entry_and_only_flags_an_approved_one(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    status = _status(client, admin, "cash")
    sl, sd = _received(client, db_conn, status)
    cashier, account = _cashier(client, db_conn), login(client, db_conn, "accounts")
    _confirm(client, cashier, sl)
    first = _merge(client, cashier, _row_for(client, cashier, sl))
    txn = first["txnIds"][0]
    approve = client.post(f"/payments/transactions/{txn}/decision", headers=account, json={"decision": "Approved"})
    assert approve.json()["success"]

    # the order's amount is corrected in O2D: the confirmation goes, the entry is flagged but stays Approved
    fix = {"paymentStatus": status, "amountReceived": 7000}
    assert client.put(f"/o2d/orders/{sl}/dispatch", headers=sd, json=fix).json()["success"]
    assert _row_for(client, cashier, sl) is None                       # back to Awaiting cashier first
    assert sl in {o["slNo"] for o in client.get("/o2d/cashier", headers=cashier).json()["awaiting"]}
    t = _txn(db_conn, txn)
    assert t[0] == "Approved" and "review" in (t[4] or "")

    assert _confirm(client, cashier, sl).json()["success"] is True      # then it comes back to the tab, tagged
    row = _row_for(client, cashier, sl)
    assert row["alreadyMerged"] is True and row["cash"] == 70.0 and row["txnNos"] == [txn]
    second = _merge(client, cashier, row)
    assert second["txnIds"] == [txn]                                    # the same entry, updated - not a new one
    t = _txn(db_conn, txn)
    assert float(t[1]) == 70.0 and t[0] == "Approved" and t[4]          # still approved, still carries the warning


def test_withdrawing_a_confirmation_removes_its_waiting_row_and_sales_are_not_typed_by_hand(client, db_conn, seed):
    admin = login(client, db_conn, "admin")
    sl, _ = _received(client, db_conn, _status(client, admin, "bank"))
    cashier = _cashier(client, db_conn)
    _confirm(client, cashier, sl)
    assert _row_for(client, cashier, sl) is not None
    assert _confirm(client, cashier, sl, confirmed=False).json()["success"] is True
    assert _row_for(client, cashier, sl) is None
    shop = login(client, db_conn, "shop")
    assert client.get("/payments/merge", headers=shop).status_code == 403          # needs a Payments page
    boot = client.get("/payments/bootstrap", headers=cashier).json()
    assert "Sales" not in boot["entryTypes"] and "Sale Return" not in boot["entryTypes"]
    assert "Sales" in boot["txnTypes"]                                              # still there to filter and edit
