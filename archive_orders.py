"""Archive orders that are 7+ days old and fully complete (Closed or Cancelled stage).

    python archive_orders.py          # dry run: shows what would be archived
    python archive_orders.py --yes    # marks them archived_at = now()

Archived orders are never deleted - they stay in fact_orders, just excluded from
v_orders (and so from the API, the operating apps and the dashboard). Full history,
active + archived, is queryable via v_orders_archive with direct database access only.

Intended to run on a schedule (e.g. a Render Cron Job, weekly or daily) so history
does not build up in the operational views.
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

CANDIDATES_SQL = """
    SELECT sl_no, dc_inv_no, order_date, stage
    FROM v_orders
    WHERE stage IN ('Closed', 'Cancelled')
      AND order_date <= (now() AT TIME ZONE 'Asia/Kolkata')::date - 7
    ORDER BY order_date
"""

ARCHIVE_SQL = """
    UPDATE fact_orders o SET archived_at = now()
    FROM v_orders v
    WHERE v.order_key = o.order_key
      AND v.stage IN ('Closed', 'Cancelled')
      AND v.order_date <= (now() AT TIME ZONE 'Asia/Kolkata')::date - 7
"""


def main(really: bool) -> int:
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CANDIDATES_SQL)
            rows = cur.fetchall()
            print(f"{len(rows)} order(s) eligible for archiving (7+ days old, Closed or Cancelled).")
            if rows:
                oldest, newest = rows[0][2], rows[-1][2]
                print(f"Order date range: {oldest} to {newest}")
            if not really:
                print("Dry run. Re-run with --yes to archive.")
                return 0
            cur.execute(ARCHIVE_SQL)
            print(f"Archived {cur.rowcount} order(s).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main("--yes" in sys.argv))
