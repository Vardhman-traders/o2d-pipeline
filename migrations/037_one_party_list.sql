-- 037: one simple Payments party list. A party is a name: it no longer belongs to a company or a transaction type (that split only came
-- from the old sheet's per-company dropdowns, and made one typed name create several parties). Duplicates of the same name are merged into
-- one party and every entry is pointed at the one that stays. Vendors (Purchase) were already a plain list.

CREATE TEMP TABLE party_keep AS
SELECT DISTINCT ON (lower(btrim(party_name))) lower(btrim(party_name)) AS n, party_key AS keep
FROM dim_party WHERE party_kind = 'payment'
ORDER BY lower(btrim(party_name)), (review_status = 'approved') DESC, party_key;

UPDATE fact_payment_txn t SET party_key = k.keep
FROM dim_party p JOIN party_keep k ON k.n = lower(btrim(p.party_name))
WHERE p.party_key = t.party_key AND p.party_kind = 'payment' AND t.party_key <> k.keep;

DELETE FROM dim_party p USING party_keep k
WHERE p.party_kind = 'payment' AND lower(btrim(p.party_name)) = k.n AND p.party_key <> k.keep;

UPDATE dim_party SET company_key = NULL, txn_type_key = NULL WHERE party_kind = 'payment';

-- Bulk upload of the party list (Setup > Import > Payments > Parties), undoable like the other uploads.
ALTER TABLE dim_party ADD COLUMN import_batch_id integer REFERENCES import_batch (batch_id);
CREATE INDEX idx_dim_party_import_batch ON dim_party (import_batch_id) WHERE import_batch_id IS NOT NULL;

ALTER TABLE import_batch DROP CONSTRAINT IF EXISTS import_batch_entity_check;
ALTER TABLE import_batch ADD CONSTRAINT import_batch_entity_check CHECK (entity IN (
    'orders', 'users', 'payments', 'tasks', 'purchases', 'parties',
    'decor_clients', 'decor_payments', 'decor_expenses', 'att_employees', 'att_attendance', 'att_advances', 'checklists'));
