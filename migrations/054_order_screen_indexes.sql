-- 054: indexes for the order screens.
--
-- Shop, Godown, Dispatch and Receiving each ask for "the open orders, newest first" and "the closed orders, most recently updated first" every
-- 20 seconds on every open tab (app/o2d_screens.py _open_then_closed). With nothing to search by, each of those two queries read the whole of
-- fact_orders and sorted it - about 30 ms at 30,000 orders and growing with every order. These indexes hold exactly the rows the two queries
-- walk (live orders only), so they stop at the first page of rows they need. The open-orders index lists only the orders still open, which
-- stays small however many orders are closed. The Overview board reads the same newest-first order.
-- (the closed test below is the same expression as roles._CLOSED: Postgres only uses a partial index for a query that says the same thing)

CREATE INDEX IF NOT EXISTS idx_fact_orders_open ON fact_orders (timestamp_created DESC, order_key DESC)
WHERE archived_at IS NULL
AND NOT (material_delivery_datetime IS NOT NULL AND date_of_receiving_key IS NOT NULL AND payment_status_key IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_fact_orders_updated ON fact_orders (last_updated_at DESC, order_key DESC) WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_fact_orders_created ON fact_orders (timestamp_created DESC) WHERE archived_at IS NULL;
