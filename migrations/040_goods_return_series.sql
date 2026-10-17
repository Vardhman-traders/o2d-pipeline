-- 040: follow-up to 039 (039 was already applied, so it is left exactly as it ran).
-- The bill_series column was varchar(10), too small for 'sales_return_challan'; and a submission type can now be tagged "Goods Return"
-- (the order form asks Challan or Invoice and the order goes to the matching Sales Return series). Safe to run twice.

ALTER TABLE dim_submission_type ALTER COLUMN bill_series TYPE varchar(30);

ALTER TABLE dim_submission_type DROP CONSTRAINT IF EXISTS dim_submission_type_bill_series_check;
ALTER TABLE dim_submission_type ADD CONSTRAINT dim_submission_type_bill_series_check
    CHECK (bill_series IN ('challan', 'invoice', 'both', 'sales_return_challan', 'sales_return_invoice', 'goods_return'));
