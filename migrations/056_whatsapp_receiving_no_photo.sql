-- 056: WhatsApp messages without photos, and the Receiving group told at receiving.
--   * the "Photo: {photo}" line is taken out of the saved dispatch wordings (photos are no longer part of WhatsApp messages);
--   * the Receiving group alert used to repeat the dispatch message at dispatch time. It is now a receiving message, sent when the
--     receiving entry is made - a wording an admin has not changed (still the old dispatch text) is replaced by the new one.

UPDATE whatsapp_alert SET template = rtrim(replace(replace(template, E'\n{details_line}Photo: {photo}', E'\n{details_line}'), E'Photo: {photo}', ''), E'\n ')
WHERE template LIKE '%Photo: {photo}%';

UPDATE whatsapp_alert SET
    template = E'Order Received\n\nOrder Date: {order_date}\nDC No: {dc_no}\nAddress: {address}\nDelivered By: {delivered_by}\nReceived On: {received_date}\nPayment Mode: {payment_status}\n{amount_line}Entered By: {entered_by}',
    label = 'Receiving group alert'
WHERE alert_key = 'dispatch_receiving' AND template LIKE 'New Dispatch - Godown%';
