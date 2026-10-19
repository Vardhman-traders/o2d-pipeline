-- 044: the admin can correct a wrongly punched attendance day (Owner dashboard). Who corrected it, when and why stays on the day itself;
-- the old values are in the admin audit log. Safe to run twice.

ALTER TABLE fact_attendance ADD COLUMN IF NOT EXISTS corrected_at         timestamptz;
ALTER TABLE fact_attendance ADD COLUMN IF NOT EXISTS corrected_by_user_key integer REFERENCES dim_user (user_key) ON DELETE SET NULL;
ALTER TABLE fact_attendance ADD COLUMN IF NOT EXISTS correction_note      varchar(200);
