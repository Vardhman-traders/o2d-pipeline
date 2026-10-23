"""The pool guards: who holds the connections is logged when it runs dry, a nested cursor never asks for a second connection, and
WhatsApp sends cannot take the whole pool. No database needed: a fake pool stands in for psycopg2's."""
import contextlib
import logging
import threading
import time

import pytest
from psycopg2.pool import PoolError

from app import attendance, db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, vars=None):
        self.conn.statements.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    closed = False

    def __init__(self):
        self.statements = []

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.statements.append("COMMIT")

    def rollback(self):
        self.statements.append("ROLLBACK")


class FakePool:
    maxconn = 1

    def __init__(self, size=1):
        self.free = [FakeConn() for _ in range(size)]

    def getconn(self):
        if not self.free:
            raise PoolError("connection pool exhausted")
        return self.free.pop()

    def putconn(self, conn, close=False):
        self.free.append(conn)


@pytest.fixture()
def fake_pool(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(db, "_pool", pool)
    monkeypatch.setattr(db, "WAIT_FOR_CONNECTION_S", 0.2)
    monkeypatch.setattr(db, "WARN_AFTER_S", 0.05)
    return pool


def holds_the_only_connection(started, release):
    with db.cursor():
        started.set()
        release.wait(5)


def test_exhausted_pool_names_who_holds_the_connections(fake_pool, caplog):
    caplog.set_level(logging.WARNING, logger="db")
    started, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=holds_the_only_connection, args=(started, release), name="slow-request")
    holder.start()
    started.wait(5)
    try:
        with pytest.raises(PoolError):
            with db.cursor():
                pass
    finally:
        release.set()
        holder.join()
    text = caplog.text
    assert "connection pool exhausted" in text and "held by" in text
    assert "holds_the_only_connection" in text and "test_db_pool.py" in text and "slow-request" in text
    assert len(fake_pool.free) == 1 and not db._holders      # nothing leaked, nothing left registered


def test_nested_cursor_never_asks_the_pool_for_a_second_connection(fake_pool):
    with db.cursor():
        with db.cursor():            # a pool of one would be exhausted if this checked a second connection out
            with db.cursor():
                pass
    conn = fake_pool.free[0]
    assert conn.statements.count("COMMIT") == 1               # one transaction, committed once, by the outermost block
    assert [s for s in conn.statements if "SAVEPOINT" in s] == [
        "SAVEPOINT vt_nested_1", "SAVEPOINT vt_nested_2", "RELEASE SAVEPOINT vt_nested_2", "RELEASE SAVEPOINT vt_nested_1"]


def test_connection_is_returned_after_an_error(fake_pool):
    with pytest.raises(ValueError):
        with db.cursor():
            raise ValueError("boom")
    assert len(fake_pool.free) == 1 and not db._holders and fake_pool.free[0].statements == ["ROLLBACK"]
    with db.cursor():
        pass


def test_a_failed_nested_block_rolls_back_to_its_savepoint(fake_pool):
    with db.cursor():
        with pytest.raises(ValueError):
            with db.cursor():
                raise ValueError
    statements = fake_pool.free[0].statements
    assert "ROLLBACK TO SAVEPOINT vt_nested_1" in statements and statements[-1] == "COMMIT"


def test_whatsapp_sends_in_flight_are_capped(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(attendance, "_WA_SLOTS", slots)
    monkeypatch.setattr(attendance, "WA_SLOT_WAIT_S", 0.05)
    slots.acquire()                                       # another send is in flight
    started = time.monotonic()
    assert attendance.process_wa_row(1) == {"status": "busy"}   # no database touched, the retry job will send it
    assert time.monotonic() - started < 1


def test_send_buffers_its_log_entries_for_a_caller_that_holds_a_connection(monkeypatch):
    monkeypatch.setattr(attendance.whatsapp, "send_alert", lambda *a, **k: {"code": 200, "body": "ok"})
    monkeypatch.setattr(attendance, "_wa_ok", lambda code, body: True)
    monkeypatch.setattr(attendance, "_log_wa", lambda *a: pytest.fail("must not write the log itself"))
    logs: list = []
    assert attendance._send("att_checkin", {}, "CHECK-IN", "Someone", logs) == "sent"
    assert logs == [("CHECK-IN", "Someone", 200, True, "ok")]
