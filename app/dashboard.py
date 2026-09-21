"""Read-only aggregate queries for the admin dashboard. All data comes from the v_orders view."""
from datetime import date, timedelta

from . import db

IST = "Asia/Kolkata"

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
FROM v_orders WHERE order_date BETWEEN %s AND %s
"""


def today_ist(cur) -> date:
    cur.execute("SELECT (now() AT TIME ZONE %s)::date AS d", (IST,))
    return cur.fetchone()["d"]


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def headline(date_from: date, date_to: date) -> dict:
    with db.cursor() as cur:
        cur.execute(HEADLINE_SQL, (date_from, date_to))
        return cur.fetchone()


def build(date_from: date, date_to: date) -> dict:
    span = (date_to - date_from).days + 1
    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span - 1)
    with db.cursor() as cur:
        cur.execute(HEADLINE_SQL, (date_from, date_to))
        current = cur.fetchone()
        cur.execute(HEADLINE_SQL, (prev_from, prev_to))
        previous = cur.fetchone()
        rng = (date_from, date_to)
        out = {
            "range": {"from": date_from, "to": date_to, "days": span,
                      "previous_from": prev_from, "previous_to": prev_to},
            "headline": current,
            "previous": previous,
            "daily": _rows(cur, """
                SELECT dd.full_date AS date, dd.is_monday_holiday AS holiday,
                       count(v.order_key) AS orders,
                       count(v.order_key) FILTER (WHERE v.stage = 'Cancelled') AS cancelled,
                       COALESCE(sum(v.amount_received), 0) AS amount_received,
                       COALESCE(sum(v.cartage), 0) AS cartage,
                       avg(v.hours_to_deliver) AS avg_hours
                FROM dim_date dd LEFT JOIN v_orders v ON v.order_date = dd.full_date
                WHERE dd.full_date BETWEEN %s AND %s GROUP BY dd.full_date, dd.is_monday_holiday
                ORDER BY dd.full_date""", rng),
            "delivery_time_buckets": _rows(cur, """
                SELECT bucket, n FROM (
                  SELECT CASE WHEN hours_to_deliver < 2 THEN 1 WHEN hours_to_deliver < 6 THEN 2
                              WHEN hours_to_deliver < 24 THEN 3 WHEN hours_to_deliver < 48 THEN 4 ELSE 5 END AS o,
                         CASE WHEN hours_to_deliver < 2 THEN 'Under 2 h' WHEN hours_to_deliver < 6 THEN '2 to 6 h'
                              WHEN hours_to_deliver < 24 THEN '6 to 24 h' WHEN hours_to_deliver < 48 THEN '1 to 2 days'
                              ELSE 'Over 2 days' END AS bucket, count(*) AS n
                  FROM v_orders WHERE order_date BETWEEN %s AND %s AND hours_to_deliver IS NOT NULL
                  GROUP BY 1, 2) t ORDER BY o""", rng),
            "by_channel": _rows(cur, """
                SELECT COALESCE(channel, 'Not set') AS label, count(*) AS orders,
                       COALESCE(sum(amount_received), 0) AS amount_received
                FROM v_orders WHERE order_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 2 DESC""", rng),
            "by_submission_type": _rows(cur, """
                SELECT COALESCE(submission_type, 'Not set') AS label, count(*) AS orders
                FROM v_orders WHERE order_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 2 DESC""", rng),
            "by_delivery_status": _rows(cur, """
                SELECT COALESCE(delivery_status, 'Not set') AS label, count(*) AS orders
                FROM v_orders WHERE order_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 2 DESC""", rng),
            "by_payment_status": _rows(cur, """
                SELECT COALESCE(payment_status, 'Not set') AS label, count(*) AS orders,
                       COALESCE(sum(amount_received), 0) AS amount_received
                FROM v_orders WHERE order_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 2 DESC""", rng),
            "by_hour": _rows(cur, """
                SELECT h.hour, count(v.order_key) AS orders
                FROM generate_series(0, 23) AS h(hour)
                LEFT JOIN v_orders v ON v.order_date BETWEEN %s AND %s
                     AND EXTRACT(HOUR FROM v.timestamp_created AT TIME ZONE %s)::int = h.hour
                GROUP BY h.hour ORDER BY h.hour""", (date_from, date_to, IST)),
            "ready_by": _rows(cur, """
                SELECT ready_by AS label, count(*) AS orders FROM v_orders
                WHERE order_date BETWEEN %s AND %s AND ready_by IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", rng),
            "colour_making_by": _rows(cur, """
                SELECT colour_making_by AS label, count(*) AS orders FROM v_orders
                WHERE order_date BETWEEN %s AND %s AND colour_making_by IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC""", rng),
            "delivered_by": _rows(cur, """
                SELECT delivered_by AS label, count(*) AS orders, COALESCE(sum(cartage), 0) AS cartage,
                       avg(hours_to_deliver) AS avg_hours
                FROM v_orders WHERE order_date BETWEEN %s AND %s AND delivered_by IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", rng),
            "created_by": _rows(cur, """
                SELECT created_by AS label, count(*) AS orders FROM v_orders
                WHERE order_date BETWEEN %s AND %s AND created_by IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", rng),
            # Right now, regardless of the selected range:
            "pipeline": _rows(cur, """
                SELECT stage AS label, count(*) AS orders, min(timestamp_created) AS oldest
                FROM v_orders WHERE stage NOT IN ('Closed', 'Cancelled')
                GROUP BY stage ORDER BY CASE stage WHEN 'Awaiting godown' THEN 1
                     WHEN 'Awaiting dispatch' THEN 2 ELSE 3 END"""),
            "oldest_open": _rows(cur, """
                SELECT sl_no, dc_inv_no, order_date, stage, delivery_status, shipping_location,
                       round((EXTRACT(EPOCH FROM (now() - timestamp_created)) / 3600.0)::numeric, 1) AS age_hours
                FROM v_orders WHERE stage NOT IN ('Closed', 'Cancelled')
                ORDER BY timestamp_created ASC LIMIT 10"""),
            "today": today_ist(cur),
            "data_start": _rows(cur, "SELECT min(order_date) AS d FROM v_orders")[0]["d"],
        }
    return out
