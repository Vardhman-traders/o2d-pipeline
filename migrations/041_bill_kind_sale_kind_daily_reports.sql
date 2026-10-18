-- 041: (a) the admin views get two derived columns so the dashboards can filter on them:
--   bill_kind  'Challan' / 'Invoice' - the bill series the order belongs to (its submission type's "Bill number series"; for a type that
--              allows either, the Challan / Invoice pick stored on the order). NULL for free-form types and old untagged "12/345" orders.
--   sale_kind  'Sales' / 'Sales return' - a return is a Sales Return series type, a Goods Return type, or a delivery status that subtracts.
-- (b) the automatic 7:30 PM run also keeps the pending / received reports of the Reports card: report_kind says which one a row is
--   (NULL = a delivery person's report, as before; person_name then holds the report's title).
-- Columns are only appended, so the views can be replaced in place. Safe to run twice.

ALTER TABLE daily_delivery_report ADD COLUMN IF NOT EXISTS report_kind varchar(40);

CREATE OR REPLACE VIEW v_orders AS
SELECT
    o.order_key, o.sl_no, o.dc_inv_no,
    d.full_date AS order_date,
    c.channel_name AS channel,
    st.type_name AS submission_type,
    ds.status_name AS delivery_status,
    ps.status_name AS payment_status,
    rp.full_name AS ready_by,
    cp.full_name AS colour_making_by,
    dp.full_name AS delivered_by,
    dp.phone_number AS delivered_by_phone,
    cu.display_name AS created_by,
    lu.display_name AS last_updated_by,
    o.shipping_location, o.detailed_remarks,
    o.timestamp_created, o.material_delivery_datetime,
    rd.full_date AS date_of_receiving,
    o.last_updated_at, (CASE WHEN ds.amount_effect = 'subtract' THEN -1 ELSE 1 END * round(o.amount_received / 100, 2))::numeric(10, 2) AS amount_received, o.cartage, o.is_cancelled,
    CASE
        WHEN o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled' THEN 'Cancelled'
        WHEN o.delivery_status_key IS NULL THEN 'Awaiting godown'
        WHEN o.material_delivery_datetime IS NULL THEN 'Awaiting dispatch'
        WHEN o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL THEN 'Awaiting receiving'
        ELSE 'Closed'
    END AS stage,
    CASE
        WHEN NOT (o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled')
             AND o.material_delivery_datetime >= o.timestamp_created
        THEN EXTRACT(EPOCH FROM (o.material_delivery_datetime - o.timestamp_created)) / 3600.0
    END AS hours_to_deliver,
    o.archived_at,
    o.delivered_by_detail,
    o.amount_received AS amount_received_entered,
    CASE
        WHEN st.bill_series IN ('challan', 'sales_return_challan') THEN 'Challan'
        WHEN st.bill_series IN ('invoice', 'sales_return_invoice') THEN 'Invoice'
        WHEN st.bill_series IN ('both', 'goods_return') AND o.bill_series = 'challan' THEN 'Challan'
        WHEN st.bill_series IN ('both', 'goods_return') AND o.bill_series = 'invoice' THEN 'Invoice'
    END AS bill_kind,
    CASE
        WHEN st.bill_series IN ('sales_return_challan', 'sales_return_invoice', 'goods_return') OR ds.amount_effect = 'subtract' THEN 'Sales return'
        ELSE 'Sales'
    END AS sale_kind
FROM fact_orders AS o
JOIN dim_date AS d ON d.date_key = o.order_received_date_key
LEFT JOIN dim_date AS rd ON rd.date_key = o.date_of_receiving_key
LEFT JOIN dim_order_channel AS c ON c.channel_key = o.order_via_key
LEFT JOIN dim_submission_type AS st ON st.submission_type_key = o.submission_type_key
LEFT JOIN dim_delivery_status AS ds ON ds.status_key = o.delivery_status_key
LEFT JOIN dim_payment_status AS ps ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_person AS rp ON rp.person_key = o.ready_by_person_key
LEFT JOIN dim_person AS cp ON cp.person_key = o.colour_making_person_key
LEFT JOIN dim_person AS dp ON dp.person_key = o.delivered_by_person_key
LEFT JOIN dim_user AS cu ON cu.user_key = o.created_by_user_key
LEFT JOIN dim_user AS lu ON lu.user_key = o.last_updated_by_user_key
WHERE o.archived_at IS NULL;

CREATE OR REPLACE VIEW v_orders_archive AS
SELECT
    o.order_key, o.sl_no, o.dc_inv_no,
    d.full_date AS order_date,
    c.channel_name AS channel,
    st.type_name AS submission_type,
    ds.status_name AS delivery_status,
    ps.status_name AS payment_status,
    rp.full_name AS ready_by,
    cp.full_name AS colour_making_by,
    dp.full_name AS delivered_by,
    dp.phone_number AS delivered_by_phone,
    cu.display_name AS created_by,
    lu.display_name AS last_updated_by,
    o.shipping_location, o.detailed_remarks,
    o.timestamp_created, o.material_delivery_datetime,
    rd.full_date AS date_of_receiving,
    o.last_updated_at, (CASE WHEN ds.amount_effect = 'subtract' THEN -1 ELSE 1 END * round(o.amount_received / 100, 2))::numeric(10, 2) AS amount_received, o.cartage, o.is_cancelled,
    CASE
        WHEN o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled' THEN 'Cancelled'
        WHEN o.delivery_status_key IS NULL THEN 'Awaiting godown'
        WHEN o.material_delivery_datetime IS NULL THEN 'Awaiting dispatch'
        WHEN o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL THEN 'Awaiting receiving'
        ELSE 'Closed'
    END AS stage,
    CASE
        WHEN NOT (o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled')
             AND o.material_delivery_datetime >= o.timestamp_created
        THEN EXTRACT(EPOCH FROM (o.material_delivery_datetime - o.timestamp_created)) / 3600.0
    END AS hours_to_deliver,
    o.archived_at,
    o.delivered_by_detail,
    o.amount_received AS amount_received_entered,
    CASE
        WHEN st.bill_series IN ('challan', 'sales_return_challan') THEN 'Challan'
        WHEN st.bill_series IN ('invoice', 'sales_return_invoice') THEN 'Invoice'
        WHEN st.bill_series IN ('both', 'goods_return') AND o.bill_series = 'challan' THEN 'Challan'
        WHEN st.bill_series IN ('both', 'goods_return') AND o.bill_series = 'invoice' THEN 'Invoice'
    END AS bill_kind,
    CASE
        WHEN st.bill_series IN ('sales_return_challan', 'sales_return_invoice', 'goods_return') OR ds.amount_effect = 'subtract' THEN 'Sales return'
        ELSE 'Sales'
    END AS sale_kind
FROM fact_orders AS o
JOIN dim_date AS d ON d.date_key = o.order_received_date_key
LEFT JOIN dim_date AS rd ON rd.date_key = o.date_of_receiving_key
LEFT JOIN dim_order_channel AS c ON c.channel_key = o.order_via_key
LEFT JOIN dim_submission_type AS st ON st.submission_type_key = o.submission_type_key
LEFT JOIN dim_delivery_status AS ds ON ds.status_key = o.delivery_status_key
LEFT JOIN dim_payment_status AS ps ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_person AS rp ON rp.person_key = o.ready_by_person_key
LEFT JOIN dim_person AS cp ON cp.person_key = o.colour_making_person_key
LEFT JOIN dim_person AS dp ON dp.person_key = o.delivered_by_person_key
LEFT JOIN dim_user AS cu ON cu.user_key = o.created_by_user_key
LEFT JOIN dim_user AS lu ON lu.user_key = o.last_updated_by_user_key;
