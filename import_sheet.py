"""One-off import of the Google Sheets export (data/orders.csv, data/users.csv) into Postgres.

    python import_sheet.py             # dry run: does everything, then rolls back
    python import_sheet.py --commit    # writes for real

Refuses to run if fact_orders already contains rows.
"""
import csv
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import bcrypt
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).parent
DATA = ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")  # sheet timestamps are assumed to be India local time
load_dotenv(ROOT / ".env")

PHONE_RE = re.compile(r"\d{10}")


def blank(v):
    v = (v or "").strip()
    return v or None


def norm(v):
    return " ".join(v.split()).lower()


def parse_dt(v):
    v = blank(v)
    return datetime.strptime(v, "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST) if v else None


def parse_date(v):
    v = blank(v)
    if not v:
        return None
    fmt = "%Y-%m-%d" if re.match(r"\d{4}-", v) else "%d/%m/%Y"
    return datetime.strptime(v, fmt).date()


def date_key(d):
    return int(d.strftime("%Y%m%d")) if d else None


def money(v):
    v = blank(v)
    return v.replace(",", "") if v else None


def split_person(raw):
    """'Ramkumar(R)(9540957190)' -> ('Ramkumar(R)', '9540957190')."""
    raw = raw.strip()
    m = PHONE_RE.search(raw)
    phone = m.group(0) if m else None
    name = PHONE_RE.sub("", raw) if m else raw
    name = re.sub(r"\(\s*\)", "", name)
    return " ".join(name.split()), phone


class Lookup:
    """In-memory get-or-create for a simple name dimension."""

    def __init__(self, cur, table, key_col, name_col):
        self.cur, self.table, self.key_col, self.name_col = cur, table, key_col, name_col
        cur.execute(f"SELECT {key_col}, {name_col} FROM {table}")
        self.map = {norm(n): k for k, n in cur.fetchall()}
        self.created = 0

    def get(self, name):
        name = blank(name)
        if not name:
            return None
        n = norm(name)
        if n not in self.map:
            self.cur.execute(
                f"INSERT INTO {self.table} ({self.name_col}) VALUES (%s) RETURNING {self.key_col}",
                (" ".join(name.split()),))
            self.map[n] = self.cur.fetchone()[0]
            self.created += 1
        return self.map[n]


def main(commit):
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set.")
    orders = list(csv.DictReader(open(DATA / "orders.csv", encoding="utf-8")))
    users = list(csv.DictReader(open(DATA / "users.csv", encoding="utf-8")))

    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM fact_orders")
        if cur.fetchone()[0]:
            sys.exit("fact_orders is not empty; refusing to import.")

        # ---- users: hashes only, never the plain-text passwords
        for u in users:
            pw = bcrypt.hashpw(u["Password"].encode(), bcrypt.gensalt()).decode()
            cur.execute(
                """INSERT INTO dim_user (username, password_hash, role, display_name)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO NOTHING""",
                (u["Username"].strip().lower(), pw, u["Role"].strip().lower(), u["Display Name"].strip()))
        cur.execute("SELECT user_key, username, display_name FROM dim_user ORDER BY user_key")
        user_map = {}
        for k, uname, disp in cur.fetchall():
            user_map.setdefault(norm(uname), k)
            user_map.setdefault(norm(disp), k)
        legacy = []

        def user_key(name):
            name = blank(name)
            if not name:
                return None
            n = norm(name)
            if n not in user_map:  # in the order history but not in the Users tab: login disabled
                cur.execute(
                    """INSERT INTO dim_user (username, password_hash, role, display_name)
                       VALUES (%s, '!', 'legacy', %s) RETURNING user_key""", (n, name))
                user_map[n] = cur.fetchone()[0]
                legacy.append(name)
            return user_map[n]

        channel = Lookup(cur, "dim_order_channel", "channel_key", "channel_name")
        subtype = Lookup(cur, "dim_submission_type", "submission_type_key", "type_name")
        dstatus = Lookup(cur, "dim_delivery_status", "status_key", "status_name")
        pstatus = Lookup(cur, "dim_payment_status", "payment_status_key", "status_name")

        people, phone_conflicts = {}, []

        def person_key(raw, role):
            raw = blank(raw)
            if not raw:
                return None
            name, phone = split_person(raw)
            k = (norm(name), role)
            if k not in people:
                cur.execute(
                    """INSERT INTO dim_person (full_name, phone_number, person_role)
                       VALUES (%s, %s, %s) RETURNING person_key""", (name, phone, role))
                people[k] = (cur.fetchone()[0], phone)
            elif phone and people[k][1] and phone != people[k][1]:
                phone_conflicts.append((name, people[k][1], phone))
            return people[k][0]

        # ---- duplicate sl_no: keep the earliest, renumber later ones above the current max
        for o in orders:
            o["_ts"] = parse_dt(o["Timestamp"])
        orders.sort(key=lambda o: o["_ts"])
        seen, next_sl, renumbered = set(), max(int(o["Sl No"]) for o in orders) + 1, []
        for o in orders:
            sl = int(o["Sl No"])
            o["_sl"] = sl
            if sl in seen:
                o["_sl"] = next_sl
                renumbered.append((sl, next_sl, o["Timestamp"], o["DC/Inv No."]))
                next_sl += 1
            seen.add(o["_sl"])

        rows = []
        for o in orders:
            rows.append((
                o["_sl"], blank(o["DC/Inv No."]),
                date_key(parse_date(o["Order Received Date"])),
                channel.get(o["Order Via"]), subtype.get(o["Type of Submission"]),
                dstatus.get(o["Delivery Status"]),
                person_key(o["Ready By Whom"], "ready_by"),
                person_key(o["Colour Making By Whom"], "colour_making"),
                person_key(o["Delivered By Whom"], "delivery"),
                pstatus.get(o["Mat. Payment Status"]),
                user_key(o["Created By"]), user_key(o["Last Updated By"]),
                blank(o["Shipping Location"]), blank(o["Detailed Remarks"]),
                o["_ts"], parse_dt(o["Material Delivery Date & Time"]),
                date_key(parse_date(o["Date of Receiving"])), parse_dt(o["Last Updated At"]),
                money(o["Amount Received (Rs)"]), money(o["Cartage (Rs)"]),
                (blank(o["Delivery Status"]) or "").lower() == "cancelled",
            ))
        execute_values(cur, """INSERT INTO fact_orders (
            sl_no, dc_inv_no, order_received_date_key, order_via_key, submission_type_key,
            delivery_status_key, ready_by_person_key, colour_making_person_key,
            delivered_by_person_key, payment_status_key, created_by_user_key,
            last_updated_by_user_key, shipping_location, detailed_remarks, timestamp_created,
            material_delivery_datetime, date_of_receiving_key, last_updated_at,
            amount_received, cartage, is_cancelled) VALUES %s""", rows, page_size=500)

        print(f"orders inserted:      {len(rows)}")
        print(f"users in dim_user:    {len(set(user_map.values()))} "
              f"(legacy, login disabled: {sorted(set(legacy))})")
        print(f"channels/types/delivery/payment created: "
              f"{channel.created}/{subtype.created}/{dstatus.created}/{pstatus.created}")
        print(f"people created:       {len(people)}  {dict(Counter(r for _, r in people))}")
        print(f"phone conflicts:      {phone_conflicts}")
        print(f"renumbered sl_no:     {len(renumbered)}")
        with open(DATA / "sl_no_renumbered.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["original_sl_no", "new_sl_no", "timestamp", "dc_inv_no"])
            w.writerows(renumbered)

        if commit:
            conn.commit()
            print("COMMITTED.")
        else:
            conn.rollback()
            print("Dry run: rolled back, nothing written. Re-run with --commit.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main("--commit" in sys.argv)
