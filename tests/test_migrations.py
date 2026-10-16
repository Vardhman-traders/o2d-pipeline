"""Migration safety: every migration applies to an empty DB, re-running is a no-op, edits to applied
migrations are refused, and a failing migration leaves nothing behind."""
import re
import shutil
from pathlib import Path

import psycopg2
import pytest

from tests.conftest import ROOT, run_migrations

MIGRATIONS = ROOT / "migrations"


def test_migration_files_are_numbered_without_gaps_or_duplicates():
    nums = [int(m.group(1)) for f in MIGRATIONS.glob("*.sql") if (m := re.match(r"(\d{3})_", f.name))]
    assert nums, "no migrations found"
    assert sorted(nums) == list(range(1, len(nums) + 1)), f"gap or duplicate in migration numbers: {sorted(nums)}"


def test_all_migrations_recorded(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        applied = cur.fetchone()[0]
    assert applied == len(list(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")))


def test_core_tables_exist(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        tables = {r[0] for r in cur.fetchall()}
    assert {"fact_orders", "dim_user", "dim_date", "dim_person", "schema_migrations"} <= tables


def test_rerun_is_a_noop(migrated_db_url, capsys):
    assert run_migrations(migrated_db_url) == 0
    assert "0 migration(s) applied" in capsys.readouterr().out


@pytest.mark.slow
def test_editing_an_applied_migration_is_refused(fresh_db_url, tmp_path):
    work = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, work)
    assert run_migrations(fresh_db_url, work) == 0
    first = sorted(work.glob("[0-9][0-9][0-9]_*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    assert run_migrations(fresh_db_url, work) == 1


@pytest.mark.slow
def test_failed_migration_rolls_back_and_is_not_recorded(fresh_db_url, tmp_path):
    work = tmp_path / "migrations"
    work.mkdir()
    (work / "001_ok.sql").write_text("CREATE TABLE ok_table (id int);", encoding="utf-8")
    (work / "002_bad.sql").write_text(
        "CREATE TABLE half_done (id int); SELECT 1/0;", encoding="utf-8")
    assert run_migrations(fresh_db_url, work) == 1
    conn = psycopg2.connect(fresh_db_url)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.half_done'), to_regclass('public.ok_table')")
        half, ok = cur.fetchone()
        cur.execute("SELECT filename FROM schema_migrations")
        recorded = [r[0] for r in cur.fetchall()]
    conn.close()
    assert half is None, "a failed migration left partial changes behind"
    assert ok is not None
    assert recorded == ["001_ok.sql"]


def test_migrations_dir_is_a_path():
    assert isinstance(MIGRATIONS, Path) and MIGRATIONS.is_dir()


@pytest.mark.slow
def test_037_merges_duplicate_parties_and_repoints_entries(fresh_db_url, tmp_path):
    """Parties that differ only by company / type become one party; every entry follows the one that stays."""
    old = tmp_path / "before"
    old.mkdir()
    for f in MIGRATIONS.glob("*.sql"):
        if int(f.name[:3]) < 37:
            shutil.copy(f, old / f.name)
    assert run_migrations(fresh_db_url, old) == 0
    conn = psycopg2.connect(fresh_db_url)
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dim_company (company_name) VALUES ('C1'), ('C2') ON CONFLICT DO NOTHING")
        cur.execute("SELECT company_key FROM dim_company ORDER BY company_key LIMIT 2")
        c1, c2 = [r[0] for r in cur.fetchall()]
        keys = []
        for ck in (c1, c2):
            cur.execute("INSERT INTO dim_party (party_name, party_kind, company_key, review_status) VALUES ('Acme Ltd', 'payment', %s, 'pending') RETURNING party_key", (ck,))
            keys.append(cur.fetchone()[0])
        cur.execute("INSERT INTO dim_party (party_name, party_kind, review_status) VALUES ('  acme ltd', 'payment', 'approved') RETURNING party_key")
        keys.append(cur.fetchone()[0])
    conn.close()
    assert run_migrations(fresh_db_url) == 0
    conn = psycopg2.connect(fresh_db_url)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT party_key, company_key FROM dim_party WHERE party_kind = 'payment' AND lower(btrim(party_name)) = 'acme ltd'")
        rows = cur.fetchall()
    conn.close()
    assert rows == [(keys[2], None)]               # one left: the approved one, with no company
