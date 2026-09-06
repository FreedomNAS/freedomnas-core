"""
Every (category, action) the code asks the engine for must exist in every
profile.

Fourteen actions had gone missing this way — `system.timezone_get` returned a
500 from the WebUI, `system.timezone_set` failed on every reconcile, and the
encrypted-pool commands were absent from three editions — because nothing
ever compared what the Python calls against what the JSON defines. This test
is that comparison.
"""

import ast
import json
from pathlib import Path

import pytest

REPOS = Path(__file__).resolve().parent.parent.parent
PROFILES = REPOS / "freedomnas-profiles"
SOURCES = [REPOS / "freedomnas-core", REPOS / "freedomnas-webui"]

ENGINE_METHODS = {"execute", "get_command", "execute_with_stdin", "get_profile_value"}
EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]

# Only consulted on FreeBSD (api/routes/network.py branches on os_family).
BSD_ONLY = {("network", "list_interfaces_json")}


def _merged_profile(edition: str) -> dict:
    """Assemble a profile the way engine._load_profile() does."""
    profile = json.loads((PROFILES / "os" / f"os-{edition}.json").read_text())
    for entry in sorted(PROFILES.iterdir()):
        if not entry.is_dir() or entry.name in {"os", "init", "packages", "assets"}:
            continue
        fragment = entry / f"{entry.name}-{edition}.json"
        if fragment.exists():
            profile.update(json.loads(fragment.read_text()))
    init_system = profile.get("init_system", "systemd")
    init = PROFILES / "init" / f"init-{init_system}.json"
    if init.exists():
        profile["services"] = json.loads(init.read_text())["services"]
    return profile


def _engine_calls() -> dict:
    """Every (category, action) pair reached through the engine, with origins."""
    calls: dict = {}
    for root in SOURCES:
        for source in root.rglob("*.py"):
            if "__pycache__" in str(source) or f"{root.name}/tests/" in str(source):
                continue
            try:
                tree = ast.parse(source.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ENGINE_METHODS:
                    continue
                literals = [
                    a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                if len(literals) >= 2:
                    key = (literals[0], literals[1])
                    calls.setdefault(key, set()).add(f"{source.name}:{node.lineno}")
    return calls


pytestmark = pytest.mark.skipif(
    not PROFILES.is_dir(),
    reason="freedomnas-profiles is not checked out beside freedomnas-core",
)


@pytest.mark.parametrize("edition", EDITIONS)
def test_every_engine_call_exists_in_the_profile(edition):
    profile = _merged_profile(edition)
    missing = []
    for (category, action), origins in sorted(_engine_calls().items()):
        if (category, action) in BSD_ONLY and edition != "freebsd":
            continue
        if action not in profile.get(category, {}):
            missing.append(f"{category}.{action} (called from {', '.join(sorted(origins))})")

    assert not missing, (
        f"os-{edition}.json and its fragments define no such action:\n  "
        + "\n  ".join(missing)
    )


def test_the_scan_actually_finds_calls():
    """A silent parse failure would make the test above pass on nothing."""
    calls = _engine_calls()
    assert len(calls) > 30, f"only {len(calls)} engine calls found — is the scan working?"
    assert ("system", "hostname_set") in calls
