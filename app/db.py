"""Small Postgres connection pool. Render's free tier caps connections, so keep the pool tiny.

Three things on top of the pool, all about not running out of it:
  - cursor() is re-entrant per thread: a function that opens a cursor while its caller already holds one (an access check inside an
    order save, a config read inside a WhatsApp send) shares the caller's connection under a SAVEPOINT instead of asking the pool for a
    second one. Asking for a second one while holding the first is what froze the whole site when every connection was held that way.
  - read() keeps the rows of small, rarely changing lookups (page access, dropdown lists, the signed-in user) for a few seconds. Every
    write that goes through cursor() bumps a per-table version once it has committed, so a change made here shows up on the very next
    request; the time limit only covers writes made behind the app's back (a manual SQL edit, another instance).
  - the log says who holds the connections when the pool runs dry.
"""
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from psycopg2.pool import PoolError, ThreadedConnectionPool

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger("db")
_pool = None


def init_pool():
    global _pool
    _pool = ThreadedConnectionPool(
        1, int(os.environ.get("DB_POOL_MAX", "5")), os.environ["DATABASE_URL"], connect_timeout=15
    )
    log.info("database pool ready: up to %s connections", _pool.maxconn)


def close_pool():
    if _pool:
        _pool.closeall()


WAIT_FOR_CONNECTION_S = 10
WARN_AFTER_S = 1

# Who has each connection right now, so "connection pool exhausted" in the log says which code is holding them and for how long.
_holders: dict = {}          # id(conn) -> (since, thread name, "file:line function")
_holders_lock = threading.Lock()
_mine = threading.local()    # .conn: the connection this thread's outermost cursor() holds; .dirty: tables it has written; .depth: nesting


def _caller() -> str:
    f = sys._getframe(1)
    while f and (f.f_code.co_filename == __file__ or f.f_code.co_filename.endswith("contextlib.py")):
        f = f.f_back
    return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno} {f.f_code.co_name}" if f else "?"


def _describe_holders() -> str:
    now = time.monotonic()
    with _holders_lock:
        rows = sorted(_holders.values())
    return "; ".join(f"{who} [{thread}] {now - since:.1f}s" for since, thread, who in rows) or "none recorded"


def _get_connection():
    """A free connection from the pool. A busy moment (a screen firing several requests at once) waits a little for one to come back
    instead of failing the request with "connection pool exhausted"."""
    start = time.monotonic()
    deadline = start + WAIT_FOR_CONNECTION_S
    warned = False
    while True:
        try:
            return _pool.getconn()
        except PoolError:
            now = time.monotonic()
            if now >= deadline:
                log.error("connection pool exhausted after %ss (max %s); held by: %s", WAIT_FOR_CONNECTION_S, _pool.maxconn,
                          _describe_holders())
                raise
            if not warned and now - start >= WARN_AFTER_S:
                warned = True
                log.warning("waiting for a database connection (max %s); held by: %s", _pool.maxconn, _describe_holders())
            time.sleep(0.03)


def _checkout():
    who = _caller()
    conn = _get_connection()
    with _holders_lock:
        _holders[id(conn)] = (time.monotonic(), threading.current_thread().name, who)
    return conn


def _release(conn, close=None):
    with _holders_lock:
        _holders.pop(id(conn), None)
    _pool.putconn(conn, close=bool(conn.closed) if close is None else close)


# ------------------------------------------------------------------ which tables a statement writes
_WRITE_WORD = re.compile(r"\b(?:insert|update|delete|truncate|merge|copy)\b", re.I)
_WRITE_TARGET = re.compile(r"\b(?:insert\s+into|update|delete\s+from|truncate(?:\s+table)?|merge\s+into)\s+(?:only\s+)?([A-Za-z_][\w.\"]*)", re.I)


def tables_written(sql) -> set:
    """The tables a statement may change: {} for a read, {"*"} when it looks like a write but the target cannot be told. Over-reporting
    (a SELECT ... FOR UPDATE names "of" or "skip") only costs a needless cache refresh, so this errs that way."""
    if not isinstance(sql, str) or _WRITE_WORD.search(sql) is None:
        return set() if isinstance(sql, str) else {"*"}
    names = {m.strip('"').split(".")[-1].strip('"').lower() for m in _WRITE_TARGET.findall(sql)}
    return names or {"*"}


class _Cursor(RealDictCursor):
    """RealDictCursor that remembers which tables this transaction has written, for the read cache."""

    def execute(self, query, vars=None):
        dirty = getattr(_mine, "dirty", None)
        if dirty is not None:
            dirty |= tables_written(query)
        return super().execute(query, vars)

    def executemany(self, query, vars_list):
        dirty = getattr(_mine, "dirty", None)
        if dirty is not None:
            dirty |= tables_written(query)
        return super().executemany(query, vars_list)


# ------------------------------------------------------------------ the cursor
@contextmanager
def cursor():
    """One transaction per request: commit on success, roll back on any error. Opened again while this thread already holds a cursor,
    it joins that one's transaction (inside a SAVEPOINT, so an error in the inner block can still be caught by the caller)."""
    outer = getattr(_mine, "conn", None)
    if outer is not None:
        with _nested(outer) as cur:
            yield cur
        return
    conn = _checkout()
    try:
        if conn.closed:  # server dropped an idle connection
            _release(conn, close=True)
            conn = None
            conn = _checkout()
        _mine.conn, _mine.dirty, _mine.depth = conn, set(), 0
        with conn.cursor(cursor_factory=_Cursor) as cur:
            yield cur
        conn.commit()
        _bump(_mine.dirty)   # only now are the changes visible to everyone, so only now may a cached copy be thrown away
    except BaseException:
        if conn is not None and not conn.closed:
            conn.rollback()
        raise
    finally:
        _mine.conn, _mine.dirty = None, None
        if conn is not None:
            _release(conn)


@contextmanager
def _nested(conn):
    _mine.depth += 1
    name = f"vt_nested_{_mine.depth}"
    try:
        with conn.cursor(cursor_factory=_Cursor) as cur:
            cur.execute(f"SAVEPOINT {name}")
            try:
                yield cur
            except BaseException:
                if not conn.closed:
                    try:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    except psycopg2.Error:
                        pass   # the outer transaction is already lost; the original error is the one to raise
                raise
            cur.execute(f"RELEASE SAVEPOINT {name}")
    finally:
        _mine.depth -= 1


@contextmanager
def advisory_lock(lock_id: int):
    """Yields True when this process now holds the Postgres advisory lock `lock_id`, False when another copy of the service has it.
    The lock sits on a connection of its own, not on this thread's cursor, so the work inside the block still uses db.cursor() and
    commits step by step (a transaction-level lock would have made every step part of one transaction)."""
    conn = _checkout()
    got, close = False, False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
            got = cur.fetchone()[0]
        conn.commit()
        yield got
    finally:
        if got:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
                conn.commit()
            except psycopg2.Error:
                close = True    # closing the connection releases the lock
        _release(conn, close=close or None)


# ------------------------------------------------------------------ cached reads of small lookup tables
READ_CACHE_TTL = float(os.environ.get("VT_READ_CACHE_TTL", "15"))   # seconds; 0 turns the cache off (the tests do, they edit tables directly)
READ_CACHE_MAX = 2000
_versions: dict = {}         # table -> number of committed writes seen (and "*" for writes whose table is unknown)
_versions_lock = threading.Lock()
_read_cache: dict = {}       # (sql, params) -> (stored at, versions when it was read, rows)


def _bump(tables):
    if not tables:
        return
    with _versions_lock:
        for t in tables:
            _versions[t] = _versions.get(t, 0) + 1


def _snapshot(tables) -> tuple:
    with _versions_lock:
        return tuple(_versions.get(t, 0) for t in (*tables, "*"))


def read(sql: str, params=None, *, tables) -> list[dict]:
    """The rows of a SELECT over `tables`, from a copy kept while none of those tables has been written (and for at most
    READ_CACHE_TTL). A caller that has written one of them in its own open transaction always reads the database, so it sees its change."""
    def load():
        with cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    dirty = getattr(_mine, "dirty", None) or ()
    if READ_CACHE_TTL <= 0 or "*" in dirty or any(t in dirty for t in tables):
        return load()
    key = (sql, repr(params))
    seen = _snapshot(tables)          # taken before reading: a write that commits during the read makes this copy stale on arrival
    hit = _read_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < READ_CACHE_TTL and hit[1] == seen:
        return [dict(r) for r in hit[2]]
    rows = load()
    if len(_read_cache) >= READ_CACHE_MAX:
        _read_cache.clear()
    _read_cache[key] = (now, seen, rows)
    return [dict(r) for r in rows]
