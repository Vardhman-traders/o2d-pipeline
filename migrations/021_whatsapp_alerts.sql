-- 021: WhatsApp alert settings in the database (Setup > WhatsApp) instead of one environment variable per group.
-- One row per kind of alert: the group it goes to, an on/off switch and the message template.

CREATE TABLE whatsapp_alert (
    alert_key varchar(40) PRIMARY KEY,
    label varchar(100) NOT NULL,
    group_id varchar(100),
    enabled boolean NOT NULL DEFAULT true,
    template text NOT NULL,
    updated_by_user_key integer REFERENCES dim_user (user_key) ON DELETE SET NULL,
    updated_at timestamptz
);

-- The group stays empty: until an admin sets it, the existing app_config / WHATSAPP_GROUP_ID value keeps being used.
INSERT INTO whatsapp_alert (alert_key, label, template)
SELECT
    'dispatch_godown' AS alert_key,
    'Godown dispatch alert' AS label,
    concat_ws(
        chr(10),
        'New Dispatch - Godown',
        '',
        'Order Date: {order_date}',
        'DC No: {dc_no}',
        'Ready By: {ready_by}',
        'Delivery Status: {delivery_status}',
        'Address: {address}',
        'Remarks: {remarks}',
        'Delivered By: {delivered_by}'
    ) AS template;
