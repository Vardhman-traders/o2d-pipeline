-- 045: every WhatsApp alert goes to a GROUP (Delegation / Checklist no longer message individual numbers), and the messages of the
-- "WhatsApp Template" document are set up:
--   * the dispatch message gets Cartage, the delivery person's phone, the extra text (By Porter / By Company / By Transport) and the photo link;
--     By Porter goes to its own Porter group, and every dispatch is also posted to the Receiving group (new alerts below);
--   * the automatic report PDFs go to a Reports group at 7:30 PM and 9:00 PM (two batches; daily_report_run now says which batch ran).
-- The group IDs stay empty until an admin types them in Setup > WhatsApp. Safe to run twice.

-- the old standard dispatch wording is replaced; a wording an admin edited is left alone
UPDATE whatsapp_alert SET template = E'New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\nDelivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\n{cartage_line}Delivered By: {delivered_by}\n{details_line}Photo: {photo}'
WHERE alert_key = 'dispatch_godown'
  AND template = E'New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\nDelivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\nDelivered By: {delivered_by}';

INSERT INTO whatsapp_alert (alert_key, label, enabled, template) VALUES
    ('dispatch_porter', 'Porter dispatch alert', true,
     E'New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\nDelivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\n{cartage_line}Delivered By: {delivered_by}\n{details_line}Photo: {photo}'),
    ('dispatch_receiving', 'Receiving group dispatch alert', true,
     E'New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\nDelivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\n{cartage_line}Delivered By: {delivered_by}\n{details_line}Photo: {photo}'),
    ('reports_evening', 'Daily reports - 7:30 PM', true,
     E'*Reports - {time}* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n📅 {date}\n\n{list}\n━━━━━━━━━━━━━━━━━━━━'),
    ('reports_night', 'Daily reports - 9:00 PM', true,
     E'*Reports - {time}* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n📅 {date}\n\n{list}\n━━━━━━━━━━━━━━━━━━━━')
ON CONFLICT (alert_key) DO NOTHING;

-- two report batches a day: the evening one (7:30 PM) and the night one (9:00 PM)
ALTER TABLE daily_report_run ADD COLUMN IF NOT EXISTS batch varchar(10) NOT NULL DEFAULT 'evening';
ALTER TABLE daily_report_run DROP CONSTRAINT IF EXISTS daily_report_run_pkey;
ALTER TABLE daily_report_run ADD PRIMARY KEY (report_date, batch);
