"""Read-only aggregate queries for the admin dashboard. All data comes from the v_orders view."""
from datetime import date, timedelta

from . import db

IST = "Asia/Kolkata"

# Attribute filters the dashboard accepts, same columns as the Orders page's filter groups.
FILTERABLE_COLUMNS = ("stage", "channel", "submission_type", "delivery_status", "payment_status",
                      "ready_by", "colour_making_by", "delivered_by", "created_by")


def _filter_clause(filters: dict | None, prefix: str = "") -> tuple[str, list]:
    """Build "AND [prefix.]col = ANY(%s) ..." for each non-empty filter (column names are fixed, not user input)."""
    filters = filters or {}
    clauses, params = [], []
    for col in FILTERABLE_COLUMNS:
        values = filters.get(col)
        if values:
            clauses.append(f"{prefix}{col} = ANY(%s)")
            params.append(list(values))
    return ("" if not clauses else " AND " + " AND ".join(clauses)), params


HEADLINE_SQL = """
SELECT count(*)                                                          AS orders,
       count(*) FILTER (WHERE stage = 'Cancelled')                       AS cancelled,
       count(*) FILTER (WHERE material_delivery_datetime IS NOT NULL
                          AND stage <> 'Cancelled')                      AS delivered,
       count(*) FILTER (WHERE stage = 'Closed')                          AS closed,
       COALESCE(sum(amount_received), 0)                                 AS amount_received,
       COALESCE(sum(cartage), 0)                                         AS cartage,
       avg(hours_to_deliver)                                             AS avg_hours,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY hours_to_deliver)     AS median_hours,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY hours_to_deliver)     AS p90_hours,
       (count(*) FILTER (WHERE hours_to_deliver <= 24))::float
           / NULLIF(count(hours_to_deliver), 0)                          AS within_24h
FROM v_orders WHERE order_date BETWEEN %s AND %s{extra}
"""


def today_ist(cur) -> date:
    cur.execute("SELECT (now() AT TIME ZONE %s)::date AS d", (IST,))
    return cur.fetchone()["d"]


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def headline(date_from: date, date_to: date) -> dict:
    with db.cursor() as cur:
        cur.execute(HEADLINE_SQL.format(extra=""), (date_from, date_to))
        return cur.fetchone()


def build(date_from: date, date_to: date, filters: dict | None = None) -> dict:
    span = (date_to - date_from).days + 1
    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span - 1)
    extra, extra_params = _filter_clause(filters)
    extra_v, extra_v_params = _filter_clause(filters, prefix="v.")
    with db.cursor() as cur:
        cur.execute(HEADLINE_SQL.format(extra=extra), (date_from, date_to, *extra_params))
        current = cur.fetchone()
        cur.execute(HEADLINE_SQL.format(extra=extra), (prev_from, prev_to, *extra_params))
        previous = cur.fetchone()
        rng = (date_from, date_to, *extra_params)
        out = {
            "range": {"from": date_from, "to": date_to, "days": span,
                      "previous_from": prev_from, "previous_to": prev_to},
            "headline": current,
            "previous": previous,
            "daily": _rows(cur, f"""
                SELECT dd.full_date AS date, dd.is_monday_holiday AS holiday,
                       count(v.order_key) AS orders,
                       count(v.order_key) FILTER (WHERE v.stage = 'Cancelled') AS cancelled,
                       avg(v.hours_to_deliver) AS avg_hours
                FROM dim_date dd LEFT JOIN v_orders v ON v.order_date = dd.full_date{extra_v}
                WHERE dd.full_date BETWEEN %s AND %s GROUP BY dd.full_date, dd.is_monday_holiday
                ORDER BY dd.full_date""", (*extra_v_params, date_from, date_to)),
            "delivery_time_buckets": _rows(cur, f"""
                SELECT bucket, n FROM (
                  SELECT CASE WHEN hours_to_deliver < 2 THEN 1 WHEN hours_to_deliver < 6 THEN 2
                              WHEN hours_to_deliver < 24 THEN 3 WHEN hours_to_deliver < 48 THEN 4 ELSE 5 END AS o,
                         CASE WHEN hours_to_deliver < 2 THEN 'Under 2 h' WHEN hours_to_deliver < 6 THEN '2 to 6 h'
                              WHEN hours_to_deliver < 24 THEN '6 to 24 h' WHEN hours_to_deliver < 48 THEN '1 to 2 days'
                              ELSE 'Over 2 days' END AS bucket,
                         CASE WHEN hours_to_deliver < 2 THEN 0 WHEN hours_to_deliver < 6 THEN 2
                              WHEN hours_to_deliver < 24 THEN 6 WHEN hours_to_deliver < 48 THEN 24 ELSE 48 END AS min_hours,
                         CASE WHEN hours_to_deliver < 2 THEN 2 WHEN hours_to_deliver < 6 THEN 6
                              WHEN hours_to_deliver < 24 THEN 24 WHEN hours_to_deliver < 48 THEN 48 END AS max_hours,
                         count(*) AS n
                  FROM v_orders WHERE order_date BETWEEN %s AND %s AND hours_to_deliver IS NOT NULL{extra}
                  GROUP BY 1, 2, 3, 4) t ORDER BY o""", rng),
            # Right now, regardless of the selected date range, but still honouring the same attribute filters:
            "pipeline": _rows(cur, f"""
                SELECT stage AS label, count(*) AS orders, min(timestamp_created) AS oldest
                FROM v_orders WHERE stage NOT IN ('Closed', 'Cancelled'){extra}
                GROUP BY stage ORDER BY CASE stage WHEN 'Awaiting godown' THEN 1
                     WHEN 'Awaiting dispatch' THEN 2 ELSE 3 END""", extra_params),
            "oldest_open": _rows(cur, f"""
                SELECT sl_no, dc_inv_no, order_date, stage, delivery_status, shipping_location,
                       round((EXTRACT(EPOCH FROM (now() - timestamp_created)) / 3600.0)::numeric, 1) AS age_hours
                FROM v_orders WHERE stage NOT IN ('Closed', 'Cancelled'){extra}
                ORDER BY timestamp_created ASC LIMIT 10""", extra_params),
            "today": today_ist(cur),
            "data_start": _rows(cur, "SELECT min(order_date) AS d FROM v_orders")[0]["d"],
        }
    return out
