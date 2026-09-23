-- 007: whether a role may view all orders (read-only) is now an admin-controlled
-- setting, not something baked into code. Defaults to FALSE for every role - nobody
-- gets visibility until admin explicitly grants it from Setup -> Permissions.
-- This only applies to roles whose visibility isn't already defined by the core
-- operational business logic in roles.py (shop/godown/shop_dispatch/godown_dispatch/
-- receiving each have their own workflow-stage visibility rule; admin always sees
-- everything) - it's for everything else: cashier, accounts, cartage, and any future
-- role that isn't part of daily order operations.

CREATE TABLE role_view_access (
    role      varchar(30) PRIMARY KEY,
    can_view  boolean     NOT NULL DEFAULT false
);

INSERT INTO role_view_access (role, can_view) VALUES
    ('cashier', false), ('accounts', false), ('cartage', false);
