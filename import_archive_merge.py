"""Merge-import the Archive sheet's full history into Postgres, skipping anything
already there. Unlike import_sheet.py (which refuses on a non-empty database), this
is designed to run against a database that already has orders - safe to re-run.

    python import_archive_merge.py "path/to/Archive - Orders.csv"              # dry run
    python import_archive_merge.py "path/to/Archive - Orders.csv" --commit     # writes for real

Duplicate detection: a CSV row is treated as already present if an existing order
has the same (DC/Inv No, Timestamp) - the Timestamp is effectively a creation-event
id, so this pairing is reliable. New rows get fresh sl_no values (continuing after
the current max) rather than reusing the CSV's own Sl No column, since that range
collides with what's already imported.
"""
import csv
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
load_dotenv(ROOT / ".env")

PHONE_RE = re.compile(r"\d{10}")
TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")


def blank(v):
    v = (v or "").strip()
    return v or None


def norm(v):
    return " ".join(v.split()).lower()


def parse_ts(v):
    v = blank(v)
    if not v:
        return None
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp: {v!r}")


def parse_date_only(v):
    v = blank(v)
    if not v:
        return None
    v = v.split(" ")[0]  # some exports carry a trailing 00:00:00
    fmt = "%Y-%m-%d" if re.match(r"\d{4}-", v) else "%d/%m/%Y"
    return datetime.strptime(v, fmt).date()


def date_key(d):
    return int(d.strftime("%Y%m%d")) if d else None


def money(v):
    v = blank(v)
    return v.replace(",", "") if v else None


def split_person(raw):
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


def main(csv_path, commit):
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set.")
    rows_in = list(csv.DictReader(open(csv_path, encoding="utf-8")))

    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        cur = conn.cursor()

        # ---- what's already in the database, for duplicate detection
        cur.execute("SELECT lower(btrim(dc_inv_no)), timestamp_created FROM fact_orders WHERE dc_inv_no IS NOT NULL")
        existing = set(cur.fetchall())
        cur.execute("SELECT COALESCE(MAX(sl_no), 0) FROM fact_orders")
        next_sl = cur.fetchone()[0] + 1

        # ---- users referenced by name in the sheet; login stays disabled unless
        # they already exist for real (matches import_sheet.py's approach)
        cur.execute("SELECT user_key, username, display_name FROM dim_user")
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
            if n not in user_map:
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

        cur.execute("SELECT person_key, full_name, phone_number, person_role FROM dim_person")
        people = {(norm(name), role): (key, phone) for key, name, phone, role in cur.fetchall()}
        phone_conflicts = []
        people_created = []

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
                people_created.append(role)
            elif phone and people[k][1] and phone != people[k][1]:
                phone_conflicts.append((name, people[k][1], phone))
            return people[k][0]

        skipped, to_insert = 0, []
        for o in rows_in:
            ts = parse_ts(o["Timestamp"])
            dc = blank(o["DC/Inv No."])
            key = (norm(dc) if dc else None, ts)
            if key in existing:
                skipped += 1
                continue
            to_insert.append((o, ts))
            existing.add(key)  # guard against dupes within the CSV itself too

        rows = []
        for o, ts in to_insert:
            sl = next_sl
            next_sl += 1
            rows.append((
                sl, blank(o["DC/Inv No."]),
                date_key(parse_date_only(o["Order Received Date"])),
                channel.get(o["Order Received Thru"]), subtype.get(o["Type of Submission"]),
                dstatus.get(o["Delivery Status"]),
                person_key(o["Ready By Whom"], "ready_by"),
                person_key(o["Colour Making By Whom"], "colour_making"),
                person_key(o["Delivered By Whom"], "delivery"),
                pstatus.get(o["Mat. Payment Status"]),
                user_key(o["Created By"]), user_key(o["Last Updated By"]),
                blank(o["Shipping Location"]), blank(o["Detailed Remarks"]),
                ts, parse_ts(o["Material Delivery Date & Time"]),
                date_key(parse_date_only(o["Date of Receiving"])), parse_ts(o["Last Updated At"]) or ts,
                money(o["Amount Rcvd"]), money(o["Cartage (In Rupees)"]),
                (blank(o["Delivery Status"]) or "").lower() == "cancelled",
            ))

        if rows:
            execute_values(cur, """INSERT INTO fact_orders (
                sl_no, dc_inv_no, order_received_date_key, order_via_key, submission_type_key,
                delivery_status_key, ready_by_person_key, colour_making_person_key,
                delivered_by_person_key, payment_status_key, created_by_user_key,
                last_updated_by_user_key, shipping_location, detailed_remarks, timestamp_created,
                material_delivery_datetime, date_of_receiving_key, last_updated_at,
                amount_received, cartage, is_cancelled) VALUES %s""", rows, page_size=500)

        print(f"CSV rows:              {len(rows_in)}")
        print(f"already in database:   {skipped}")
        print(f"newly inserted:        {len(rows)}")
        print(f"legacy users created:  {sorted(set(legacy))}")
        print(f"channels/types/delivery/payment created: "
              f"{channel.created}/{subtype.created}/{dstatus.created}/{pstatus.created}")
        print(f"people created:        {len(people_created)}  {dict(Counter(people_created))}")
        print(f"phone conflicts:       {phone_conflicts}")

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
    args = [a for a in sys.argv[1:] if a != "--commit"]
    if not args:
        sys.exit("Usage: python import_archive_merge.py <csv path> [--commit]")
    main(args[0], "--commit" in sys.argv)
