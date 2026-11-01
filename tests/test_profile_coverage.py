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
from functools import lru_cache
from pathlib import Path

import pytest

REPOS = Path(__file__).resolve().parent.parent.parent
PROFILES = REPOS / "freedomnas-profiles"
SOURCES = [
    REPOS / "freedomnas-core",
    REPOS / "freedomnas-webui",
    REPOS / "freedomnas-installer",
]

# The installer reaches the engine through its own ExecutionContext, which is
# why `host_run`/`target_run`/`stage_run` belong here: they resolve the same
# (category, action) pairs against the same profiles.
ENGINE_METHODS = {
    "execute", "get_command", "execute_with_stdin", "get_profile_value",
    "host_run", "target_run", "stage_run", "target_run_stdin", "has",
}
EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]

# Only consulted on FreeBSD (api/routes/network.py branches on os_family;
# bootstate.py asks for the installed kernel only without a UKI).
BSD_ONLY = {("network", "list_interfaces_json"), ("bootloader", "kernel_installed")}
# Boot environments (bootenv.py, asked only after bootenv.supported() found be_list).
BSD_ONLY |= {("bootloader", action) for action in
             ("be_list", "be_create", "be_activate", "be_destroy", "be_activate_once", "be_mountpoint",
              "be_mount", "be_umount", "be_fix_loader", "be_fingerprint", "be_enter", "be_leave")}
BSD_ONLY |= {("updates", "be_update")}

# Looked up inside a try/except: an edition declares one only when it needs
# to correct the tool being configured, and its absence is the normal case.
OPTIONAL = {("updates", "topgrade_extra_config")}

# The UKI's rollback slot and boot state (bootstate.py, the update terminal):
# editions that boot a UKI declare them, FreeBSD boots its own loader.
UKI_ONLY = {("bootloader", action) for action in
            ("uki_backup", "boot_ok", "autorollback_set", "boot_state", "rollback_forget")}


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
    # Per-edition first, exactly as _load_init_profile() does: runit on Artix
    # is not runit on Void.
    for name in (f"init-{init_system}-{edition}.json", f"init-{init_system}.json"):
        init = PROFILES / "init" / name
        if init.exists():
            profile["services"] = json.loads(init.read_text())["services"]
            break
    return profile


guarded: set = set()


@lru_cache(maxsize=None)
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
                    if node.func.attr == "has":
                        guarded.add(key)
                    else:
                        calls.setdefault(key, set()).add(f"{source.name}:{node.lineno}")
    return calls


def _guarded_pairs() -> set:
    """
    Pairs the code asks `has()` about before calling.

    An action behind that question is optional by construction — the edition
    that lacks it takes the other branch — so it is not a coverage failure.
    Deriving it from the code beats a hand-kept list, which goes stale.
    """
    _engine_calls()
    return guarded


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
        if (category, action) in UKI_ONLY and edition == "freebsd":
            continue
        if (category, action) in OPTIONAL or (category, action) in _guarded_pairs():
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
    assert ("chroot", "mount") in calls, "the installer's own calls are not being scanned"


@pytest.mark.parametrize("edition", EDITIONS)
def test_every_edition_installs_a_boot_chain(edition):
    """
    Arch shipped an empty `bootloader` section: every branch in
    bootloader_installer.py is behind a `has()`, so the installer completed
    without writing anything to the ESP and the machine could not boot.
    Nothing else here would notice, because "optional everywhere" is exactly
    what the coverage test above is told to ignore.
    """
    bootloader = _merged_profile(edition).get("bootloader", {})
    assert "build_uki" in bootloader or "install_efi_loader" in bootloader, (
        f"os-{edition}.json declares no way to install a boot chain: "
        f"the installer would finish leaving the ESP empty"
    )


@pytest.mark.parametrize("edition", EDITIONS)
def test_no_fragment_replaces_a_section_of_another(edition):
    """
    The fragments are merged with a plain dict.update(): a category a
    fragment shares with the OS profile or with another fragment replaces
    it whole. A `paths` category in storage-*.json once wiped the OS
    profile's `paths` (install_root, config_dir) on every edition.
    """
    owners = {key: f"os-{edition}.json"
              for key in json.loads((PROFILES / "os" / f"os-{edition}.json").read_text())}
    clashes = []
    for entry in sorted(PROFILES.iterdir()):
        if not entry.is_dir() or entry.name in {"os", "init", "packages", "assets"}:
            continue
        fragment = entry / f"{entry.name}-{edition}.json"
        if not fragment.exists():
            continue
        for key in json.loads(fragment.read_text()):
            if key in owners:
                clashes.append(f"{fragment.name} redefines {key!r} from {owners[key]}")
            owners[key] = fragment.name
    assert not clashes, "\n  ".join(["sections replaced by a later fragment:"] + clashes)
