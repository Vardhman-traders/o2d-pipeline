"""Fill a LOCAL database with made-up but realistic data, so every screen has something to show.

    python scripts/seed_demo.py                      # uses DATABASE_URL (must be a local database)
    python scripts/run_local.py                      # does this for you on an empty local database

Adds: the usual lookup lists (with a few deliberate spelling mix-ups so the Reconciliation screen has work to do),
people, one demo user per role, and about 160 orders spread over the last 60 days in every stage: waiting for
godown, waiting for dispatch, waiting for receiving, closed and cancelled. Nothing here is real.
"""
import os
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import auth  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DEMO_PASSWORD = "Demo-Pass-123"
ROLES = ("shop", "godown", "godown_dispatch", "shop_dispatch", "receiving", "cashier")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CHANNELS = ("Call", "Walk-in", "Online", "Company")
SUBMISSIONS = ("Challan", "Invoice", "Cancelled", "Challan ")          # trailing space: merged by the database
DELIVERY = ("Shop", "Delivered", "Cancelled", "Delivred", "Shop Delivery")   # two typos on purpose
PAYMENT = ("Paid", "Pending", "Part Paid", "paid")
PEOPLE = {"ready_by": ("Ravi", "Ravee", "Sunil", "Manoj"), "colour_making": ("Sonu", "Sonu K", "Deepak"),
          "delivery": ("Amit", "Amit Kumar", "Bablu", "Suresh", "Sures")}
PLACES = ("Rohini", "Pitampura", "Karol Bagh", "Janakpuri", "Laxmi Nagar", "Dwarka", "Shalimar Bagh", "Mayapuri")


def ensure_local(url):
    if urlparse(url).hostname not in LOCAL_HOSTS:
        sys.exit("Refusing to add demo data: DATABASE_URL is not a local database.")


def date_key(d):
    return int(d.strftime("%Y%m%d"))


def seed(url, count=160):
    ensure_local(url)
    rnd = random.Random(7)
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM fact_orders")
    if cur.fetchone()[0]:
        conn.close()
        return False

    today = date.today()
    for i in range(-150, 10):
        d = today + timedelta(days=i)
        cur.execute("INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (date_key(d), d, d.day, d.month, d.year, d.strftime("%A"), d.weekday() == 0))
    lookups = (("dim_order_channel", "channel_name", CHANNELS), ("dim_submission_type", "type_name", SUBMISSIONS),
               ("dim_delivery_status", "status_name", DELIVERY), ("dim_payment_status", "status_name", PAYMENT))
    ids: dict[str, dict[str, int]] = {}
    for table, col, names in lookups:
        ids[table] = {}
        for n in names:
            cur.execute(f"INSERT INTO {table} ({col}) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1", (n,))
            cur.execute(f"SELECT * FROM {table} WHERE lower(btrim({col})) = %s", (n.strip().lower(),))
            ids[table][n.strip()] = cur.fetchone()[0]
    people: dict[str, list[int]] = {}
    for role, names in PEOPLE.items():
        people[role] = []
        for n in names:
            cur.execute("INSERT INTO dim_person (full_name, person_role, phone_number) VALUES (%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (n, role, "98" + str(rnd.randint(10**7, 10**8 - 1))))
            cur.execute("SELECT person_key FROM dim_person WHERE lower(btrim(full_name)) = %s AND person_role = %s",
                        (n.lower(), role))
            people[role].append(cur.fetchone()[0])

    users = {}
    for role in ROLES:
        cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                    "VALUES (%s,%s,%s,%s,false) ON CONFLICT DO NOTHING",
                    ("demo_" + role, auth.hash_password(DEMO_PASSWORD), role, "Demo " + role.replace("_", " ").title()))
        cur.execute("SELECT user_key FROM dim_user WHERE username = %s", ("demo_" + role,))
        users[role] = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(max(sl_no), 0) FROM fact_orders")
    sl = cur.fetchone()[0]
    fresh = ids["dim_delivery_status"]
    stages = ["godown"] * 3 + ["dispatch"] * 3 + ["receiving"] * 3 + ["closed"] * 8 + ["cancelled"]
    for _ in range(count):
        sl += 1
        ordered = today - timedelta(days=rnd.randint(0, 60))
        created = datetime.combine(ordered, time(rnd.randint(9, 18), rnd.randint(0, 59)), tzinfo=IST)
        stage = rnd.choice(stages)
        row = {"sl_no": sl, "dc_inv_no": f"DEMO-{sl:04d}", "order_received_date_key": date_key(ordered),
               "order_via_key": ids["dim_order_channel"][rnd.choice(CHANNELS)],
               "submission_type_key": ids["dim_submission_type"][rnd.choice(("Challan", "Challan", "Invoice"))],
               "created_by_user_key": users["shop"], "shipping_location": rnd.choice(PLACES),
               "detailed_remarks": rnd.choice(("", "", "urgent", "call before delivery", "fragile")),
               "timestamp_created": created, "is_cancelled": False}
        if stage == "cancelled":
            row.update(delivery_status_key=fresh["Cancelled"], is_cancelled=True,
                       last_updated_by_user_key=users["godown"], last_updated_at=created + timedelta(hours=1))
        elif stage != "godown":
            status = rnd.choice(("Shop", "Delivered", "Delivered", "Delivred" if rnd.random() < .1 else "Delivered"))
            row.update(delivery_status_key=fresh[status], ready_by_person_key=rnd.choice(people["ready_by"]),
                       colour_making_person_key=rnd.choice(people["colour_making"]),
                       last_updated_by_user_key=users["godown"], last_updated_at=created + timedelta(hours=2))
            if stage in ("receiving", "closed"):
                delivered = created + timedelta(hours=rnd.randint(3, 60))
                row.update(material_delivery_datetime=delivered, delivered_by_person_key=rnd.choice(people["delivery"]),
                           cartage=rnd.choice((0, 50, 100, 150)), last_updated_by_user_key=users["godown_dispatch"],
                           last_updated_at=delivered)
            if stage == "closed":
                received = min(delivered.date() + timedelta(days=rnd.randint(0, 3)), today)
                row.update(date_of_receiving_key=date_key(received),
                           payment_status_key=ids["dim_payment_status"][rnd.choice(("Paid", "Paid", "Part Paid"))],
                           amount_received=rnd.randint(8, 400) * 50, last_updated_by_user_key=users["receiving"])
        cols = ", ".join(row)
        cur.execute(f"INSERT INTO fact_orders ({cols}) VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))
    conn.commit()
    conn.close()
    return True


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL") or sys.exit("DATABASE_URL is not set.")
    print("Added demo data." if seed(url) else "The database already has orders; left it alone.")
    print(f"Demo users: demo_<role> (shop, godown, godown_dispatch, shop_dispatch, receiving, cashier), "
          f"password {DEMO_PASSWORD}")
