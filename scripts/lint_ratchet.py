"""Lint ratchet: block NEW ruff/mypy problems without forcing a cleanup of legacy code.

  python scripts/lint_ratchet.py check            # CI: fail if anything got worse than the baseline
  python scripts/lint_ratchet.py update-baseline  # after you FIX things, lower the baseline

The baseline (.lint-baseline.json) stores counts per file per rule. A file/rule may go down, never up.
A file that is not in the baseline (new code) must have zero problems.
"""
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".lint-baseline.json"


def _run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def _ruff_cmd():
    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def ruff_counts():
    out = _run([*_ruff_cmd(), "check", ".", "--output-format", "json", "--exit-zero"])
    if out.returncode != 0:
        raise SystemExit(f"ruff failed to run:\n{out.stderr}")
    counts = Counter()
    for item in json.loads(out.stdout or "[]"):
        rel = Path(item["filename"]).resolve().relative_to(ROOT).as_posix()
        counts[f"ruff|{rel}|{item['code']}"] += 1
    return counts


def mypy_counts():
    out = _run([sys.executable, "-m", "mypy", "app", "migrate.py", "--no-error-summary"])
    if out.returncode not in (0, 1):
        raise SystemExit(f"mypy failed to run:\n{out.stdout}\n{out.stderr}")
    counts = Counter()
    for line in out.stdout.splitlines():
        if ": error:" in line:
            rel = line.split(":", 1)[0].replace("\\", "/")
            counts[f"mypy|{rel}|error"] += 1
    return counts


def current():
    c = Counter()
    c.update(ruff_counts())
    c.update(mypy_counts())
    return c


def main(argv):
    mode = argv[1] if len(argv) > 1 else "check"
    now = current()
    if mode == "update-baseline":
        BASELINE.write_text(json.dumps(dict(sorted(now.items())), indent=1) + "\n")
        print(f"Baseline written: {sum(now.values())} known problems in {len(now)} file/rule groups.")
        return 0
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    worse = {k: (base.get(k, 0), v) for k, v in now.items() if v > base.get(k, 0)}
    better = sum(base.get(k, 0) - now.get(k, 0) for k in base if now.get(k, 0) < base[k])
    if worse:
        print("Lint got WORSE than the baseline:")
        for k, (was, is_) in sorted(worse.items()):
            tool, path, rule = k.split("|")
            print(f"  {path}: {tool} {rule}  {was} -> {is_}")
        print("Fix the new problems. (Only run update-baseline after genuinely fixing old ones.)")
        return 1
    print(f"Lint OK. {sum(now.values())} legacy problems remain (baseline {sum(base.values())}).")
    if better:
        print(f"{better} problem(s) fixed since the baseline - run 'update-baseline' to lock that in.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
