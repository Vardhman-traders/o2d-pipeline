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
