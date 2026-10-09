-- 022: the hard-coded display rule for "Amount received": every dashboard, report and export shows the entered value divided by 100
-- (enter 10,000 -> shown as 100). The stored value is untouched - only the views the dashboards read apply the rule.
-- The value exactly as entered stays available as amount_received_entered, which the edit forms use so that saving a form
-- never changes an amount by accident. The same rule is applied in Python (app/config.py AMOUNT_RECEIVED_DIVISOR) for the
-- O2D screens and the PDF reports; change both together if the rule ever changes.

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
    o.last_updated_at, round(o.amount_received / 100, 2)::numeric(10, 2) AS amount_received, o.cartage, o.is_cancelled,
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
    o.amount_received AS amount_received_entered
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
    o.last_updated_at, round(o.amount_received / 100, 2)::numeric(10, 2) AS amount_received, o.cartage, o.is_cancelled,
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
    o.amount_received AS amount_received_entered
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
