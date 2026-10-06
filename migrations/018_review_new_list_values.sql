-- 018: values created from the module screens (a new company, payment party or vendor typed into a form) are usable at once,
-- but wait in Setup > Dropdown values for an admin to review them (approve, rename, or merge into the right spelling).
-- Values an admin creates in Setup, and everything that existed before this migration, are approved.
ALTER TABLE dim_company
    ADD COLUMN review_status varchar(10) NOT NULL DEFAULT 'approved' CHECK (review_status IN ('pending', 'approved')),
    ADD COLUMN created_by_user_key integer REFERENCES dim_user (user_key) ON DELETE SET NULL,
    ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE dim_party
    ADD COLUMN review_status varchar(10) NOT NULL DEFAULT 'approved' CHECK (review_status IN ('pending', 'approved')),
    ADD COLUMN created_by_user_key integer REFERENCES dim_user (user_key) ON DELETE SET NULL,
    ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX idx_dim_company_pending ON dim_company (review_status) WHERE review_status = 'pending';
CREATE INDEX idx_dim_party_pending ON dim_party (review_status) WHERE review_status = 'pending';
