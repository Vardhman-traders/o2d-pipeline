-- 025: three delivery-status settings (Setup > Dropdown values > Delivery status).
--   dispatch_route - where an order goes once the godown picks this status:
--       godown = Godown Dispatch "Ready for dispatch", shop = Shop Dispatch & Receiving "Ready for dispatch",
--       none   = it stays with the godown ("Under processing") and appears on no dispatch screen.
--   amount_effect  - 'subtract' marks a sales return (Goods Return): its amount is subtracted instead of added in every report.
--   screens        - "Dispatch for Shop" (stored as Shop) is offered on the Godown dashboard only.

ALTER TABLE dim_delivery_status ADD COLUMN dispatch_route varchar(10) NOT NULL DEFAULT 'godown' CHECK (dispatch_route IN ('godown', 'shop', 'none'));
ALTER TABLE dim_delivery_status ADD COLUMN amount_effect varchar(10) CHECK (amount_effect IN ('subtract'));

UPDATE dim_delivery_status SET dispatch_route = 'shop' WHERE lower(btrim(status_name)) IN ('shop', 'cancelled');
UPDATE dim_delivery_status SET dispatch_route = 'none'
WHERE lower(btrim(status_name)) IN ('processing', 'delivery for nextday', 'delivery for next day', 'order delivery for next day');
UPDATE dim_delivery_status SET amount_effect = 'subtract' WHERE lower(btrim(status_name)) = 'goods return';
UPDATE dim_delivery_status SET screens = ARRAY['godown'] WHERE lower(btrim(status_name)) = 'shop' AND screens IS NULL;

-- The two built-in statuses keep their meaning however they get created (Setup, a bulk import, a fresh database): Shop and Cancelled go to
-- Shop Dispatch & Receiving, and "Dispatch for Shop" is offered on the Godown dashboard only.
CREATE FUNCTION delivery_status_builtins() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF lower(btrim(NEW.status_name)) IN ('shop', 'cancelled') THEN
        NEW.dispatch_route := 'shop';
    END IF;
    IF lower(btrim(NEW.status_name)) = 'shop' AND NEW.screens IS NULL THEN
        NEW.screens := ARRAY['godown'];
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_delivery_status_builtins BEFORE INSERT ON dim_delivery_status FOR EACH ROW EXECUTE FUNCTION delivery_status_builtins();

-- The admin views now also apply the sales-return sign (the 022 rule - entered / 100 - is unchanged).
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
