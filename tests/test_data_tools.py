# ruff: noqa: F811
"""Setup > Export & Delete: one Excel per dashboard in a zip; delete removes completed orders and Payments entries only (open orders and
every other dashboard stay), only after a preview and the confirmation word."""
import io
import uuid
import zipfile

from app import o2d_screens
from tests.conftest import bill_no
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)


def _close(db_conn, sl_no):
    """Make an order complete: delivered, received, paid."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_delivery_status (status_name) VALUES ('Delivered') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO dim_payment_status (status_name) VALUES ('Paid') ON CONFLICT DO NOTHING")
        cur.execute("UPDATE fact_orders SET delivery_status_key = (SELECT status_key FROM dim_delivery_status WHERE status_name = 'Delivered'), "
                    "material_delivery_datetime = now(), date_of_receiving_key = order_received_date_key, "
                    "payment_status_key = (SELECT payment_status_key FROM dim_payment_status WHERE status_name = 'Paid') WHERE sl_no = %s", (sl_no,))
    db_conn.commit()


def test_export_is_one_excel_per_dashboard_and_admin_only(client, db_conn, seed):
    admin, shop = login(client, db_conn, "admin"), login(client, db_conn, "shop")
    assert client.get("/admin/data/export.zip", headers=shop).status_code == 403
    r = client.get("/admin/data/export.zip", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == {"O2D_Orders.xlsx", "Payments.xlsx", "Delegation.xlsx", "Purchase.xlsx", "VT_Decor.xlsx", "Attendance.xlsx"}


def test_delete_removes_completed_orders_keeps_open_ones_and_needs_the_word(client, db_conn, seed):
    admin, shop = login(client, db_conn, "admin"), login(client, db_conn, "shop")
    today = o2d_screens.today_ist()
    ensure_date(db_conn, today)
    done = add(client, shop, today, bill_no())["slNo"]
    open_ = add(client, shop, today, bill_no())["slNo"]
    _close(db_conn, done)
    assert client.get("/admin/data/delete-preview", headers=shop).status_code == 403
    pre = client.get("/admin/data/delete-preview", headers=admin).json()
    assert pre["kept_orders"] >= 1 and pre["confirm_word"] == "DELETE"
    assert next(a for a in pre["areas"] if a["label"].startswith("Completed orders"))["count"] >= 1
    assert [a["label"].split(" (")[0] for a in pre["areas"]] == ["Completed orders", "Cashier confirmations waiting for Payments", "Payments entries"]
    # nothing is deleted without the word
    assert client.post("/admin/data/delete", json={"confirm": "yes"}, headers=admin).status_code == 422
    assert client.post("/admin/data/delete", json={"confirm": "DELETE"}, headers=shop).status_code == 403
    with db_conn.cursor() as cur:   # another dashboard's data must survive
        cur.execute("INSERT INTO dim_att_employee (employee_name) VALUES (%s)", ("Keeper " + uuid.uuid4().hex[:6],))
        cur.execute("INSERT INTO fact_attendance (employee_key, work_date, check_in_time) "
                    "SELECT max(employee_key), current_date, '09:00' FROM dim_att_employee")
    db_conn.commit()
    r = client.post("/admin/data/delete", json={"confirm": "DELETE"}, headers=admin)
    assert r.status_code == 200 and r.json()["ok"] is True
    with db_conn.cursor() as cur:
        cur.execute("SELECT sl_no FROM fact_orders WHERE sl_no = ANY(%s)", ([done, open_],))
        assert [x[0] for x in cur.fetchall()] == [open_]                     # the completed one is gone from the table, the open one stays
        cur.execute("SELECT count(*) FROM fact_payment_txn")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM fact_attendance")
        assert cur.fetchone()[0] >= 1                                        # attendance (and the other dashboards) are not touched
        cur.execute("SELECT count(*) FROM dim_user")
        assert cur.fetchone()[0] >= 2                                         # members are never touched
        cur.execute("SELECT count(*) FROM admin_audit_log WHERE action = 'data.delete_all'")
        assert cur.fetchone()[0] == 1
