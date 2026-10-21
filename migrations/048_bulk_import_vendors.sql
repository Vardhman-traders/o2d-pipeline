-- 048: bulk upload of the Purchase vendor list (Setup > Import > Purchase > Parties), undoable like the other uploads.
ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN (
    'orders', 'users', 'payments', 'tasks', 'purchases', 'purchases_pf', 'parties', 'vendors',
    'decor_clients', 'decor_payments', 'decor_expenses', 'decor_items', 'decor_photos', 'decor_quotations', 'att_employees', 'att_attendance', 'att_advances', 'checklists'));
