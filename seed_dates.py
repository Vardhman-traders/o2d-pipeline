"""Generate dim_date rows for a range of years. Safe to re-run (existing dates are skipped)."""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

START = date(2024, 1, 1)
END = date(2035, 12, 31)
# Business rule: every Monday is a holiday. Adjust here if the rule changes.
def is_monday_holiday(d: date) -> bool:
    return d.weekday() == 0

load_dotenv(Path(__file__).parent / ".env")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1
    rows = []
    d = START
    while d <= END:
        rows.append((int(d.strftime("%Y%m%d")), d, d.day, d.month, d.year,
                     d.strftime("%A"), is_monday_holiday(d)))
        d += timedelta(days=1)

    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO dim_date (date_key, full_date, day, month, year, day_of_week, is_monday_holiday)
                   VALUES %s ON CONFLICT (date_key) DO NOTHING""",
                rows, page_size=1000)
            cur.execute("SELECT count(*), min(full_date), max(full_date) FROM dim_date")
            print("dim_date now has %s rows (%s to %s)" % cur.fetchone())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
