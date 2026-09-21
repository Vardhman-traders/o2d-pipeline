"""Remove everything created during testing: TEST- orders and test_* users.

    python cleanup_test_data.py          # shows what would be deleted
    python cleanup_test_data.py --yes    # deletes it

Dropdown values (channels, people, ...) added during testing are only listed, never deleted.
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ORDERS = ("FROM fact_orders WHERE upper(dc_inv_no) LIKE 'TEST-%' OR created_by_user_key IN "
          "(SELECT user_key FROM dim_user WHERE username LIKE 'test\\_%')")


def main(really):
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT sl_no, dc_inv_no, timestamp_created " + ORDERS + " ORDER BY sl_no")
            orders = cur.fetchall()
            cur.execute("SELECT username, role FROM dim_user WHERE username LIKE 'test\\_%' ORDER BY username")
            users = cur.fetchall()
            print(f"{len(orders)} test order(s):")
            for o in orders:
                print("   sl_no", o[0], "|", o[1], "|", o[2])
            print(f"{len(users)} test user(s):", [u[0] for u in users])
            if not really:
                print("Dry run. Re-run with --yes to delete.")
                return
            cur.execute("DELETE " + ORDERS)
            # also detach test users from any remaining rows before deleting them
            cur.execute("UPDATE fact_orders SET last_updated_by_user_key = NULL WHERE last_updated_by_user_key IN "
                        "(SELECT user_key FROM dim_user WHERE username LIKE 'test\\_%')")
            cur.execute("DELETE FROM admin_audit_log WHERE admin_user_key IN "
                        "(SELECT user_key FROM dim_user WHERE username LIKE 'test\\_%')")
            cur.execute("DELETE FROM app_links WHERE name LIKE 'TEST %'")
            cur.execute("DELETE FROM dim_user WHERE username LIKE 'test\\_%'")
            cur.execute("SELECT count(*), max(sl_no) FROM fact_orders")
            print("Deleted. fact_orders now: %s rows, max sl_no %s" % cur.fetchone())
    finally:
        conn.close()


if __name__ == "__main__":
    main("--yes" in sys.argv)
