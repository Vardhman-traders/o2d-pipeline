"""Copy every row from one VT Traders Postgres database into another (for example to move to a new region).

    python scripts/copy_database.py SOURCE_URL TARGET_URL            # dry run: shows what it would do
    python scripts/copy_database.py SOURCE_URL TARGET_URL --commit   # really copy

Both databases must already have the same migrations applied (run `python migrate.py` against each first).
The target's data tables are emptied and refilled from the source, in one transaction: if anything fails,
the target is left exactly as it was. The source is only read. Needs no pg_dump; works from the Render Shell.
"""
import io
import sys

import psycopg2

SKIP = {"schema_migrations"}


def tables_with_fk_edges(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_type = 'BASE TABLE' ORDER BY table_name")
    tables = [r[0] for r in cur.fetchall() if r[0] not in SKIP]
    cur.execute("""SELECT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent, c.conkey
                   FROM pg_constraint c WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace""")
    edges = [(a, b) for a, b, _ in cur.fetchall() if a != b]
    return tables, edges


def load_order(cur):
    """Parents before children. The one known loop (dim_user <-> import_batch) is handled by the caller."""
    tables, edges = tables_with_fk_edges(cur)
    edges = [(c, p) for c, p in edges if {c, p} != {"dim_user", "import_batch"} or c == "import_batch"]
    order, left = [], set(tables)
    while left:
        ready = sorted(t for t in left if all(p not in left or p == t for c, p in edges if c == t))
        if not ready:
            sys.exit(f"Cannot order these tables (unexpected foreign-key loop): {sorted(left)}")
        order += ready
        left -= set(ready)
    return order


def columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
                "ORDER BY ordinal_position", (table,))
    return [r[0] for r in cur.fetchall()]


def main(src_url, dst_url, commit):
    src, dst = psycopg2.connect(src_url, connect_timeout=20), psycopg2.connect(dst_url, connect_timeout=20)
    sc, dc = src.cursor(), dst.cursor()
    sc.execute("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
    dc.execute("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
    if sc.fetchall() != dc.fetchall():
        sys.exit("The two databases have different migrations applied. Run `python migrate.py` against each "
                 "(with its own DATABASE_URL) first, then try again.")
    order = load_order(sc)
    sc.execute("SELECT 1")
    plan = []
    for t in order:
        if columns(sc, t) != columns(dc, t):
            sys.exit(f"Table {t} has different columns in the two databases.")
        sc.execute(f'SELECT count(*) FROM "{t}"')
        plan.append((t, sc.fetchone()[0]))
    print(f"{'table':28} rows to copy")
    for t, n in plan:
        print(f"{t:28} {n}")
    if not commit:
        print("\nDry run only. Nothing was changed. Add --commit to copy.")
        return
    try:
        dc.execute("TRUNCATE " + ", ".join(f'"{t}"' for t, _ in plan) + " RESTART IDENTITY CASCADE")
        for t, _ in plan:
            cols = columns(sc, t)
            # dim_user and import_batch point at each other: load dim_user with that column empty, fill it in after
            select = ", ".join("NULL" if (t == "dim_user" and c == "import_batch_id") else f'"{c}"' for c in cols)
            collist = ", ".join(f'"{c}"' for c in cols)
            buf = io.StringIO()
            sc.copy_expert(f'COPY (SELECT {select} FROM "{t}") TO STDOUT', buf)
            data = buf.getvalue()
            dc.copy_expert(f'COPY "{t}" ({collist}) FROM STDIN', io.StringIO(data))
        sc.execute("SELECT user_key, import_batch_id FROM dim_user WHERE import_batch_id IS NOT NULL")
        for key, batch in sc.fetchall():
            dc.execute("UPDATE dim_user SET import_batch_id = %s WHERE user_key = %s", (batch, key))
        # keep numbering going from where the source left off
        dc.execute("""SELECT t.relname, a.attname, pg_get_serial_sequence(quote_ident(t.relname), a.attname)
                      FROM pg_class t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0
                      WHERE t.relkind = 'r' AND t.relnamespace = 'public'::regnamespace AND NOT a.attisdropped""")
        for table, col, seq in dc.fetchall():
            if seq and table not in SKIP:
                dc.execute(f'SELECT setval(%s, GREATEST(COALESCE((SELECT max("{col}") FROM "{table}"), 0), 1), '
                           f'(SELECT count(*) > 0 FROM "{table}"))', (seq,))
        bad = []
        for t, n in plan:
            dc.execute(f'SELECT count(*) FROM "{t}"')
            got = dc.fetchone()[0]
            if got != n:
                bad.append((t, n, got))
        if bad:
            raise RuntimeError(f"Row counts do not match: {bad}")
        dst.commit()
        print("\nDone. Every table copied and the row counts match.")
    except Exception:
        dst.rollback()
        raise
    finally:
        src.rollback()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--commit"]
    if len(args) != 2:
        sys.exit(__doc__)
    main(args[0], args[1], "--commit" in sys.argv)
