"""
The native configuration manager must write the same file as PyYAML.

Byte equality is the point. An appliance saves config.yaml with whichever
engine is running, mirrors it onto every pool, and reads it back after a
reinstall with possibly the other one — so quoting, folding, key order and
the header all have to match, not just the values.

The `Last modified:` line is the one exception: it carries the time of the
save, so it is normalised away before comparing.
"""

import json
import random
import string as string_module
from pathlib import Path

import pytest
import yaml

pytest.importorskip("freedomnas_native", reason="native engine not built")

import config_native
import config_py

AWKWARD = [
    "", " lead", "trail ", "a: b", "a #b", "#lead", "-lead", "?lead", "...",
    "---", "it's", 'quote"d', "line\nbreak", "multi\n\nblank", "tab\tchar",
    "città", "日本", "x" * 100, " ".join(f"word{i}" for i in range(30)),
    "12", "yes", "no", "null", "~", "true", "off", "0x1f", "1.5", "12:30",
    "2026-07-18T10:30:00", "2026-07-18 10:30:00", "/mnt/tank/docs",
    "rw,sync,no_subtree_check", "[bracket]", "{brace}", "*star", "&amp",
    "%pct", "@at", "`tick`", "a  b", "end\n", "\nstart",
]

HAND_WRITTEN = [
    "",
    "# only a comment\n",
    "system:\n  hostname: nas\n  initialized: false\n",
    "flow: {a: 1, b: [2, 3]}\n",
    "block: |\n  line1\n  line2\n",
    "folded: >\n  a\n  b\n",
    "when: 2026-07-18 10:30:00\nday: 2026-07-18\n",
    "quoted: \"a\\tb\"\nsingle: 'it''s'\n",
    "numbers:\n  hex: 0x1f\n  oct: 0o17\n  sexa: 12:30\n  under: 1_000\n  neg: -5\n",
    "empties:\n  l: []\n  m: {}\n  n: null\n  tilde: ~\n  blank:\n",
    "deep:\n  a:\n    b:\n    - 1\n    - x: y\n",
]


def managers(tmp_path):
    """One of each, writing to files of their own."""
    return (
        config_py.ConfigurationManager(config_path=str(tmp_path / "py" / "config.yaml")),
        config_native.ConfigurationManager(config_path=str(tmp_path / "nim" / "config.yaml")),
    )


def normalise(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("# Last modified:")
    )


def saved(manager) -> str:
    return normalise(Path(manager.config_path).read_text())


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_a_fresh_config_is_written_identically(tmp_path):
    py, native = managers(tmp_path)
    py.save()
    native.save()
    assert saved(py) == saved(native)


def test_a_configured_appliance_is_written_identically(tmp_path):
    py, native = managers(tmp_path)
    for manager in (py, native):
        manager.set_hostname("freedomnas")
        manager.set_timezone("Europe/Rome")
        manager.mark_initialized("debian", "installer")
        manager.add_interface({
            "logical_name": "eth0",
            "mac_address": "00:1a:2b:3c:4d:5e",
            "config": {"method": "static", "ip": "192.168.1.50", "cidr": 24,
                       "gateway": "192.168.1.1", "dns": ["1.1.1.1", "8.8.8.8"]},
        })
        manager.add_user({"username": "admin", "groups": ["sudo", "samba"],
                          "shell": "/bin/bash"})
        manager.add_pool({"name": "rpool", "vdev_type": "single", "ashift": 12,
                          "compression": "lz4"})
        manager.add_pool({"name": "datapool", "vdev_type": "mirror",
                          "encrypted": True, "usb_keyslot_devices": 2})
        manager.add_smb_share({"name": "Documents", "path": "/mnt/datapool/documents",
                               "read_only": False, "valid_users": ["admin"],
                               "guest_ok": False})
        manager.add_nfs_share({"name": "backup", "path": "/mnt/datapool/backup",
                               "allowed_hosts": "192.168.1.0/24",
                               "options": "rw,sync,no_subtree_check"})
        manager.add_installed_feature("samba-cifs")
        manager.set_snapshot_policy("tank/home", {"hourly": 24, "daily": 7})
        manager.configure_topgrade(["system", "containers"])

    # The timestamps these record differ by microseconds between the two
    # runs, so compare the file with them blanked out.
    def blank_times(text):
        return "\n".join(
            line.split(": ")[0] + ": <time>"
            if line.strip().startswith(("initialized_at:", "installed_at:", "configured_at:"))
            else line
            for line in text.splitlines()
        )

    assert blank_times(saved(py)) == blank_times(saved(native))


@pytest.mark.parametrize("value", AWKWARD)
def test_awkward_values_are_written_identically(tmp_path, value):
    py, native = managers(tmp_path)
    for manager in (py, native):
        manager.config["system"]["hostname"] = value
        manager.config["users"] = [{"username": value, "groups": [value]}]
        manager.config["network"]["interfaces_mapping"] = {"00:11": value}
        manager.save()
    assert saved(py) == saved(native)


@pytest.mark.parametrize("key", [k for k in AWKWARD if k and "\n" not in k])
def test_awkward_keys_are_written_identically(tmp_path, key):
    py, native = managers(tmp_path)
    for manager in (py, native):
        manager.config["reporting"] = {key: "value"}
        manager.save()
    assert saved(py) == saved(native)


def test_random_structures_are_written_identically(tmp_path):
    rng = random.Random(20260916)

    def value(depth=0):
        choice = rng.random()
        if depth > 2 or choice < 0.5:
            return rng.choice([
                rng.choice(AWKWARD), rng.randint(-5, 10 ** 6), rng.random() * 1000,
                True, False, None,
                "".join(rng.choice(string_module.printable) for _ in range(rng.randint(0, 40))),
            ])
        if choice < 0.75:
            return [value(depth + 1) for _ in range(rng.randint(0, 4))]
        return {f"k{rng.randint(0, 9)}": value(depth + 1) for _ in range(rng.randint(0, 4))}

    py, native = managers(tmp_path)
    for _ in range(150):
        blob = {f"key{i}": value() for i in range(rng.randint(1, 5))}
        for manager in (py, native):
            manager.config["reporting"] = blob
            manager.save()
        assert saved(py) == saved(native), json.dumps(blob, default=str)[:400]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", HAND_WRITTEN)
def test_hand_written_yaml_loads_identically(tmp_path, text):
    results = []
    for name, module in (("py", config_py), ("nim", config_native)):
        path = tmp_path / name / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        try:
            results.append(("ok", module.ConfigurationManager(config_path=str(path)).config))
        except Exception as exc:
            results.append(("raised", type(exc).__name__, str(exc)))
    assert results[0] == results[1]


def test_what_one_engine_writes_the_other_rewrites_unchanged(tmp_path):
    py, native = managers(tmp_path)
    py.mark_initialized("void", "webui")
    py.add_user({"username": "admin", "groups": ["wheel"], "shell": "/bin/sh"})
    py.add_smb_share({"name": "Long Share Name " + "x" * 60,
                      "path": "/mnt/tank/" + "y" * 70, "read_only": True})
    written = Path(py.config_path).read_text()

    reread = tmp_path / "nim" / "config.yaml"
    reread.parent.mkdir(parents=True, exist_ok=True)
    reread.write_text(written)
    reloaded = config_native.ConfigurationManager(config_path=str(reread))
    assert reloaded.config == py.config

    reloaded.save()
    assert normalise(reread.read_text()) == normalise(written)


def test_invalid_configs_fail_identically(tmp_path):
    broken = [
        "users: not a list\n",
        "system: {}\nnetwork: {}\nusers: []\nshares: {}\nstorage: {}\nupdates: {}\n",
        "system:\n  initialized: maybe\n",
        "shares:\n  smb: {}\n  nfs: []\n",
        "storage:\n  pools: 3\n",
        "network:\n  interfaces_mapping: []\n",
        "- a list at the top level\n",
        "just a string\n",
        "a: [unclosed\n",
        "\tbad indent\n",
    ]
    for index, text in enumerate(broken):
        results = []
        for name, module in (("py", config_py), ("nim", config_native)):
            path = tmp_path / f"{name}{index}" / "config.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            try:
                module.ConfigurationManager(config_path=str(path))
                results.append(("ok", None))
            except Exception as exc:
                results.append(("raised", type(exc).__name__))
        assert results[0] == results[1], text


def test_defaults_match_pyyaml_and_the_python_manager(tmp_path):
    py, native = managers(tmp_path)
    assert native.config == py.config
    assert native.config == config_py.ConfigurationManager.DEFAULT_CONFIG


def test_saved_file_is_valid_yaml_for_pyyaml(tmp_path):
    _, native = managers(tmp_path)
    native.config["reporting"] = {"note": " ".join(f"w{i}" for i in range(40)),
                                  "unicode": "città", "multi": "a\n\nb"}
    native.save()
    round_tripped = yaml.safe_load(Path(native.config_path).read_text())
    assert round_tripped["reporting"] == native.config["reporting"]


def test_file_permissions_match(tmp_path):
    py, native = managers(tmp_path)
    py.save()
    native.save()
    assert (Path(py.config_path).stat().st_mode & 0o777
            == Path(native.config_path).stat().st_mode & 0o777)
