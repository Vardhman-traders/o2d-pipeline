"""Add a handful of deliberately odd orders and near-duplicate names to a LOCAL database, so the
dashboard, filters and the Reconciliation screen all have unusual-looking things to poke at.

    python scripts/seed_weird_demo.py      # uses DATABASE_URL (must be a local database)

Nothing here is real. Safe to run more than once; it skips anything it already added.
"""
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IST = ZoneInfo("Asia/Kolkata")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MARKER = "WEIRD-"   # every DC/Inv number this script adds starts with this, so it's easy to find/remove


def ensure_local(url):
    if urlparse(url).hostname not in LOCAL_HOSTS:
        sys.exit("Refusing to add demo data: DATABASE_URL is not a local database.")


def date_key(d):
    return int(d.strftime("%Y%m%d"))


def seed(url):
    ensure_local(url)
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM fact_orders WHERE dc_inv_no LIKE %s LIMIT 1", (MARKER + "%",))
    if cur.fetchone():
        conn.close()
        return False

    today = date.today()
    for i in range(-5, 1):  # make sure today and a few recent days exist even on a stale demo DB
        d = today + timedelta(days=i)
        cur.execute("INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (date_key(d), d, d.day, d.month, d.year, d.strftime("%A"), d.weekday() == 0))

    cur.execute("SELECT channel_key FROM dim_order_channel LIMIT 1")
    channel_key = cur.fetchone()[0]
    cur.execute("SELECT submission_type_key FROM dim_submission_type LIMIT 1")
    submission_key = cur.fetchone()[0]
    cur.execute("SELECT status_key FROM dim_delivery_status WHERE status_name = 'Delivered'")
    row = cur.fetchone()
    delivered_key = row[0] if row else None
    cur.execute("SELECT payment_status_key FROM dim_payment_status LIMIT 1")
    payment_key = cur.fetchone()[0]
    cur.execute("SELECT user_key FROM dim_user WHERE role = 'shop' LIMIT 1")
    row = cur.fetchone()
    user_key = row[0] if row else None

    # A cluster of near-duplicate spellings for the same real person, for the "Clean up names" screen.
    weird_names = ["Ramesh", "Ramesh ", "ramesh", "Ramehs", "RAMESH KUMAR"]
    person_keys = []
    for n in weird_names:
        cur.execute("INSERT INTO dim_person (full_name, person_role, phone_number) VALUES (%s, 'ready_by', %s) "
                    "ON CONFLICT DO NOTHING", (n, "9" + "9" * 9))
        cur.execute("SELECT person_key FROM dim_person WHERE full_name = %s AND person_role = 'ready_by'", (n,))
        row = cur.fetchone()
        if row:
            person_keys.append(row[0])

    cur.execute("SELECT COALESCE(max(sl_no), 0) FROM fact_orders")
    sl = cur.fetchone()[0]

    def insert(suffix, **overrides):
        nonlocal sl
        sl += 1
        row = {
            "sl_no": sl, "dc_inv_no": f"{MARKER}{suffix}",
            "order_received_date_key": date_key(today),
            "order_via_key": channel_key, "submission_type_key": submission_key,
            "created_by_user_key": user_key, "shipping_location": "Test location with a very very very "
                "long shipping address that goes on and on to see how the screen wraps it, Delhi",
            "detailed_remarks": "", "timestamp_created": datetime.combine(today, time(10, 0), tzinfo=IST),
            "is_cancelled": False,
        }
        row.update(overrides)
        cols = ", ".join(row)
        cur.execute(f"INSERT INTO fact_orders ({cols}) VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))

    # Odd-looking values: zero, a very large amount, a blank DC number pattern, a same-day order+delivery,
    # an order with no remarks/location at all, and the near-duplicate-name cluster above.
    insert("ZERO-AMOUNT", delivery_status_key=delivered_key, payment_status_key=payment_key,
           date_of_receiving_key=date_key(today), amount_received=0, cartage=0,
           material_delivery_datetime=datetime.combine(today, time(11, 0), tzinfo=IST))
    insert("HUGE-AMOUNT", delivery_status_key=delivered_key, payment_status_key=payment_key,
           date_of_receiving_key=date_key(today), amount_received=9999999.99, cartage=50000,
           material_delivery_datetime=datetime.combine(today, time(11, 0), tzinfo=IST))
    insert("SAME-DAY", delivery_status_key=delivered_key, payment_status_key=payment_key,
           date_of_receiving_key=date_key(today), amount_received=500, cartage=0,
           material_delivery_datetime=datetime.combine(today, time(10, 5), tzinfo=IST))  # delivered 5 min after order
    insert("NO-LOCATION", shipping_location="", detailed_remarks=None)
    for i, pk in enumerate(person_keys):
        insert(f"NAME-{i}", ready_by_person_key=pk, delivery_status_key=delivered_key,
               payment_status_key=payment_key, date_of_receiving_key=date_key(today), amount_received=100)

    conn.commit()
    conn.close()
    return True


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL") or sys.exit("DATABASE_URL is not set.")
    print("Added weird demo data." if seed(url) else "Weird demo data is already there; left it alone.")
