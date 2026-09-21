-- 002: support "get or create by name" from the app without case-variant duplicates.
-- Replace the case-sensitive UNIQUE constraints with case-insensitive unique indexes.

ALTER TABLE dim_order_channel   DROP CONSTRAINT IF EXISTS dim_order_channel_channel_name_key;
ALTER TABLE dim_submission_type DROP CONSTRAINT IF EXISTS dim_submission_type_type_name_key;
ALTER TABLE dim_delivery_status DROP CONSTRAINT IF EXISTS dim_delivery_status_status_name_key;
ALTER TABLE dim_payment_status  DROP CONSTRAINT IF EXISTS dim_payment_status_status_name_key;

CREATE UNIQUE INDEX uq_dim_order_channel_name   ON dim_order_channel   (lower(btrim(channel_name)));
CREATE UNIQUE INDEX uq_dim_submission_type_name ON dim_submission_type (lower(btrim(type_name)));
CREATE UNIQUE INDEX uq_dim_delivery_status_name ON dim_delivery_status (lower(btrim(status_name)));
CREATE UNIQUE INDEX uq_dim_payment_status_name  ON dim_payment_status  (lower(btrim(status_name)));

-- A person is identified by name + role (the same name may hold different roles).
CREATE UNIQUE INDEX uq_dim_person_name_role ON dim_person (lower(btrim(full_name)), person_role);
