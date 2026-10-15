"""Clear every ENTRY in the system and keep everything that is set up. For going live after testing.

    python scripts/clear_entries.py                          # shows what would be deleted (changes nothing)
    python scripts/clear_entries.py --yes                    # asks you to type a phrase, then deletes
    python scripts/clear_entries.py --yes --restart-numbers  # also starts SL no., TXN-, T-, EXP- numbers again from 1

Deleted (the entries): orders and their timeline / photos / reminders / Payments pushes, payment entries, delegation tasks / weekly
plans / extra work, checklist occurrences (what was marked), regular-expense entries, uploaded challans, purchase entries, VT Decor
clients and everything under them, attendance records / advances / WhatsApp log, access requests, bulk-upload history, stored files
(the database rows - the files themselves stay in the R2 bucket until you empty it there).

Kept exactly as they are: members and passwords, the access matrix and per-person access, every dropdown list (channels, statuses,
people, companies, modes, types, parties, vendors, products, stages, categories, expense accounts and opening balances, work places),
the employee list with salaries / PINs / work-place tags, checklist definitions, module settings, WhatsApp settings, field
permissions, links, the calendar, the audit log and the report-run guards.

It runs in ONE transaction: if anything fails, nothing is changed. Take a database backup first (Render > your Postgres > Backups).
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# the entries, children and parents together (one TRUNCATE can only skip the FK check for tables named in the same statement)
ENTRY_TABLES = [
    # O2D
    "order_attachments", "order_event", "order_reminder", "payment_push", "doc_gap_void", "fact_orders",
    # Payments, Regular expenses, Upload challan
    "fact_payment_txn", "fact_expense_entry", "fact_challan_upload",
    # Delegation + Checklist (the checklists themselves are kept; what was marked is not)
    "fact_task", "fact_weekly_plan", "fact_extra_work", "fact_checklist_occurrence",
    # Purchase
    "fact_purchase_entry",
    # VT Decor
    "fact_decor_payment", "fact_decor_expense", "fact_decor_item", "fact_decor_photo", "fact_decor_quotation", "fact_decor_revision",
    "decor_fixed_expense", "dim_decor_client",
    # Attendance
    "fact_attendance", "fact_att_advance", "att_wa_log",
    # shared
    "file_attachment", "access_request", "import_batch",
]
# these keep their rows but point at an upload batch that is going away
UNLINK = [("dim_user", "import_batch_id"), ("dim_att_employee", "import_batch_id"), ("dim_checklist", "import_batch_id")]


def delete_order(cur, tables):
    """The tables sorted so that any table that points at another one (a foreign key) is cleared before it."""
    cur.execute("SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c WHERE c.contype = 'f'")
    edges = [(a.strip('"'), b.strip('"')) for a, b in cur.fetchall()]
    todo, out = set(tables), []
    while todo:
        ready = [t for t in sorted(todo) if not any(p == t and ch in todo and ch != t for ch, p in edges)]
        if not ready:
            raise SystemExit("These tables point at each other, so there is no safe order: " + ", ".join(sorted(todo)))
        out += ready
        todo -= set(ready)
    return out


def main(argv):
    really, restart = "--yes" in argv, "--restart-numbers" in argv
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            present = {r[0] for r in cur.fetchall()}
            tables = [t for t in ENTRY_TABLES if t in present]
            skipped = [t for t in ENTRY_TABLES if t not in present]
            print("Rows that will be deleted:")
            total = 0
            for t in tables:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                n = cur.fetchone()[0]
                total += n
                print(f"  {t:32} {n:>8}")
            if skipped:
                print("  (not in this database, skipped:", ", ".join(skipped) + ")")
            print(f"  {'TOTAL':32} {total:>8}")
            for t in ("dim_user", "dim_att_employee", "dim_company", "dim_payment_status", "dim_party", "dim_checklist", "dim_expense_account"):
                if t in present:
                    cur.execute(f'SELECT count(*) FROM "{t}"')
                    print(f"  kept: {t:26} {cur.fetchone()[0]:>8}")
            if not really:
                print("\nDry run - nothing was changed. Run again with --yes to delete.")
                return 0
            print("\nThis permanently deletes the rows above from:", conn.dsn.split("host=")[-1].split()[0] if "host=" in conn.dsn else "the database")
            if input('Type  DELETE ENTRIES  to continue: ').strip() != "DELETE ENTRIES":
                print("Not confirmed. Nothing was changed.")
                return 1
            for table, col in UNLINK:
                if table in present:
                    cur.execute(f'UPDATE "{table}" SET "{col}" = NULL WHERE "{col}" IS NOT NULL')
            for t in delete_order(cur, tables):     # children before parents, so no foreign key is ever violated
                cur.execute(f'DELETE FROM "{t}"')
            if restart:
                cur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                            "AND is_identity = 'YES' AND table_name = ANY(%s)", (tables,))
                for t, col in cur.fetchall():
                    cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{t}"', col))
                    seq = cur.fetchone()[0]
                    if seq:
                        cur.execute(f"ALTER SEQUENCE {seq} RESTART")
            print(f"\nDone. {total} row(s) deleted" + (", numbering restarted." if restart else ". Numbering continues from where it was."))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
