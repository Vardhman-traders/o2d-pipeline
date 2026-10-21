-- 050: VT Decor money goes through the same "merge" step into Payments as the O2D Sales / Sales Return rows.
--
-- Every VT Decor payment (money received from a client) and every VT Decor expense entered on the VT Decor dashboard is staged in payment_push
-- (kind decor_in / decor_out). On the Payments page two more tabs list them: whoever works the tab adds the company / party / remarks and merges,
-- which creates the normal Pending Payments entries (a Cash and / or a Bank entry, tagged Source = "VT Decor"), so they add to Total Received /
-- Cash Paid, Cash in Hand and Bank like any entry. Editing or deleting the VT Decor entry afterwards brings the row back as "already merged"
-- (an edit) or flags the Payments entries (a delete), exactly like an O2D order that changes after it was merged.
-- Entries loaded by a bulk import (Setup > Import) are history and are NOT staged.

ALTER TABLE payment_push DROP CONSTRAINT payment_push_order_key_key;
ALTER TABLE payment_push ALTER COLUMN order_key DROP NOT NULL;
CREATE UNIQUE INDEX uq_payment_push_order ON payment_push (order_key) WHERE order_key IS NOT NULL;

ALTER TABLE payment_push DROP CONSTRAINT payment_push_kind_check;
ALTER TABLE payment_push ALTER COLUMN kind TYPE varchar(10);
ALTER TABLE payment_push ALTER COLUMN merged_kind TYPE varchar(10);
ALTER TABLE payment_push ADD CONSTRAINT payment_push_kind_check CHECK (kind IN ('sales', 'return', 'decor_in', 'decor_out'));

ALTER TABLE payment_push
    ADD COLUMN decor_payment_key bigint REFERENCES fact_decor_payment (payment_key) ON DELETE SET NULL,
    ADD COLUMN decor_expense_key bigint REFERENCES fact_decor_expense (expense_key) ON DELETE SET NULL,
    ADD COLUMN decor_label       varchar(40),          -- the client code (C001), shown where an order shows its DC / Inv no
    ADD COLUMN decor_info        text,                 -- client name and stage / expense type, shown where an order shows its address
    ADD COLUMN decor_split       varchar(4) CHECK (decor_split IN ('cash', 'bank', 'both'));
CREATE UNIQUE INDEX uq_payment_push_decor_payment ON payment_push (decor_payment_key) WHERE decor_payment_key IS NOT NULL;
CREATE UNIQUE INDEX uq_payment_push_decor_expense ON payment_push (decor_expense_key) WHERE decor_expense_key IS NOT NULL;

-- Stage, or update, the row of one VT Decor payment (p_kind decor_in) or expense (decor_out).
CREATE FUNCTION decor_push_upsert(p_kind text, p_pay bigint, p_exp bigint, p_client integer, p_date date, p_amount numeric,
                                  p_split text, p_what text, p_user integer) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    r    payment_push%ROWTYPE;
    code text;
    nm   text;
    cash numeric;
    info text;
BEGIN
    SELECT client_code, client_name INTO code, nm FROM dim_decor_client WHERE client_key = p_client;
    info := nm || COALESCE(' · ' || NULLIF(p_what, ''), '');
    cash := CASE WHEN p_split = 'cash' OR p_split = 'both' THEN p_amount ELSE 0 END;
    SELECT * INTO r FROM payment_push
    WHERE (p_pay IS NOT NULL AND decor_payment_key = p_pay) OR (p_exp IS NOT NULL AND decor_expense_key = p_exp) FOR UPDATE;
    IF p_amount IS NULL OR p_amount <= 0 THEN        -- nothing to merge (a refund / reversal is entered in Payments by hand)
        IF FOUND AND r.merged_at IS NULL THEN DELETE FROM payment_push WHERE push_key = r.push_key; END IF;
        RETURN;
    END IF;
    IF NOT FOUND THEN
        INSERT INTO payment_push (kind, txn_date, cash_amount, bank_amount, order_amount, pushed_by_user_key, decor_payment_key, decor_expense_key,
                                  decor_label, decor_info, decor_split)
        VALUES (p_kind, p_date, cash, p_amount - cash, p_amount, p_user, p_pay, p_exp, code, info, p_split);
    ELSIF r.merged_at IS NULL THEN
        UPDATE payment_push SET txn_date = p_date, cash_amount = cash, bank_amount = p_amount - cash, order_amount = p_amount,
               decor_label = code, decor_info = info, decor_split = p_split WHERE push_key = r.push_key;
    ELSE                                              -- already merged: comes back for review, the entries it made carry a warning
        UPDATE payment_push SET status = 'awaiting', edited_after_merge = true, order_amount = p_amount, decor_label = code, decor_info = info,
               decor_split = p_split, cash_amount = CASE WHEN p_split = 'both' THEN r.cash_amount ELSE cash END,
               bank_amount = CASE WHEN p_split = 'both' THEN r.bank_amount ELSE p_amount - cash END WHERE push_key = r.push_key;
        UPDATE fact_payment_txn SET order_warning = 'VT Decor entry changed after this was merged - please review'
        WHERE txn_key IN (r.cash_txn_key, r.bank_txn_key);
    END IF;
END
$$;

CREATE FUNCTION decor_payment_stage() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    is_cash boolean;
    what    text;
BEGIN
    SELECT COALESCE((SELECT m.is_cash FROM dim_payment_mode m WHERE m.mode_key = NEW.mode_key), true) INTO is_cash;
    SELECT COALESCE((SELECT s.stage_name FROM dim_decor_stage s WHERE s.stage_key = NEW.stage_key), NEW.stage_text) INTO what;
    PERFORM decor_push_upsert('decor_in', NEW.payment_key, NULL, NEW.client_key, NEW.pay_date, NEW.amount,
                              CASE WHEN is_cash THEN 'cash' ELSE 'bank' END, what, NEW.created_by_user_key);
    RETURN NEW;
END
$$;

CREATE FUNCTION decor_expense_stage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM decor_push_upsert('decor_out', NULL, NEW.expense_key, NEW.client_key, NEW.exp_date, NEW.amount, 'both', NEW.expense_type,
                              NEW.created_by_user_key);
    RETURN NEW;
END
$$;

-- A deleted VT Decor entry: a row nobody merged yet simply goes; a merged one stays, and the Payments entries it made carry a warning.
CREATE FUNCTION decor_unstage() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    r payment_push%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'fact_decor_payment' THEN
        SELECT * INTO r FROM payment_push WHERE decor_payment_key = OLD.payment_key;
    ELSE
        SELECT * INTO r FROM payment_push WHERE decor_expense_key = OLD.expense_key;
    END IF;
    IF FOUND THEN
        IF r.merged_at IS NULL THEN
            DELETE FROM payment_push WHERE push_key = r.push_key;
        ELSE
            UPDATE fact_payment_txn SET order_warning = 'The VT Decor entry behind this was deleted - please review'
            WHERE txn_key IN (r.cash_txn_key, r.bank_txn_key);
            UPDATE payment_push SET status = 'merged', edited_after_merge = false WHERE push_key = r.push_key;
        END IF;
    END IF;
    RETURN OLD;
END
$$;

CREATE TRIGGER trg_decor_payment_stage AFTER INSERT OR UPDATE ON fact_decor_payment
    FOR EACH ROW WHEN (NEW.import_batch_id IS NULL) EXECUTE FUNCTION decor_payment_stage();
CREATE TRIGGER trg_decor_expense_stage AFTER INSERT OR UPDATE ON fact_decor_expense
    FOR EACH ROW WHEN (NEW.import_batch_id IS NULL) EXECUTE FUNCTION decor_expense_stage();
CREATE TRIGGER trg_decor_payment_unstage BEFORE DELETE ON fact_decor_payment FOR EACH ROW EXECUTE FUNCTION decor_unstage();
CREATE TRIGGER trg_decor_expense_unstage BEFORE DELETE ON fact_decor_expense FOR EACH ROW EXECUTE FUNCTION decor_unstage();
