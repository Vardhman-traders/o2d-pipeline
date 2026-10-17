-- 039: two more bill number series for submission types: "Sales Return Challan" (daily, like Challan) and "Sales Return Invoice"
-- (one per financial year, like Invoice). Each is its own series; see app/doc_numbers.py. Safe to run twice.

DO $$
DECLARE c text;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'dim_submission_type'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%bill_series%'
    LOOP
        EXECUTE format('ALTER TABLE dim_submission_type DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE dim_submission_type ADD CONSTRAINT dim_submission_type_bill_series_check
    CHECK (bill_series IN ('challan', 'invoice', 'both', 'sales_return_challan', 'sales_return_invoice'));

-- "closed as no bill / voided" notes are kept per series, so they need the two new series names too
ALTER TABLE doc_gap_void ALTER COLUMN doc_type TYPE varchar(30);
ALTER TABLE doc_gap_void DROP CONSTRAINT IF EXISTS doc_gap_void_doc_type_check;
ALTER TABLE doc_gap_void ADD CONSTRAINT doc_gap_void_doc_type_check
    CHECK (doc_type IN ('Challan', 'Invoice', 'Sales Return Challan', 'Sales Return Invoice'));

-- Setup > Import: purchase entries in the Payments sheet layout are a separate upload kind (still one table, still undoable per batch)
ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN (
    'orders', 'users', 'payments', 'tasks', 'purchases', 'purchases_pf', 'parties',
    'decor_clients', 'decor_payments', 'decor_expenses', 'att_employees', 'att_attendance', 'att_advances', 'checklists'));
