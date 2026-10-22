-- 051: indexes for "which merge made this Payments entry?".
--
-- Every Payments list row and the Payments dashboard ask whether a transaction came from an O2D / VT Decor merge, by looking it up in
-- payment_push through cash_txn_key or bank_txn_key. Neither column had an index, so each lookup read the whole of payment_push - once per
-- transaction row, on every 30-second refresh of every open Payments tab - and got slower as both tables grew. The queries now probe each
-- column separately (payments.py _pushed), and these two indexes make each probe a single index lookup.

CREATE INDEX IF NOT EXISTS idx_payment_push_cash_txn ON payment_push (cash_txn_key) WHERE cash_txn_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payment_push_bank_txn ON payment_push (bank_txn_key) WHERE bank_txn_key IS NOT NULL;
