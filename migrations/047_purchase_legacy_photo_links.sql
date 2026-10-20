-- 047: purchase entries imported from the old Purchase Report sheet keep their Google Drive photo links (shown with the entry's photos).
ALTER TABLE fact_purchase_entry ADD COLUMN IF NOT EXISTS legacy_photo_urls text;
