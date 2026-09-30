"""Report changes to EXISTING tests so a human decides before they merge.

Adding new tests is always fine. Editing or deleting/renaming a test that already exists on the base branch
may mean the change is hiding a regression, so this prints a report and exits 1 (the CI job then blocks the
merge until the PR carries the `tests-change-approved` label).

  python scripts/test_change_report.py origin/New_Development        # markdown report on stdout
Exit code: 0 = no existing tests touched, 1 = existing tests changed.
"""
import subprocess
import sys

TEST_PATHS = ["tests", "conftest.py"]


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main(base):
    changed = git("diff", "--name-status", "--diff-filter=MDR", f"{base}...HEAD", "--", *TEST_PATHS).splitlines()
    if not changed:
        print("No existing tests were modified, deleted or renamed. New tests only: nothing to approve.")
        return 0
    print("## Existing tests changed - needs your decision\n")
    print("These tests already existed on the base branch. Modifying or removing them can hide a regression.")
    print("Check each one below. If the change is intended, add the `tests-change-approved` label to the PR.\n")
    for line in changed:
        status, *paths = line.split("\t")
        label = {"M": "modified", "D": "DELETED", "R": "renamed"}.get(status[0], status)
        print(f"### `{paths[-1]}` - {label}")
        if status[0] == "D":
            print("The whole file was removed.\n")
            continue
        diff = git("diff", "-U2", f"{base}...HEAD", "--", paths[-1])
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        print(f"{removed} line(s) removed, {added} line(s) added.\n")
        print("```diff")
        body = diff.splitlines()
        print("\n".join(body[:120]))
        if len(body) > 120:
            print(f"... ({len(body) - 120} more diff lines)")
        print("```\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "origin/New_Development"))
