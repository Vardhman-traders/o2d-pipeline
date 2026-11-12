"""Start the local test server with NO PostgreSQL install: an embedded PostgreSQL (pip package pgserver) keeps its data in .localdb.

Used by start_local_test_embedded.bat. Everything is local; nothing touches Render. First run needs: pip install pgserver tzdata
(the script copies the time-zone files pgserver lacks, which the app needs for Asia/Kolkata).
"""
import atexit
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import pgserver
    import tzdata
except ImportError:
    raise SystemExit("Missing packages. Run once:  pip install pgserver tzdata")

tz_dst = Path(pgserver.__file__).parent / "pginstall" / "share" / "postgresql" / "timezone"
if not tz_dst.exists():
    shutil.copytree(Path(tzdata.__file__).parent / "zoneinfo", tz_dst)

DATA = ROOT / ".localdb"
PG_BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"


def _port_if_ready() -> int | None:
    """The port of the database in .localdb when it is running and accepts connections, else None."""
    import psycopg2
    try:
        port = int((DATA / "postmaster.pid").read_text().splitlines()[3])
        psycopg2.connect(host="127.0.0.1", port=port, dbname="postgres", user="postgres", connect_timeout=3).close()
        return port
    except Exception:
        return None


def _wait_until_ready(seconds: int = 180) -> int:
    deadline = time.time() + seconds
    while time.time() < deadline:
        port = _port_if_ready()
        if port:
            return port
        time.sleep(2)
    raise SystemExit("The local database did not become ready in 3 minutes. Look at the file .localdb/log, close other copies of this window, and try again.")


def _start() -> int:
    """Start (or find) the database and return its port.

    It is started here, not by pgserver: pgserver starts it inside THIS console, so pressing Ctrl+C (or closing the window) killed the database
    too, which is why every start needed crash recovery (30 - 60 s on Windows, longer than pgserver waits). Started detached it survives Ctrl+C,
    is stopped properly when this script ends, and is simply found again if the window was closed with the X."""
    port = _port_if_ready()
    if port:
        return port
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    print("Starting the local database (the first start after an unclean stop can take up to a minute)...")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(DATA), "-w", "-t", "180", "-o", "-h 127.0.0.1", "-o", f"-p {port}",
                        "-l", str(DATA / "log"), "start"], check=True, timeout=200, creationflags=flags,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Waiting for the local database that is already starting...")
        return _wait_until_ready()
    return _wait_until_ready(60)


def _stop_cleanly() -> None:
    """Stop the database properly when this script ends, so the next start needs no crash recovery."""
    try:
        subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(DATA), "-w", "-m", "fast", "-t", "60", "stop"], timeout=90,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


db_port = _start()
atexit.register(_stop_cleanly)
base = f"postgresql://postgres:@127.0.0.1:{db_port}"
print("Embedded PostgreSQL running:", base)
print("Stop the server with Ctrl+C in this window: the database is shut down properly. (If the window is closed with the X, the database keeps running and is reused next time.)")

import run_local  # noqa: E402

sys.argv = [sys.argv[0], "--no-docker", "--db-url", base + "/vt_dev", *sys.argv[1:]]
run_local.main()
