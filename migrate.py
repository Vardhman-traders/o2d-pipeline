"""Minimal SQL migration runner.

Applies migrations/NNN_*.sql in order, each in its own transaction, and records
them in the schema_migrations table. Reads the connection string from DATABASE_URL (environment or .env file).
"""
import hashlib
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

load_dotenv(Path(__file__).parent / ".env")  # real environment variables take precedence


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename   text PRIMARY KEY,
                       checksum   text NOT NULL,
                       applied_at timestamptz NOT NULL DEFAULT now()
                   )"""
            )
        with conn.cursor() as cur:
            cur.execute("SELECT filename, checksum FROM schema_migrations")
            applied = dict(cur.fetchall())
        conn.commit()

        pending = 0
        for f in files:
            sql = f.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if f.name in applied:
                if applied[f.name] != checksum:
                    print(f"ERROR: {f.name} was modified after being applied.", file=sys.stderr)
                    return 1
                continue
            print(f"Applying {f.name} ...")
            try:
                with conn, conn.cursor() as cur:  # one transaction per migration
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                        (f.name, checksum),
                    )
            except Exception as exc:
                print(f"FAILED {f.name}: {exc}", file=sys.stderr)
                return 1
            pending += 1
        print(f"Done. {pending} migration(s) applied, {len(files) - pending} already up to date.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
