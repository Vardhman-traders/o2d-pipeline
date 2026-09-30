"""One command to try the app on your own machine before pushing. Never touches Render.

    python scripts/run_local.py                       # start DB, migrate, make a local admin, run the app
    python scripts/run_local.py --copy-from "RENDER_EXTERNAL_URL"   # also copy real data down (read-only on Render)
    python scripts/run_local.py --test                # run the test suite and lint instead of the app
    python scripts/run_local.py --reset               # wipe the LOCAL database and start clean

Needs a local Postgres: either Docker Desktop (the default), or PostgreSQL installed on this PC, in which case run
    python scripts/run_local.py --pg-password YOUR_POSTGRES_PASSWORD
The database URL is forced to localhost, so a DATABASE_URL in .env (which may point at Render) is ignored.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "postgresql://vt:vt@localhost:5433/vt_dev"
ADMIN_USER, ADMIN_PASSWORD = "admin", "Local-Admin-123"      # local machine only; never used on Render
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def say(msg):
    print(f"\n==> {msg}", flush=True)


def run(cmd, env, check=True):
    return subprocess.run(cmd, cwd=ROOT, env=env, check=check)


def ensure_local(url):
    host = urlparse(url).hostname
    if host not in LOCAL_HOSTS:
        sys.exit(f"Refusing to run: the database host is '{host}', not this machine. This script only works with "
                 "a local database.")


def start_docker_db():
    if not shutil.which("docker"):
        sys.exit("Docker was not found. Either install Docker Desktop, or install PostgreSQL on this PC and run:\n"
                 "    python scripts\\run_local.py --pg-password YOUR_POSTGRES_PASSWORD")
    say("Starting the local Postgres (Docker)")
    if subprocess.run(["docker", "compose", "up", "-d", "db"], cwd=ROOT).returncode != 0:
        sys.exit("Could not start Docker. Is Docker Desktop running?")


def wait_for_db(url, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            psycopg2.connect(url, connect_timeout=3).close()
            return
        except psycopg2.OperationalError:
            time.sleep(1.5)
    sys.exit("The local database did not come up in time. Check Docker Desktop.")


def ensure_database(url):
    """Create the database if it doesn't exist yet (only matters with --db-url)."""
    parts = urlparse(url)
    name = parts.path.lstrip("/")
    admin_url = url.replace(f"/{name}", "/postgres", 1)
    try:
        psycopg2.connect(url, connect_timeout=3).close()
        return
    except psycopg2.OperationalError:
        pass
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{name}"')
    conn.close()


def reset_database(url):
    say("Wiping the LOCAL database")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    conn.close()


def ensure_admin(url):
    from app import auth  # imported late so DATABASE_URL is already the local one
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM dim_user WHERE role = 'admin' LIMIT 1")
    if cur.fetchone():
        conn.close()
        return False
    cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) "
                "VALUES (%s, %s, 'admin', 'Local Admin', false) ON CONFLICT DO NOTHING",
                (ADMIN_USER, auth.hash_password(ADMIN_PASSWORD)))
    conn.commit()
    conn.close()
    return True


def main():
    ap = argparse.ArgumentParser(description="Run the app locally, safely.")
    ap.add_argument("--db-url", default=DEFAULT_URL, help="local database URL (default: the Docker one)")
    ap.add_argument("--no-docker", action="store_true", help="use a Postgres you already run, skip Docker")
    ap.add_argument("--pg-password", help="password of the PostgreSQL installed on this PC (implies --no-docker)")
    ap.add_argument("--pg-user", default="postgres", help="its user name (default: postgres)")
    ap.add_argument("--pg-port", type=int, default=5432, help="its port (default: 5432)")
    ap.add_argument("--copy-from", metavar="URL", help="copy all data from this database (read only) first")
    ap.add_argument("--reset", action="store_true", help="wipe the local database before starting")
    ap.add_argument("--test", action="store_true", help="run tests and lint instead of starting the app")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.pg_password is not None:
        a.no_docker = True
        a.db_url = f"postgresql://{quote(a.pg_user)}:{quote(a.pg_password, safe='')}@localhost:{a.pg_port}/vt_dev"

    ensure_local(a.db_url)
    env = {**os.environ, "DATABASE_URL": a.db_url, "JWT_SECRET": "local-dev-secret-" + "x" * 40,
           "TEST_DATABASE_URL": a.db_url.rsplit("/", 1)[0] + "/postgres"}   # overrides anything in .env
    sys.path.insert(0, str(ROOT))
    os.environ.update(env)

    if not a.no_docker:
        start_docker_db()
    say("Waiting for the database")
    if a.no_docker:
        ensure_database(a.db_url)
    wait_for_db(a.db_url)
    if a.reset:
        reset_database(a.db_url)

    say("Applying migrations")
    run([sys.executable, "migrate.py"], env)

    if a.copy_from:
        say("Copying data from the source (it is only read, never changed)")
        run([sys.executable, "scripts/copy_database.py", a.copy_from, a.db_url, "--commit"], env)

    if a.test:
        say("Running tests and lint")
        ok = run([sys.executable, "-m", "pytest", "-q"], env, check=False).returncode == 0
        ok = run([sys.executable, "-m", "ruff", "check", "tests", "scripts"], env, check=False).returncode == 0 and ok
        ok = run([sys.executable, "scripts/lint_ratchet.py", "check"], env, check=False).returncode == 0 and ok
        print("\nAll checks passed. Safe to push." if ok else "\nSomething failed above. Fix it before pushing.")
        sys.exit(0 if ok else 1)

    if ensure_admin(a.db_url):
        print(f"\nCreated a local admin:  username '{ADMIN_USER}'  password '{ADMIN_PASSWORD}'")
    else:
        print("\nUsing the admins already in the local database.")
    url = f"http://localhost:{a.port}"
    say(f"Starting the app at {url}   (Ctrl+C to stop; code changes reload automatically)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        run([sys.executable, "-m", "uvicorn", "app.main:app", "--reload", "--port", str(a.port)], env)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
