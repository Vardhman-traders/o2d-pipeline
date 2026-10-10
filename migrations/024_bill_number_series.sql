-- 024: which submission types are numbered bills. The missing-number panel and the "digits only / no repeat" rules used to
-- recognise only types literally named Challan and Invoice; now each submission type says (Setup > Dropdown values > Submission
-- type > "Bill number series") whether it belongs to the daily Challan series, the yearly Invoice series, or neither.

ALTER TABLE dim_submission_type ADD COLUMN bill_series varchar(10) CHECK (bill_series IN ('challan', 'invoice'));

-- Keep working exactly as before for types named the old way.
UPDATE dim_submission_type SET bill_series = 'challan' WHERE lower(btrim(type_name)) = 'challan';
UPDATE dim_submission_type SET bill_series = 'invoice' WHERE lower(btrim(type_name)) = 'invoice';
