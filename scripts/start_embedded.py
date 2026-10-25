"""Start the local test server with NO PostgreSQL install: an embedded PostgreSQL (pip package pgserver) keeps its data in .localdb.

Used by start_local_test_embedded.bat. Everything is local; nothing touches Render. First run needs: pip install pgserver tzdata
(the script copies the time-zone files pgserver lacks, which the app needs for Asia/Kolkata).
"""
import shutil
import sys
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

server = pgserver.get_server(ROOT / ".localdb", cleanup_mode=None)
base = server.get_uri().rsplit("/", 1)[0]
print("Embedded PostgreSQL running:", base)

import run_local  # noqa: E402

sys.argv = [sys.argv[0], "--no-docker", "--db-url", base + "/vt_dev", *sys.argv[1:]]
run_local.main()
