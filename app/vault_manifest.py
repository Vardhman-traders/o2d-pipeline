# ruff: noqa: E501
"""What the other side shows after unlocking: dashboards, lists and detail pages, as DATA that travels inside every encrypted export.

The viewer is generic: it knows how to draw a number card, a bar chart, a line chart, a table, a searchable list and a detail page, and nothing
about orders or payments. Everything business-specific (names, queries, labels, layout) is here, is written into the encrypted file, and is
therefore only visible after unlocking. The queries run read-only, in the browser, over the exported SQLite copy (SQLite dialect, money
columns are text so they are cast for sums).

Block types:  kpis {items:[{label, sql, format}]} (sql returns one value) | bars {title, sql(label,value), format} | line {title, sql(x,y), format}
              table {title, sql, columns:[{name,label,format}]} | list tab {sql, columns, search, order, key, detail:{sections}}
Formats:      int | money | hours | pct | date | datetime | text
Detail sections (":key" is the clicked row's key):  fields {sql(one row), formats} | table {sql, columns} | files {sql(id,label), source}
"""
import json

MANIFEST_VERSION = 1


def col(name, label=None, fmt="text", hidden=False):
    return {"name": name, "label": label or name.replace("_", " ").capitalize(), "format": fmt, **({"hidden": True} if hidden else {})}


def kpi(label, sql, fmt="int"):
    return {"label": label, "sql": sql, "format": fmt}


def bars(title, sql, fmt="int"):
    return {"type": "bars", "title": title, "sql": sql, "format": fmt}


def line(title, sql, fmt="int"):
    return {"type": "line", "title": title, "sql": sql, "format": fmt}


def table(title, sql, columns):
    return {"type": "table", "title": title, "sql": sql, "columns": columns}


def _by(field, label_sql=None):
    return f"COALESCE({label_sql or field}, '(none)')"


ORDERS = "v_orders_archive"
_IN_OUT = "JOIN dim_txn_type y ON y.txn_type_key = t.txn_type_key"


def build() -> dict:
    order_kpis = {"type": "kpis", "items": [
        kpi("Orders", f"SELECT count(*) FROM {ORDERS}"),
        kpi("Cancelled", f"SELECT count(*) FROM {ORDERS} WHERE stage = 'Cancelled'"),
        kpi("Delivered", f"SELECT count(*) FROM {ORDERS} WHERE material_delivery_datetime IS NOT NULL AND stage <> 'Cancelled'"),
        kpi("Closed", f"SELECT count(*) FROM {ORDERS} WHERE stage = 'Closed'"),
        kpi("Amount received", f"SELECT COALESCE(SUM(CAST(amount_received AS REAL)), 0) FROM {ORDERS}", "money"),
        kpi("Cartage", f"SELECT COALESCE(SUM(CAST(cartage AS REAL)), 0) FROM {ORDERS}", "money"),
        kpi("Average hours to deliver", f"SELECT AVG(CAST(hours_to_deliver AS REAL)) FROM {ORDERS} WHERE hours_to_deliver IS NOT NULL", "hours"),
        kpi("Delivered within 24 hours", f"SELECT 100.0 * SUM(CASE WHEN CAST(hours_to_deliver AS REAL) <= 24 THEN 1 ELSE 0 END) / NULLIF(COUNT(hours_to_deliver), 0) FROM {ORDERS} WHERE hours_to_deliver IS NOT NULL", "pct"),
    ]}

    def count_by(title, column):
        return bars(title, f"SELECT {_by(column)} AS label, count(*) AS value FROM {ORDERS} GROUP BY 1 ORDER BY 2 DESC LIMIT 12")

    orders_tab = {"id": "orders", "title": "Orders", "blocks": [
        order_kpis,
        line("Orders per day (latest 60 days)", f"SELECT order_date AS x, count(*) AS y FROM {ORDERS} WHERE order_date IS NOT NULL GROUP BY order_date ORDER BY order_date DESC LIMIT 60"),
        line("Amount received per day (latest 60 days)", f"SELECT order_date AS x, SUM(CAST(amount_received AS REAL)) AS y FROM {ORDERS} WHERE order_date IS NOT NULL GROUP BY order_date ORDER BY order_date DESC LIMIT 60", "money"),
        count_by("Orders by stage", "stage"), count_by("Orders by delivery status", "delivery_status"), count_by("Orders by payment status", "payment_status"),
        count_by("Orders by channel", "channel"), count_by("Orders by type", "submission_type"),
        table("Latest orders", f"SELECT sl_no, dc_inv_no, order_date, stage, delivery_status, payment_status, amount_received FROM {ORDERS} ORDER BY order_date DESC, sl_no DESC LIMIT 15",
              [col("sl_no", "Sl no", "int"), col("dc_inv_no", "Bill no"), col("order_date", "Date", "date"), col("stage"), col("delivery_status", "Delivery"), col("payment_status", "Payment"), col("amount_received", "Amount", "money")]),
    ]}

    order_columns = [col("order_key", "key", "int", hidden=True), col("sl_no", "Sl no", "int"), col("dc_inv_no", "Bill no"), col("order_date", "Date", "date"), col("channel"),
                     col("submission_type", "Type"), col("delivery_status", "Delivery"), col("payment_status", "Payment"), col("stage"),
                     col("amount_received", "Amount", "money"), col("cartage", "Cartage", "money"), col("shipping_location", hidden=True), col("detailed_remarks", hidden=True),
                     col("ready_by", hidden=True), col("delivered_by", hidden=True), col("created_by", hidden=True)]
    orders_list = {"id": "all_orders", "title": "All orders", "list": {
        "sql": "SELECT " + ", ".join(c["name"] for c in order_columns) + f" FROM {ORDERS}", "columns": order_columns, "key": "order_key",
        "search": ["sl_no", "dc_inv_no", "channel", "submission_type", "delivery_status", "payment_status", "stage", "shipping_location", "detailed_remarks", "ready_by", "delivered_by", "created_by"],
        "order": "order_date DESC, sl_no DESC",
        "detail": {"title": "Order", "sections": [
            {"type": "fields", "title": "Order", "sql": f"SELECT * FROM {ORDERS} WHERE order_key = :key",
             "formats": {"amount_received": "money", "cartage": "money", "amount_received_entered": "money", "hours_to_deliver": "hours", "order_date": "date",
                         "timestamp_created": "datetime", "material_delivery_datetime": "datetime", "last_updated_at": "datetime", "date_of_receiving": "date", "archived_at": "datetime"},
             "hide": ["order_key", "is_cancelled", "delivered_by_phone", "delivered_by_detail"]},
            {"type": "table", "title": "History", "sql": "SELECT at, event_type, title, by_name, by_role, changes FROM order_event WHERE order_key = :key ORDER BY at",
             "columns": [col("at", "When", "datetime"), col("event_type", "Event"), col("title"), col("by_name", "By"), col("by_role", "Role"), col("changes")]},
            {"type": "table", "title": "Payments sent to accounts", "sql": "SELECT txn_date, kind, status, party_name, cash_amount, bank_amount, order_amount, remarks FROM payment_push WHERE order_key = :key ORDER BY push_key",
             "columns": [col("txn_date", "Date", "date"), col("kind"), col("status"), col("party_name", "Party"), col("cash_amount", "Cash", "money"), col("bank_amount", "Bank", "money"), col("order_amount", "Order amount", "money"), col("remarks")]},
            {"type": "files", "title": "Photos and documents", "source": "order_attachments", "sql": "SELECT attachment_key AS id, kind || ' · ' || COALESCE(uploaded_by, '') AS label FROM order_attachments WHERE order_key = :key ORDER BY attachment_key"},
        ]}}}

    paid = f"SELECT SUM(CAST(t.amount AS REAL)) FROM fact_payment_txn t {_IN_OUT} WHERE t.status = 'Approved' AND y.direction = "
    money_kpis = {"type": "kpis", "items": [
        kpi("Payment entries", "SELECT count(*) FROM fact_payment_txn"),
        kpi("Waiting for approval", "SELECT count(*) FROM fact_payment_txn WHERE status = 'Pending'"),
        kpi("Money in (approved)", "SELECT COALESCE((" + paid + "'in'), 0)", "money"),
        kpi("Money out (approved)", "SELECT COALESCE((" + paid + "'out'), 0)", "money"),
        kpi("Expenses", "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM fact_expense_entry WHERE voided_at IS NULL AND kind <> 'Topup'", "money"),
        kpi("Purchases", "SELECT COALESCE(SUM(CAST(invoice_amount AS REAL)), 0) FROM fact_purchase_entry", "money"),
        kpi("VT Decor received", "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM fact_decor_payment", "money"),
        kpi("VT Decor spent", "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM fact_decor_expense", "money"),
    ]}
    per_month = "SELECT substr(d.full_date, 1, 7) AS x, SUM(CAST({amt} AS REAL)) AS y FROM {src} JOIN dim_date d ON d.date_key = {datekey} {where} GROUP BY 1 ORDER BY 1 DESC LIMIT 24"
    money_tab = {"id": "money", "title": "Money", "blocks": [
        money_kpis,
        line("Money in per month (approved)", per_month.format(amt="t.amount", src="fact_payment_txn t " + _IN_OUT, datekey="t.txn_date_key", where="WHERE t.status = 'Approved' AND y.direction = 'in'"), "money"),
        line("Money out per month (approved)", per_month.format(amt="t.amount", src="fact_payment_txn t " + _IN_OUT, datekey="t.txn_date_key", where="WHERE t.status = 'Approved' AND y.direction = 'out'"), "money"),
        bars("Payments by type (amount)", "SELECT y.type_name AS label, SUM(CAST(t.amount AS REAL)) AS value FROM fact_payment_txn t " + _IN_OUT + " WHERE t.status = 'Approved' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        bars("Payments by mode (amount)", "SELECT COALESCE(m.mode_name, '(none)') AS label, SUM(CAST(t.amount AS REAL)) AS value FROM fact_payment_txn t LEFT JOIN dim_payment_mode m ON m.mode_key = t.mode_key WHERE t.status = 'Approved' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        bars("Payments by company (amount)", "SELECT COALESCE(c.company_name, '(none)') AS label, SUM(CAST(t.amount AS REAL)) AS value FROM fact_payment_txn t LEFT JOIN dim_company c ON c.company_key = t.company_key WHERE t.status = 'Approved' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        line("Expenses per month", per_month.format(amt="e.amount", src="fact_expense_entry e", datekey="e.entry_date_key", where="WHERE e.voided_at IS NULL AND e.kind <> 'Topup'"), "money"),
        bars("Expenses by category", "SELECT COALESCE(c.category_name, '(none)') AS label, SUM(CAST(e.amount AS REAL)) AS value FROM fact_expense_entry e LEFT JOIN dim_expense_category c ON c.category_key = e.category_key WHERE e.voided_at IS NULL AND e.kind <> 'Topup' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        line("Purchases per month", per_month.format(amt="p.invoice_amount", src="fact_purchase_entry p", datekey="p.invoice_date_key", where=""), "money"),
        bars("Purchases by vendor", "SELECT COALESCE(v.party_name, '(none)') AS label, SUM(CAST(p.invoice_amount AS REAL)) AS value FROM fact_purchase_entry p LEFT JOIN dim_party v ON v.party_key = p.vendor_party_key GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        bars("Purchases by site", "SELECT COALESCE(s.site_name, '(none)') AS label, SUM(CAST(p.invoice_amount AS REAL)) AS value FROM fact_purchase_entry p LEFT JOIN dim_purchase_site s ON s.site_key = p.site_key GROUP BY 1 ORDER BY 2 DESC", "money"),
        bars("VT Decor received by client", "SELECT COALESCE(c.client_name, '(none)') AS label, SUM(CAST(p.amount AS REAL)) AS value FROM fact_decor_payment p LEFT JOIN dim_decor_client c ON c.client_key = p.client_key GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
        bars("VT Decor spent by type", "SELECT COALESCE(expense_type, '(none)') AS label, SUM(CAST(amount AS REAL)) AS value FROM fact_decor_expense GROUP BY 1 ORDER BY 2 DESC LIMIT 12", "money"),
    ]}

    def list_tab(tab_id, title, sql, columns, search, order, detail=None):
        return {"id": tab_id, "title": title, "list": {"sql": sql, "columns": columns, "search": search, "order": order, "key": None, **({"detail": detail} if detail else {})}}

    payments = list_tab(
        "payments", "Payments",
        "SELECT t.txn_no, d.full_date AS txn_date, y.type_name AS type, c.company_name AS company, p.party_name AS party, m.mode_name AS mode, t.amount, t.status, t.invoice_no, t.account_name, t.remarks "
        "FROM fact_payment_txn t LEFT JOIN dim_date d ON d.date_key = t.txn_date_key LEFT JOIN dim_txn_type y ON y.txn_type_key = t.txn_type_key LEFT JOIN dim_company c ON c.company_key = t.company_key "
        "LEFT JOIN dim_party p ON p.party_key = t.party_key LEFT JOIN dim_payment_mode m ON m.mode_key = t.mode_key",
        [col("txn_no", "Txn no"), col("txn_date", "Date", "date"), col("type"), col("company"), col("party"), col("mode"), col("amount", "Amount", "money"), col("status"), col("invoice_no", "Invoice"), col("account_name", "Account"), col("remarks")],
        ["txn_no", "type", "company", "party", "mode", "status", "invoice_no", "account_name", "remarks"], "txn_date DESC, txn_no DESC")
    expenses = list_tab(
        "expenses", "Expenses",
        "SELECT e.entry_no, d.full_date AS entry_date, a.account_name AS account, e.kind, e.amount, c.category_name AS category, e.paid_to, e.description, e.voided_at "
        "FROM fact_expense_entry e LEFT JOIN dim_date d ON d.date_key = e.entry_date_key LEFT JOIN dim_expense_account a ON a.account_key = e.account_key LEFT JOIN dim_expense_category c ON c.category_key = e.category_key",
        [col("entry_no", "Entry no"), col("entry_date", "Date", "date"), col("account"), col("kind"), col("amount", "Amount", "money"), col("category"), col("paid_to", "Paid to"), col("description"), col("voided_at", "Voided", "datetime")],
        ["entry_no", "account", "kind", "category", "paid_to", "description"], "entry_date DESC, entry_no DESC")
    purchases = list_tab(
        "purchases", "Purchases",
        "SELECT p.purchase_key, s.site_name AS site, v.party_name AS vendor, p.invoice_no, d.full_date AS invoice_date, p.invoice_amount, p.remarks "
        "FROM fact_purchase_entry p LEFT JOIN dim_purchase_site s ON s.site_key = p.site_key LEFT JOIN dim_party v ON v.party_key = p.vendor_party_key LEFT JOIN dim_date d ON d.date_key = p.invoice_date_key",
        [col("purchase_key", "Id", "int"), col("site"), col("vendor"), col("invoice_no", "Invoice"), col("invoice_date", "Date", "date"), col("invoice_amount", "Amount", "money"), col("remarks")],
        ["site", "vendor", "invoice_no", "remarks"], "invoice_date DESC, purchase_key DESC")
    decor = list_tab(
        "decor", "VT Decor clients",
        "SELECT c.client_code, c.client_name, c.product, c.status, c.contract_amount, "
        "COALESCE((SELECT SUM(CAST(p.amount AS REAL)) FROM fact_decor_payment p WHERE p.client_key = c.client_key), 0) AS received, "
        "COALESCE((SELECT SUM(CAST(e.amount AS REAL)) FROM fact_decor_expense e WHERE e.client_key = c.client_key), 0) AS spent, c.client_key FROM dim_decor_client c",
        [col("client_code", "Code"), col("client_name", "Client"), col("product"), col("status"), col("contract_amount", "Contract", "money"), col("received", "Received", "money"), col("spent", "Spent", "money"), col("client_key", "key", "int", hidden=True)],
        ["client_code", "client_name", "product", "status"], "client_name",
        {"title": "VT Decor client", "sections": [
            {"type": "fields", "title": "Client", "sql": "SELECT client_code, client_name, mobile, address, product, contract_amount, start_date, status, notes FROM dim_decor_client WHERE client_key = :key",
             "formats": {"contract_amount": "money", "start_date": "date"}},
            {"type": "table", "title": "Payments received", "sql": "SELECT pay_date, stage_text, amount, ref_no, remarks FROM fact_decor_payment WHERE client_key = :key ORDER BY pay_date",
             "columns": [col("pay_date", "Date", "date"), col("stage_text", "Stage"), col("amount", "Amount", "money"), col("ref_no", "Ref"), col("remarks")]},
            {"type": "table", "title": "Expenses", "sql": "SELECT exp_date, expense_type, amount, notes FROM fact_decor_expense WHERE client_key = :key ORDER BY exp_date",
             "columns": [col("exp_date", "Date", "date"), col("expense_type", "Type"), col("amount", "Amount", "money"), col("notes")]},
            {"type": "files", "title": "Photos", "source": "file_attachment",
             "sql": "SELECT a.attachment_key AS id, COALESCE(ph.stage_text, a.kind) AS label FROM fact_decor_photo ph JOIN file_attachment a ON a.attachment_key = ph.attachment_key WHERE ph.client_key = :key ORDER BY a.attachment_key"},
        ]})

    return {"version": MANIFEST_VERSION, "title": "Business data", "tabs": [orders_tab, orders_list, money_tab, payments, expenses, purchases, decor]}


def build_json() -> str:
    return json.dumps(build(), separators=(",", ":"))
