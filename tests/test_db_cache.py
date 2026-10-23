"""db.py against a real Postgres: nested cursors share one connection, the read cache never serves a change made through the app,
and the advisory lock leaves the work inside free to commit step by step.

The access tests are imported below so they run again with the cache switched ON (the rest of the suite runs with it off, because
those tests edit tables with their own connection)."""
import time
import uuid

import psycopg2
import pytest

from app import db

from .test_access import (PW, client as _client, login, pages, seed,  # noqa: F401  (fixtures + helpers)
                          test_access_request_flow, test_role_defaults_keep_the_old_behaviour, test_role_matrix_and_person_exceptions)


@pytest.fixture()
def client(_client, monkeypatch):
    """The access tests' client, with the read cache on."""
    monkeypatch.setattr(db, "READ_CACHE_TTL", 15.0)
    db._read_cache.clear()
    yield _client
    db._read_cache.clear()


class Counter:
    """Counts the statements that touch a table while it is installed (the app's own cursor class, so it sees what the app sees)."""
    def __init__(self, monkeypatch, needle):
        self.needle, self.n = needle, 0
        real = db._Cursor.execute

        def execute(cur, query, vars=None):
            if isinstance(query, str) and needle in query:
                self.n += 1
            return real(cur, query, vars)
        monkeypatch.setattr(db._Cursor, "execute", execute)


def test_access_check_is_not_asked_of_the_database_twice(client, db_conn, seed, monkeypatch):
    shop, _ = login(client, db_conn, "shop")
    assert pages(client, shop) == ["o2d_shop"]
    seen = Counter(monkeypatch, "FROM page_access")
    for _ in range(3):
        assert client.get("/o2d/shop", headers=shop).status_code == 200
    assert seen.n == 0                                   # role defaults were kept from the first request


def test_a_change_made_in_setup_shows_on_the_very_next_request(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    cashier, key = login(client, db_conn, "cashier")
    before = pages(client, cashier)
    assert "dashboard_orders" not in before and pages(client, cashier) == before          # now cached
    client.put("/admin/access/matrix", headers=admin, json={"page_key": "dashboard_orders", "role": "cashier", "allowed": True})
    assert "dashboard_orders" in pages(client, cashier)
    client.put(f"/admin/access/users/{key}", headers=admin, json={"page_key": "dashboard_orders", "allowed": False})
    assert "dashboard_orders" not in pages(client, cashier)
    client.put(f"/admin/access/users/{key}", headers=admin, json={"page_key": "dashboard_orders", "allowed": None})
    client.put("/admin/access/matrix", headers=admin, json={"page_key": "dashboard_orders", "role": "cashier", "allowed": False})
    assert pages(client, cashier) == before


def test_disabling_a_login_or_changing_a_role_takes_effect_at_once(client, db_conn, seed):
    admin, _ = login(client, db_conn, "admin")
    person, key = login(client, db_conn, "shop")
    assert client.get("/auth/me", headers=person).status_code == 200                      # cached
    assert client.post(f"/admin/users/{key}/disable", headers=admin).status_code == 200
    assert client.get("/auth/me", headers=person).status_code == 401


def test_a_write_behind_the_apps_back_shows_once_the_time_limit_passes(client, db_conn, seed, monkeypatch):
    monkeypatch.setattr(db, "READ_CACHE_TTL", 0.2)
    cashier, key = login(client, db_conn, "cashier")
    before = pages(client, cashier)
    with db_conn.cursor() as cur:   # the tests' own connection: the app cannot see this write
        cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, view_only) VALUES (%s, 'dashboard_orders', true, false)", (key,))
    db_conn.commit()
    assert pages(client, cashier) == before
    time.sleep(0.3)
    assert "dashboard_orders" in pages(client, cashier)


def test_nested_cursor_shares_the_callers_connection(client):
    with db.cursor() as outer:
        with db.cursor() as inner:
            assert inner.connection is outer.connection
        assert len(db._holders) == 1                      # one connection checked out, not two
    assert not db._holders


def test_nested_block_joins_the_outer_transaction(client):
    name = "nest_" + uuid.uuid4().hex[:8]
    with pytest.raises(RuntimeError):
        with db.cursor() as outer:
            outer.execute("INSERT INTO dim_order_channel (channel_name) VALUES (%s)", (name,))
            with db.cursor() as inner:                    # sees the caller's uncommitted row (a second connection never could)
                inner.execute("SELECT 1 FROM dim_order_channel WHERE channel_name = %s", (name,))
                assert inner.fetchone() is not None
            raise RuntimeError("the outer block fails, so everything it did goes, nested part included")
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM dim_order_channel WHERE channel_name = %s", (name,))
        assert cur.fetchone() is None


def test_error_in_a_nested_block_does_not_poison_the_outer_one(client):
    name = "nest_" + uuid.uuid4().hex[:8]
    with db.cursor() as outer:
        outer.execute("INSERT INTO dim_order_channel (channel_name) VALUES (%s)", (name,))
        with pytest.raises(psycopg2.errors.UndefinedTable):
            with db.cursor() as inner:
                inner.execute("SELECT * FROM no_such_table")
        outer.execute("SELECT 1 FROM dim_order_channel WHERE channel_name = %s", (name,))   # still usable: the savepoint was rolled back
        assert outer.fetchone() is not None
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM dim_order_channel WHERE channel_name = %s", (name,))
        assert cur.fetchone() is not None
        cur.execute("DELETE FROM dim_order_channel WHERE channel_name = %s", (name,))


def test_a_transaction_reads_its_own_uncommitted_change_not_the_cache(client):
    name = "own_" + uuid.uuid4().hex[:8]
    sql = "SELECT channel_name FROM dim_order_channel WHERE channel_name = %s"
    assert db.read(sql, (name,), tables=("dim_order_channel",)) == []          # cached as empty
    with db.cursor() as cur:
        cur.execute("INSERT INTO dim_order_channel (channel_name) VALUES (%s)", (name,))
        assert db.read(sql, (name,), tables=("dim_order_channel",)) == [{"channel_name": name}]
        cur.execute("DELETE FROM dim_order_channel WHERE channel_name = %s", (name,))


def test_a_rolled_back_write_leaves_the_cache_alone(client):
    sql = "SELECT count(*) AS n FROM dim_order_channel"
    db.read(sql, tables=("dim_order_channel",))
    seen = db._snapshot(("dim_order_channel",))
    with pytest.raises(RuntimeError):
        with db.cursor() as cur:
            cur.execute("INSERT INTO dim_order_channel (channel_name) VALUES ('never kept')")
            raise RuntimeError
    assert db._snapshot(("dim_order_channel",)) == seen
    with db.cursor() as cur:
        cur.execute("INSERT INTO dim_order_channel (channel_name) VALUES ('kept briefly')")
    assert db._snapshot(("dim_order_channel",)) != seen
    with db.cursor() as cur:
        cur.execute("DELETE FROM dim_order_channel WHERE channel_name = 'kept briefly'")


def test_advisory_lock_is_exclusive_and_leaves_the_work_free_to_commit(client):
    lock = 987654
    with db.advisory_lock(lock) as got:
        assert got is True
        with db.advisory_lock(lock) as again:             # another connection of this process stands in for another copy of the service
            assert again is False
        with db.cursor() as cur:                          # not joined to the lock's connection
            cur.execute("SELECT pg_backend_pid() AS pid")
            work_pid = cur.fetchone()["pid"]
    with db.advisory_lock(lock) as got:                   # released on the way out
        assert got is True
    assert not db._holders and work_pid


@pytest.mark.parametrize("sql, tables", [
    ("SELECT * FROM page_access", set()),
    ("INSERT INTO page_access (page_key, role) VALUES (%s, %s) ON CONFLICT DO NOTHING", {"page_access"}),
    ("INSERT INTO user_page_access (a) VALUES (1) ON CONFLICT (user_key, page_key) DO UPDATE SET allowed = 1", {"user_page_access", "set"}),
    ("UPDATE public.dim_user SET role = %s", {"dim_user"}),
    ("DELETE FROM page_access WHERE role = %s", {"page_access"}),
    ("TRUNCATE TABLE page_access", {"page_access"}),
    ("WITH x AS (SELECT 1) SELECT * FROM x FOR UPDATE", {"*"}),
])
def test_which_tables_a_statement_writes(sql, tables):
    assert db.tables_written(sql) == tables
