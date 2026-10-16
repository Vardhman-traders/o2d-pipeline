-- 036: Attendance employees follow the portal members. Every active member (except admin and legacy accounts) is also an attendance
-- employee, so the check-in dropdown, Setup > Members and the Attendance lists stay in step. user_key is the link; employees with no
-- login (the existing floor staff) keep user_key empty.

ALTER TABLE dim_att_employee ADD COLUMN user_key integer UNIQUE REFERENCES dim_user (user_key) ON DELETE SET NULL;
