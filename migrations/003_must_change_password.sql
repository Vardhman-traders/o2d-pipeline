-- 003: force a password change on first login.
-- Every existing user gets TRUE: their old passwords were plain text in a shared Google Sheet.
ALTER TABLE dim_user ADD COLUMN must_change_password boolean NOT NULL DEFAULT true;
