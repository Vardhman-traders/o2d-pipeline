-- 026: a submission type named Challan or Invoice is a numbered bill however it gets created (Setup, a bulk import, a fresh database) -
-- the same default migration 024 applied to the types that existed then. Any other type stays "not a numbered bill" until an admin sets it.

CREATE FUNCTION submission_type_series_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.bill_series IS NULL THEN
        IF lower(btrim(NEW.type_name)) = 'challan' THEN
            NEW.bill_series := 'challan';
        ELSIF lower(btrim(NEW.type_name)) = 'invoice' THEN
            NEW.bill_series := 'invoice';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_submission_type_series BEFORE INSERT ON dim_submission_type FOR EACH ROW EXECUTE FUNCTION submission_type_series_defaults();
