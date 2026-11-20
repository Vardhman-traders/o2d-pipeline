# ruff: noqa: E501
"""Batch bookkeeping for the encrypted export: which batch the other side is known to have, and what every exported row looked like then.

Lifecycle of a batch:  building -> pending (file made, not yet confirmed) -> confirmed (an admin says it was uploaded and checked)
A new export is always built against the last CONFIRMED batch. A batch that was made but never confirmed is superseded by the next
export, so nothing is ever skipped just because a file was created: the next delta repeats it, and applying the same rows twice is harmless.
"""
from psycopg2.extras import execute_values

CHUNK = 5000


def confirmed(cur) -> dict | None:
    """The last confirmed batch (batch_no, kind, summary) or None."""
    cur.execute("SELECT batch_no, kind, summary FROM vault_batch WHERE status = 'confirmed' ORDER BY batch_no DESC LIMIT 1")
    return cur.fetchone()


def supersede_unconfirmed(cur) -> None:
    """Throw away the fingerprints of batches that were made but never confirmed; the new export covers their changes again."""
    cur.execute("DELETE FROM vault_row_state WHERE batch_no IN (SELECT batch_no FROM vault_batch WHERE status IN ('building', 'pending'))")
    cur.execute("UPDATE vault_batch SET status = 'superseded' WHERE status IN ('building', 'pending')")


def new_batch(cur, kind: str, base_batch: int) -> int:
    cur.execute("INSERT INTO vault_batch (kind, base_batch) VALUES (%s, %s) RETURNING batch_no", (kind, base_batch))
    return cur.fetchone()["batch_no"]


def load_state(cur, batch_no: int) -> dict:
    """{table: {row_key: fingerprint bytes}} as of a confirmed batch."""
    state: dict = {}
    cur.execute("SELECT table_name, row_key, fp FROM vault_row_state WHERE batch_no = %s", (batch_no,))
    for r in cur.fetchall():
        state.setdefault(r["table_name"], {})[r["row_key"]] = bytes(r["fp"])
    return state


def save_state(cur, batch_no: int, state: dict) -> None:
    rows = [(batch_no, t, k, fp) for t, keys in state.items() for k, fp in keys.items()]
    for i in range(0, len(rows), CHUNK):
        execute_values(cur, "INSERT INTO vault_row_state (batch_no, table_name, row_key, fp) VALUES %s", rows[i:i + CHUNK])


def mark_pending(cur, batch_no: int, export_id: str, summary: dict) -> None:
    from psycopg2.extras import Json
    cur.execute("UPDATE vault_batch SET status = 'pending', export_id = %s, summary = %s WHERE batch_no = %s",
                (export_id, Json(summary), batch_no))


def mark_failed(cur, batch_no: int) -> None:
    cur.execute("DELETE FROM vault_row_state WHERE batch_no = %s", (batch_no,))
    cur.execute("UPDATE vault_batch SET status = 'failed' WHERE batch_no = %s", (batch_no,))


def confirm(cur, batch_no: int) -> str:
    """'ok', 'unknown' or 'not_pending'. Confirming makes this batch the base of the next delta and drops older fingerprints."""
    cur.execute("SELECT status FROM vault_batch WHERE batch_no = %s FOR UPDATE", (batch_no,))
    row = cur.fetchone()
    if not row:
        return "unknown"
    if row["status"] != "pending":
        return "not_pending"
    cur.execute("UPDATE vault_batch SET status = 'confirmed', confirmed_at = now() WHERE batch_no = %s", (batch_no,))
    cur.execute("DELETE FROM vault_row_state WHERE batch_no < %s", (batch_no,))
    return "ok"


def listing(cur) -> list:
    cur.execute("SELECT batch_no, kind, base_batch, status, export_id, created_at, confirmed_at, summary FROM vault_batch ORDER BY batch_no DESC LIMIT 50")
    return cur.fetchall()
