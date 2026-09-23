-- 006: makes two things that were previously hardcoded/env-only manageable from the
-- admin dashboard: the Apps Script client key, and which order fields each role may
-- write. Both fall back to their current code-side defaults until an admin touches
-- them via the new /admin endpoints, so nothing changes until someone opts in.

-- Small key/value store for secrets and settings the admin portal manages.
-- Values are stored as-is (the client key is itself a random secret; nothing here
-- is a password requiring a one-way hash).
CREATE TABLE app_config (
    key        varchar(100) PRIMARY KEY,
    value       text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by varchar(100)
);

-- Which fields each operating role may write on an order. Mirrors
-- app/roles.py's EDITABLE_FIELDS at the time this migration was written -
-- editing this table only takes effect once app/roles.py is switched to read
-- from it (see the follow-up code change).
CREATE TABLE role_field_permissions (
    role       varchar(30)  NOT NULL,
    field_name varchar(50)  NOT NULL,
    editable   boolean      NOT NULL DEFAULT true,
    PRIMARY KEY (role, field_name)
);

INSERT INTO role_field_permissions (role, field_name) VALUES
    ('shop', 'order_received_date'), ('shop', 'order_via_key'), ('shop', 'submission_type_key'),
    ('shop', 'dc_inv_no'), ('shop', 'shipping_location'), ('shop', 'detailed_remarks'),
    ('godown', 'ready_by_person_key'), ('godown', 'colour_making_person_key'), ('godown', 'delivery_status_key'),
    ('godown_dispatch', 'delivery_status_key'), ('godown_dispatch', 'material_delivery_datetime'),
    ('godown_dispatch', 'delivered_by_person_key'), ('godown_dispatch', 'cartage'),
    ('shop_dispatch', 'delivery_status_key'), ('shop_dispatch', 'material_delivery_datetime'),
    ('shop_dispatch', 'delivered_by_person_key'), ('shop_dispatch', 'cartage'),
    ('shop_dispatch', 'date_of_receiving'), ('shop_dispatch', 'payment_status_key'), ('shop_dispatch', 'amount_received'),
    ('receiving', 'date_of_receiving'), ('receiving', 'payment_status_key'), ('receiving', 'amount_received');
