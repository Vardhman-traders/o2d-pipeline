-- 031: bulk import for VT Decor and Attendance (Setup > Import). import_batch_id lets one upload be traced and undone.

ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN (
    'orders', 'users', 'payments', 'tasks', 'purchases',
    'decor_clients', 'decor_payments', 'decor_expenses', 'att_employees', 'att_attendance', 'att_advances'));

ALTER TABLE dim_decor_client ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_decor_payment ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_decor_expense ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE dim_att_employee ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_attendance ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_att_advance ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);

CREATE INDEX idx_decor_client_import_batch ON dim_decor_client (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_decor_payment_import_batch ON fact_decor_payment (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_decor_expense_import_batch ON fact_decor_expense (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_att_employee_import_batch ON dim_att_employee (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_attendance_import_batch ON fact_attendance (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_att_advance_import_batch ON fact_att_advance (import_batch_id) WHERE import_batch_id IS NOT NULL;

-- The Attendance check-in page needs no sign-in (it is a button on the login page), so it is no longer a page in Access & permissions.
DELETE FROM page_access WHERE page_key = 'att_mark';
DELETE FROM user_page_access WHERE page_key = 'att_mark';
DELETE FROM access_request WHERE page_key = 'att_mark';
