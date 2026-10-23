"""Shared fixtures. Tests never touch your dev or production data: each session creates a throwaway
database on the TEST_DATABASE_URL server, applies every migration to it, and drops it afterwards.

Default server: the docker-compose db (postgresql://vt:vt@localhost:5433/postgres). In CI the workflow
sets TEST_DATABASE_URL to its Postgres 16 service container.
"""
import importlib
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VT_SCHEDULER", "off")   # no 7:30 PM report thread inside the tests
os.environ.setdefault("VT_ALERTS_INLINE", "1")  # WhatsApp dispatch alerts go out before the save returns, so tests can check them
os.environ.setdefault("VT_READ_CACHE_TTL", "0")  # tests edit page_access / dim_user ... with their own connection, which a cache cannot see
ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://vt:vt@localhost:5433/postgres")


def bill_no() -> str:
    """A fresh bill number for a test order. Every test takes it from this one range: the orders of the whole session share today's date,
    and a Challan more than ten times that day's highest is refused as an Invoice typed in the wrong place (doc_numbers.suspect_reason).
    Tests that each picked their own size (6 to 9 digits) failed or passed depending on which ran first."""
    return str(500_000_000 + uuid.uuid4().int % 100_000_000)


def _with_db(url: str, dbname: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path="/" + dbname))


def _create_db() -> tuple[str, str]:
    name = "vt_test_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(ADMIN_URL, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, _with_db(ADMIN_URL, name)


def _drop_db(name: str) -> None:
    conn = psycopg2.connect(ADMIN_URL, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


def run_migrations(url: str, migrations_dir: Path | None = None) -> int:
    """Run migrate.py's main() against `url`, optionally with a different migrations folder."""
    os.environ["DATABASE_URL"] = url
    import migrate
    importlib.reload(migrate)
    if migrations_dir is not None:
        migrate.MIGRATIONS_DIR = migrations_dir
    return migrate.main()


@pytest.fixture()
def fresh_db_url():
    """An empty throwaway database (no migrations applied)."""
    name, url = _create_db()
    yield url
    _drop_db(name)


@pytest.fixture(scope="session")
def migrated_db_url():
    """A throwaway database with ALL migrations applied, shared by the whole test session."""
    name, url = _create_db()
    assert run_migrations(url) == 0, "migrations failed on a fresh database"
    yield url
    _drop_db(name)


@pytest.fixture()
def db_conn(migrated_db_url):
    conn = psycopg2.connect(migrated_db_url)
    yield conn
    conn.rollback()
    conn.close()
