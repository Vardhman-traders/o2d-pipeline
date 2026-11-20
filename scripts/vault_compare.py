# ruff: noqa: E501
"""Compare what the exported copy holds with what this database holds NOW, table by table.

    python scripts/vault_compare.py --database-url postgresql://...@localhost:PORT/vt_dev  merged.sqlite

merged.sqlite is the combined, decrypted copy of the exported batches. For each table this prints rows, checksum and
exact money sums on both sides and says MATCH or DIFFERENT. Differences are expected if the source changed after the export.
It only READS this database, and refuses any database that is not on this computer unless you add --allow-remote.
Run it on the same groups the export used (they are read from the file)."""
import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True, help="the source database (explicit on purpose: .env is NOT used)")
    ap.add_argument("--allow-remote", action="store_true")
    ap.add_argument("merged")
    a = ap.parse_args()
    host = urlparse(a.database_url).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1") and not a.allow_remote:
        sys.exit(f"Refusing: {host} is not this computer. Add --allow-remote only if you really mean it.")
    from app import vault_export
    new = sqlite3.connect(a.merged)
    meta = dict(new.execute("SELECT key, value FROM _vault_meta").fetchall())
    groups = json.loads(meta["groups"])
    _, report = vault_export.build_snapshot(a.database_url, groups)
    print(f"groups: {groups}   exported copy is as of snapshot {meta.get('snapshot_at')}\n")
    print(f"{'table':28} {'rows here':>10} {'rows new':>10}  result")
    bad = 0
    for name, count, digest, sums in new.execute("SELECT name, row_count, digest, sums FROM _vault_tables ORDER BY name"):
        here = report["tables"].get(name)
        if here is None:
            print(f"{name:28} {'-':>10} {count:10}  DIFFERENT (not in this database's export list)")
            bad += 1
            continue
        same_sums = {k: Decimal(v) for k, v in json.loads(sums or "{}").items()} == {k: Decimal(v) for k, v in here["sums"].items()}
        ok = here["rows"] == count and here["digest"] == digest and same_sums
        bad += 0 if ok else 1
        note = "MATCH" if ok else "DIFFERENT" + ("" if here["rows"] == count else " (row counts differ)") + ("" if same_sums else " (money sums differ)")
        print(f"{name:28} {here['rows']:10} {count:10}  {note}")
    print(f"\n{'ALL TABLES MATCH' if not bad else str(bad) + ' table(s) differ'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
