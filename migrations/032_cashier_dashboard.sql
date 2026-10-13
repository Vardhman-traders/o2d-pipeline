-- 032: Cashier dashboard (O2D).
--   dim_payment_status.cashier_review - Setup > Dropdown values > Payment status > "Cashier dashboard": 'yes' puts every order
--       that gets this payment status (on Shop Dispatch & Receiving or Godown Receiving) on the Cashier dashboard.
--       Starts ticked for the existing Cash, Bank and Cash + Bank values; admin can tick or untick any value later.
--   fact_orders.cashier_confirmed_* - the cashier's "money received" confirmation for the order (who, when, optional note).
--       It is cleared automatically if the payment status or the amount received is changed afterwards, so the cashier
--       always confirms what is currently recorded.
--   page_access - the cashier role opens the new page "o2d_cashier" by default (admin can untick it in Setup > Access).

ALTER TABLE dim_payment_status ADD COLUMN cashier_review varchar(3) CHECK (cashier_review IN ('yes'));

UPDATE dim_payment_status SET cashier_review = 'yes'
WHERE lower(regexp_replace(btrim(status_name), '\s+', ' ', 'g')) IN ('cash', 'bank', 'cash + bank', 'cash+bank', 'cash and bank', 'cash & bank');

ALTER TABLE fact_orders
    ADD COLUMN cashier_confirmed_at          timestamptz,
    ADD COLUMN cashier_confirmed_by_user_key integer REFERENCES dim_user (user_key),
    ADD COLUMN cashier_note                  varchar(300);

CREATE INDEX idx_fact_orders_cashier_confirmed ON fact_orders (cashier_confirmed_at) WHERE cashier_confirmed_at IS NOT NULL;

CREATE FUNCTION cashier_confirmation_reset() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.payment_status_key IS DISTINCT FROM OLD.payment_status_key OR NEW.amount_received IS DISTINCT FROM OLD.amount_received)
       AND NEW.cashier_confirmed_at IS NOT DISTINCT FROM OLD.cashier_confirmed_at THEN
        NEW.cashier_confirmed_at := NULL;
        NEW.cashier_confirmed_by_user_key := NULL;
        NEW.cashier_note := NULL;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_cashier_confirmation_reset BEFORE UPDATE ON fact_orders
    FOR EACH ROW EXECUTE FUNCTION cashier_confirmation_reset();

INSERT INTO page_access (page_key, role) VALUES ('o2d_cashier', 'cashier') ON CONFLICT DO NOTHING;
