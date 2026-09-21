"""Small Postgres connection pool. Render's free tier caps connections, so keep the pool tiny."""
import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

load_dotenv(Path(__file__).parent.parent / ".env")

_pool = None


def init_pool():
    global _pool
    _pool = ThreadedConnectionPool(
        1, int(os.environ.get("DB_POOL_MAX", "5")), os.environ["DATABASE_URL"], connect_timeout=15
    )


def close_pool():
    if _pool:
        _pool.closeall()


@contextmanager
def cursor():
    """One transaction per request: commit on success, roll back on any error."""
    conn = _pool.getconn()
    try:
        if conn.closed:  # server dropped an idle connection
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except BaseException:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        _pool.putconn(conn, close=bool(conn.closed))
