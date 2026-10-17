-- Bill numbers that look like they are in the wrong series (read-only). The portal runs the same check: Setup > Order changes > "Wrong bill series check".
-- Challan: a number more than 100 above the highest OTHER challan number of that day (an invoice number typed as a challan).
WITH c AS (
  SELECT o.sl_no, v.order_date, o.dc_inv_no::bigint AS n, st.type_name
  FROM fact_orders o JOIN v_orders_archive v ON v.order_key = o.order_key
  JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key
  WHERE st.bill_series IN ('challan', 'sales_return_challan') AND o.dc_inv_no ~ '^[0-9]+$')
SELECT c.sl_no, c.order_date, c.n, c.type_name, 'far above that day''s other challan numbers' AS why
FROM c WHERE c.n > 100 + COALESCE((SELECT max(x.n) FROM c x WHERE x.order_date = c.order_date AND x.type_name = c.type_name AND x.n <> c.n), 0)
UNION ALL
-- Invoice: a number ten times smaller than the financial year's median (a challan number typed as an invoice).
SELECT i.sl_no, i.order_date, i.n, i.type_name, 'far below this financial year''s usual invoice numbers'
FROM (SELECT o.sl_no, v.order_date, o.dc_inv_no::bigint AS n, st.type_name,
             CASE WHEN extract(month FROM v.order_date) >= 4 THEN extract(year FROM v.order_date) ELSE extract(year FROM v.order_date) - 1 END AS fy
      FROM fact_orders o JOIN v_orders_archive v ON v.order_key = o.order_key
      JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key
      WHERE st.bill_series IN ('invoice', 'sales_return_invoice') AND o.dc_inv_no ~ '^[0-9]+$') i
WHERE i.n * 10 < (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY y.n) FROM (SELECT o.dc_inv_no::bigint AS n,
        CASE WHEN extract(month FROM d2.full_date) >= 4 THEN extract(year FROM d2.full_date) ELSE extract(year FROM d2.full_date) - 1 END AS fy,
        st2.type_name FROM fact_orders o JOIN dim_date d2 ON d2.date_key = o.order_received_date_key
        JOIN dim_submission_type st2 ON st2.submission_type_key = o.submission_type_key
        WHERE st2.bill_series IN ('invoice', 'sales_return_invoice') AND o.dc_inv_no ~ '^[0-9]+$') y WHERE y.fy = i.fy AND y.type_name = i.type_name)
  AND (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY y.n) FROM (SELECT o.dc_inv_no::bigint AS n,
        CASE WHEN extract(month FROM d2.full_date) >= 4 THEN extract(year FROM d2.full_date) ELSE extract(year FROM d2.full_date) - 1 END AS fy,
        st2.type_name FROM fact_orders o JOIN dim_date d2 ON d2.date_key = o.order_received_date_key
        JOIN dim_submission_type st2 ON st2.submission_type_key = o.submission_type_key
        WHERE st2.bill_series IN ('invoice', 'sales_return_invoice') AND o.dc_inv_no ~ '^[0-9]+$') y WHERE y.fy = i.fy AND y.type_name = i.type_name) >= 50
ORDER BY 2, 1;
