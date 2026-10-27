-- 053: advance amount taken at the shop counter.
--   The shop can enter the amount and payment mode (a payment status that goes to the Cashier dashboard) when it punches the order.
--   They are saved in the same two fields the receiving screens use (fact_orders.amount_received / payment_status_key), so the Cashier
--   dashboard, the Payments merge and every report work exactly as before - only the entry point moves earlier.
--   amount_locked_* - set when the shop entered the amount: from then on Shop Dispatch & Receiving and Godown Receiving cannot change the
--       amount or the payment mode. The cashier may correct them while the entry is still waiting to be merged; after that only an admin.

ALTER TABLE fact_orders
    ADD COLUMN amount_locked_at          timestamptz,
    ADD COLUMN amount_locked_by_user_key integer REFERENCES dim_user (user_key),
    ADD COLUMN amount_locked_stage       varchar(10) CHECK (amount_locked_stage IN ('shop'));
