-- 055: the company is now called Code Blue. The WhatsApp message templates saved in Setup > WhatsApp (and seeded by earlier migrations)
-- carry the old name in their first line; change it wherever it is still there. Wording an admin typed themselves is left alone.

UPDATE whatsapp_alert SET template = replace(template, 'Vardhman Traders', 'Code Blue') WHERE template LIKE '%Vardhman Traders%';
