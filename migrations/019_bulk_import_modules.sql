-- 019: bulk upload for the Payments, Delegation and Purchase modules (same flow as orders: validate -> import -> undo).
-- import_batch_id lets one upload be traced and removed again.

ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ALTER COLUMN entity TYPE varchar(30);
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN ('orders', 'users', 'payments', 'tasks', 'purchases'));

ALTER TABLE fact_payment_txn ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_task ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
ALTER TABLE fact_purchase_entry ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);

CREATE INDEX idx_payment_txn_import_batch ON fact_payment_txn (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_task_import_batch ON fact_task (import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE INDEX idx_purchase_import_batch ON fact_purchase_entry (import_batch_id) WHERE import_batch_id IS NOT NULL;
