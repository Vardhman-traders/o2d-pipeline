-- 005: archiving. Orders that are at least 7 days old (by order date) and "fully
-- complete" (stage = Closed or Cancelled) are marked archived_at and disappear from
-- every operating screen, the API and the dashboard. They still exist in the same
-- table, and are queryable in full (active + archived) via v_orders_archive, for
-- anyone with direct database access only - no front-end view of archived rows.

ALTER TABLE fact_orders ADD COLUMN archived_at timestamptz;

-- Almost every query filters "archived_at IS NULL" (the operational view), so a
-- partial index keeps that the cheap case; archived rows are only ever touched by
-- the archiving job itself and ad-hoc SQL against v_orders_archive.
CREATE INDEX idx_fact_orders_active ON fact_orders (order_received_date_key) WHERE archived_at IS NULL;

-- v_orders (used by the API and the admin dashboard) now only shows active orders.
CREATE OR REPLACE VIEW v_orders AS
SELECT
    o.order_key, o.sl_no, o.dc_inv_no,
    d.full_date                   AS order_date,
    c.channel_name                AS channel,
    st.type_name                  AS submission_type,
    ds.status_name                AS delivery_status,
    ps.status_name                AS payment_status,
    rp.full_name                  AS ready_by,
    cp.full_name                  AS colour_making_by,
    dp.full_name                  AS delivered_by,
    dp.phone_number               AS delivered_by_phone,
    cu.display_name               AS created_by,
    lu.display_name               AS last_updated_by,
    o.shipping_location, o.detailed_remarks,
    o.timestamp_created, o.material_delivery_datetime,
    rd.full_date                  AS date_of_receiving,
    o.last_updated_at, o.amount_received, o.cartage, o.is_cancelled,
    CASE
        WHEN o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled' THEN 'Cancelled'
        WHEN o.delivery_status_key IS NULL                              THEN 'Awaiting godown'
        WHEN o.material_delivery_datetime IS NULL                       THEN 'Awaiting dispatch'
        WHEN o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL THEN 'Awaiting receiving'
        ELSE 'Closed'
    END AS stage,
    CASE
        WHEN NOT (o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled')
             AND o.material_delivery_datetime >= o.timestamp_created
        THEN EXTRACT(EPOCH FROM (o.material_delivery_datetime - o.timestamp_created)) / 3600.0
    END AS hours_to_deliver,
    o.archived_at
FROM fact_orders o
JOIN dim_date d ON d.date_key = o.order_received_date_key
LEFT JOIN dim_date rd            ON rd.date_key = o.date_of_receiving_key
LEFT JOIN dim_order_channel c    ON c.channel_key = o.order_via_key
LEFT JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key
LEFT JOIN dim_delivery_status ds ON ds.status_key = o.delivery_status_key
LEFT JOIN dim_payment_status ps  ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_person rp          ON rp.person_key = o.ready_by_person_key
LEFT JOIN dim_person cp          ON cp.person_key = o.colour_making_person_key
LEFT JOIN dim_person dp          ON dp.person_key = o.delivered_by_person_key
LEFT JOIN dim_user cu            ON cu.user_key = o.created_by_user_key
LEFT JOIN dim_user lu            ON lu.user_key = o.last_updated_by_user_key
WHERE o.archived_at IS NULL;

-- Full history (active + archived), SQL access only - never exposed via the API.
CREATE VIEW v_orders_archive AS
SELECT
    o.order_key, o.sl_no, o.dc_inv_no,
    d.full_date                   AS order_date,
    c.channel_name                AS channel,
    st.type_name                  AS submission_type,
    ds.status_name                AS delivery_status,
    ps.status_name                AS payment_status,
    rp.full_name                  AS ready_by,
    cp.full_name                  AS colour_making_by,
    dp.full_name                  AS delivered_by,
    dp.phone_number               AS delivered_by_phone,
    cu.display_name               AS created_by,
    lu.display_name               AS last_updated_by,
    o.shipping_location, o.detailed_remarks,
    o.timestamp_created, o.material_delivery_datetime,
    rd.full_date                  AS date_of_receiving,
    o.last_updated_at, o.amount_received, o.cartage, o.is_cancelled,
    CASE
        WHEN o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled' THEN 'Cancelled'
        WHEN o.delivery_status_key IS NULL                              THEN 'Awaiting godown'
        WHEN o.material_delivery_datetime IS NULL                       THEN 'Awaiting dispatch'
        WHEN o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL THEN 'Awaiting receiving'
        ELSE 'Closed'
    END AS stage,
    CASE
        WHEN NOT (o.is_cancelled OR lower(btrim(st.type_name)) = 'cancelled')
             AND o.material_delivery_datetime >= o.timestamp_created
        THEN EXTRACT(EPOCH FROM (o.material_delivery_datetime - o.timestamp_created)) / 3600.0
    END AS hours_to_deliver,
    o.archived_at
FROM fact_orders o
JOIN dim_date d ON d.date_key = o.order_received_date_key
LEFT JOIN dim_date rd            ON rd.date_key = o.date_of_receiving_key
LEFT JOIN dim_order_channel c    ON c.channel_key = o.order_via_key
LEFT JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key
LEFT JOIN dim_delivery_status ds ON ds.status_key = o.delivery_status_key
LEFT JOIN dim_payment_status ps  ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_person rp          ON rp.person_key = o.ready_by_person_key
LEFT JOIN dim_person cp          ON cp.person_key = o.colour_making_person_key
LEFT JOIN dim_person dp          ON dp.person_key = o.delivered_by_person_key
LEFT JOIN dim_user cu            ON cu.user_key = o.created_by_user_key
LEFT JOIN dim_user lu            ON lu.user_key = o.last_updated_by_user_key;
