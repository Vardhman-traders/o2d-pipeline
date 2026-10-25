-- 052: what was read from the challan / invoice attached to an order (the "PERFECT" check).
--
-- One row per attachment, written when the file is read (a PDF's text, or - only if the admin switched the picture reader on - a photo).
-- Only what the document shows is kept here; whether it matches the order is worked out against the order as it is NOW each time it is asked
-- for, so changing the order's DC / Invoice number or date afterwards removes the PERFECT mark by itself.
-- Rows go with their attachment or order. See app/doc_check.py.

CREATE TABLE order_doc_read (
    attachment_key     bigint PRIMARY KEY REFERENCES order_attachments (attachment_key) ON DELETE CASCADE,
    order_key          integer NOT NULL REFERENCES fact_orders (order_key) ON DELETE CASCADE,
    method             varchar(12) NOT NULL CHECK (method IN ('pdf_text', 'vision', 'none')),
    readable           boolean NOT NULL,
    doc_numbers        jsonb NOT NULL DEFAULT '[]',
    doc_dates          jsonb NOT NULL DEFAULT '[]',
    doc_dates_labelled jsonb NOT NULL DEFAULT '[]',
    doc_total          numeric(14, 2),
    note               varchar(300),
    read_at            timestamptz NOT NULL DEFAULT now(),
    read_by_key        integer REFERENCES dim_user (user_key) ON DELETE SET NULL
);
CREATE INDEX idx_order_doc_read_order ON order_doc_read (order_key);
