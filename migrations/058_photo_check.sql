-- 058: photo check on All orders.
--   sha256 - a fingerprint of each photo taken from now on. The same picture attached to two different orders is almost always a wrong photo,
--   so All orders > Photo check lists those next to the orders that have no photo at all. Photos uploaded before this have no fingerprint.

ALTER TABLE order_attachments ADD COLUMN sha256 char(64);
CREATE INDEX idx_order_attachments_sha ON order_attachments (sha256) WHERE sha256 IS NOT NULL;
