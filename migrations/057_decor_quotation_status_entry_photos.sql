-- 057: VT Decor.
--   * a quotation is a Lead until it is Approved (a client is created from it) or Lost; the administrator sees how many of each;
--   * a photo can belong to one payment or one expense (it shows next to that entry), not only to the client.

ALTER TABLE fact_decor_quotation
    ADD COLUMN status     varchar(10) NOT NULL DEFAULT 'Lead' CHECK (status IN ('Lead', 'Approved', 'Lost')),
    ADD COLUMN client_key integer REFERENCES dim_decor_client (client_key) ON DELETE SET NULL,
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN decided_by varchar(60);
CREATE INDEX idx_decor_quotation_status ON fact_decor_quotation (status);

ALTER TABLE fact_decor_photo
    ADD COLUMN payment_key bigint REFERENCES fact_decor_payment (payment_key) ON DELETE SET NULL,
    ADD COLUMN expense_key bigint REFERENCES fact_decor_expense (expense_key) ON DELETE SET NULL;
CREATE INDEX idx_decor_photo_payment ON fact_decor_photo (payment_key) WHERE payment_key IS NOT NULL;
CREATE INDEX idx_decor_photo_expense ON fact_decor_photo (expense_key) WHERE expense_key IS NOT NULL;
