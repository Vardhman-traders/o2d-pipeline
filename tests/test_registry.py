"""Keeps tests/registry.yaml honest: every test it names must really exist."""
import re

import yaml

from tests.conftest import ROOT


def test_registry_points_at_real_tests():
    data = yaml.safe_load((ROOT / "tests" / "registry.yaml").read_text(encoding="utf-8"))
    ids = [e["id"] for e in data["entries"]]
    assert len(ids) == len(set(ids)), "duplicate registry ids"
    missing = []
    for entry in data["entries"]:
        assert entry["speed"] in ("fast", "slow"), entry["id"]
        for node in entry["tests"]:
            path, _, func = node.partition("::")
            f = ROOT / path
            if not f.exists() or not re.search(rf"^def {re.escape(func)}\(", f.read_text(encoding="utf-8"), re.M):
                missing.append(node)
    assert not missing, f"registry names tests that do not exist: {missing}"
