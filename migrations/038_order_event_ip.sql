-- 038: record the IP address a change to an order came from (shown next to who/when in the order timeline).
-- Nullable: events written before this migration, and system events (e.g. the WhatsApp alert thread), have no IP.

ALTER TABLE order_event ADD COLUMN by_ip varchar(64);
