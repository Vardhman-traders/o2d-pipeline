-- 020: every dropdown list can be put in a chosen order (drag and drop in Setup > Dropdown values), and Delivery status /
-- Payment status values can be limited to the screens that offer them.
--
-- ensure_sort_order(table, name column, backfill) is the ONE thing a future dropdown list needs: it adds the sort_order column,
-- numbers the existing values A-Z (so nothing changes until someone drags), and installs a trigger that puts every newly
-- added value at the end of the list - whichever screen or import adds it.

CREATE OR REPLACE FUNCTION assign_sort_order() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sort_order IS NULL OR NEW.sort_order = 0 THEN
        EXECUTE format('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM %I', TG_TABLE_NAME) INTO NEW.sort_order;
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ensure_sort_order(tbl regclass, name_col text, backfill boolean DEFAULT true) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS sort_order integer NOT NULL DEFAULT 0', tbl);
    IF backfill THEN
        EXECUTE format('UPDATE %s t SET sort_order = r.n FROM (SELECT ctid AS id, row_number() OVER (ORDER BY lower(%I)) AS n '
                       'FROM %s WHERE sort_order = 0) r WHERE t.ctid = r.id', tbl, name_col, tbl);
    END IF;
    EXECUTE format('DROP TRIGGER IF EXISTS trg_sort_order ON %s', tbl);
    EXECUTE format('CREATE TRIGGER trg_sort_order BEFORE INSERT ON %s FOR EACH ROW EXECUTE FUNCTION assign_sort_order()', tbl);
END
$$;

SELECT ensure_sort_order('dim_order_channel', 'channel_name');
SELECT ensure_sort_order('dim_submission_type', 'type_name');
SELECT ensure_sort_order('dim_delivery_status', 'status_name');
SELECT ensure_sort_order('dim_payment_status', 'status_name');
SELECT ensure_sort_order('dim_person', 'full_name');
SELECT ensure_sort_order('dim_party', 'party_name');
-- these three already had a position; they only need the "new value goes last" trigger
SELECT ensure_sort_order('dim_company', 'company_name', false);
SELECT ensure_sort_order('dim_payment_mode', 'mode_name', false);
SELECT ensure_sort_order('dim_txn_type', 'type_name', false);

-- Which screens offer a value. NULL = every screen (what all existing values keep doing).
-- Screen ids are the O2D roles: godown, godown_dispatch, shop_dispatch, receiving.
ALTER TABLE dim_delivery_status ADD COLUMN screens text[];
ALTER TABLE dim_payment_status ADD COLUMN screens text[];
