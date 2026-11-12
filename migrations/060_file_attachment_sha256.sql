-- 060: wrong-photo check at upload. file_attachment (VT Decor, Purchase, challan uploads) gets the same photo fingerprint order photos
--   have had since 058, so the same picture attached twice is refused with a message instead of being found later.

ALTER TABLE file_attachment ADD COLUMN sha256 char(64);
CREATE INDEX idx_file_attachment_sha ON file_attachment (module, sha256) WHERE sha256 IS NOT NULL;
