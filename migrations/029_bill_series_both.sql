-- 029: a submission type may belong to BOTH bill series. Its DC / Inv No holds the two numbers in one field, "challan/invoice"
-- (for example 12/345): one order, one record, counted in the Challan series (first number) and in the Invoice series (second).

DO $$
DECLARE c text;
BEGIN
    SELECT conname INTO c FROM pg_constraint
    WHERE conrelid = 'dim_submission_type'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%bill_series%';
    IF c IS NOT NULL THEN
        EXECUTE format('ALTER TABLE dim_submission_type DROP CONSTRAINT %I', c);
    END IF;
END $$;

ALTER TABLE dim_submission_type ADD CONSTRAINT dim_submission_type_bill_series_check
    CHECK (bill_series IN ('challan', 'invoice', 'both'));
