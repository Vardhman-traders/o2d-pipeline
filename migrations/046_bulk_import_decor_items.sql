-- 046: Setup > Import can now load VT Decor items, photo/challan links and quotations (the old ItemsLog, PhotosLog, QuotationsLog sheets); one upload can be traced and undone like the others.

ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN (
    'orders', 'users', 'payments', 'tasks', 'purchases', 'purchases_pf', 'parties',
    'decor_clients', 'decor_payments', 'decor_expenses', 'decor_items', 'decor_photos', 'decor_quotations', 'att_employees', 'att_attendance', 'att_advances', 'checklists'));

ALTER TABLE fact_decor_item ADD COLUMN IF NOT EXISTS import_batch_id integer REFERENCES import_batch (batch_id);
CREATE INDEX IF NOT EXISTS idx_decor_item_import_batch ON fact_decor_item (import_batch_id) WHERE import_batch_id IS NOT NULL;

ALTER TABLE fact_decor_photo ADD COLUMN IF NOT EXISTS import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_decor_quotation ADD COLUMN IF NOT EXISTS import_batch_id integer REFERENCES import_batch (batch_id);
CREATE INDEX IF NOT EXISTS idx_decor_photo_import_batch ON fact_decor_photo (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decor_quotation_import_batch ON fact_decor_quotation (import_batch_id) WHERE import_batch_id IS NOT NULL;
