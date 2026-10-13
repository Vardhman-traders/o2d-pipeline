-- 030: an order is tagged to ONE bill series, never both. A submission type set to "Both" now means "either - the shop picks
-- Challan or Invoice on the order form". The order's pick is stored here and decides which series (and which missing-number
-- list) its DC / Inv No counts in. DC / Inv No is a single number again. (The old "12/345" format keeps counting for orders
-- already saved that way; see doc_numbers.number_in.)

ALTER TABLE fact_orders ADD COLUMN bill_series varchar(10) CHECK (bill_series IN ('challan', 'invoice'));

-- The built-in Cancelled type may be tagged to either series, like any "Both" type. It cannot be edited on screen, so it is set here.
UPDATE dim_submission_type SET bill_series = 'both' WHERE lower(btrim(type_name)) = 'cancelled';

CREATE OR REPLACE FUNCTION submission_type_series_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.bill_series IS NULL THEN
        IF lower(btrim(NEW.type_name)) = 'challan' THEN
            NEW.bill_series := 'challan';
        ELSIF lower(btrim(NEW.type_name)) = 'invoice' THEN
            NEW.bill_series := 'invoice';
        ELSIF lower(btrim(NEW.type_name)) = 'cancelled' THEN
            NEW.bill_series := 'both';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
