"""
The native snapshot scheduler must answer exactly like the pure-Python one.

Both implementations run against a throwaway "zfs" profile whose snapshot_*
actions read and write a plain text file instead of talking to real ZFS —
each backend gets its own copy of that file, seeded identically, so the two
runs cannot interfere with each other. What is compared is the scheduler's
result: what got created, what got pruned, and — for a refused interval —
the exception class and message text.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py
import snapshots_native
import snapshots_py

WHEN = datetime(2026, 9, 10, 3, 0, 0)


def zfs_profile(state_path: Path) -> dict:
    return {
        "os_name": "Parity",
        "os_family": "linux",
        "os_version": "1",
        "init_system": "systemd",
        "zfs": {
            "snapshot_create": (
                "printf '%s@%s\\t0\\t0\\n' '{dataset}' '{snap_name}' >> " + str(state_path)
            ),
            "snapshot_list": f"cat {state_path}",
            "snapshot_destroy": (
                "awk -F'\\t' -v k='{dataset}@{snap_name}' '$1 != k' " + str(state_path) +
                " > " + str(state_path) + ".tmp && mv " + str(state_path) + ".tmp " + str(state_path)
            ),
        },
    }


class FakeConfig:
    def __init__(self, policies):
        self.policies = policies

    def get_snapshot_policies(self):
        return self.policies


def older(dataset, interval, count):
    return [
        f"{dataset}@freedomnas-{interval}-2026090{i}-030000\t0\t0"
        for i in range(1, count + 1)
    ]


@pytest.fixture
def scheduler_pair(tmp_path):
    """Two schedulers, each with its own isolated fake ZFS state file."""

    def build(existing_lines=()):
        schedulers = {}
        for name, engine_mod, snap_mod in [
            ("py", engine_py, snapshots_py),
            ("native", engine_native, snapshots_native),
        ]:
            root = tmp_path / name
            (root / "os").mkdir(parents=True)
            state = root / "state.tsv"
            text = "\n".join(existing_lines)
            state.write_text(text + "\n" if text else "")
            (root / "os" / "os-parity.json").write_text(
                json.dumps(zfs_profile(state))
            )
            engine = engine_mod.FreedomNASEngine.for_target(
                "parity", profile_dir=str(root)
            )
            schedulers[name] = snap_mod.SnapshotScheduler(engine, FakeConfig([]))
        return schedulers["py"], schedulers["native"]

    return build


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


def test_unknown_interval_fails_identically(scheduler_pair):
    py, native = scheduler_pair()
    assert (outcome(py.run, "yearly") == outcome(native.run, "yearly"))


def test_creates_one_snapshot_per_policy(scheduler_pair):
    py, native = scheduler_pair()
    py.config.policies = [{"dataset": "tank/home", "hourly": 3}]
    native.config.policies = [{"dataset": "tank/home", "hourly": 3}]

    assert (outcome(py.run, "hourly", WHEN) == outcome(native.run, "hourly", WHEN))


def test_an_interval_the_policy_does_not_ask_for_is_skipped(scheduler_pair):
    py, native = scheduler_pair()
    policies = [{"dataset": "tank/home", "hourly": 3, "daily": 0}]
    py.config.policies = policies
    native.config.policies = policies

    assert outcome(py.run, "daily") == outcome(native.run, "daily")


def test_prunes_down_to_the_kept_count(scheduler_pair):
    existing = older("tank/home", "hourly", 5)
    py, native = scheduler_pair(existing)
    policies = [{"dataset": "tank/home", "hourly": 3}]
    py.config.policies = policies
    native.config.policies = policies

    assert (outcome(py.run, "hourly", WHEN) == outcome(native.run, "hourly", WHEN))


def test_never_destroys_what_it_did_not_create(scheduler_pair):
    foreign = [
        "tank/home@pre-update-20260101-000000\t0\t0",
        "tank/home@manuale\t0\t0",
        "tank/other@freedomnas-hourly-20260101-000000\t0\t0",
        "tank/home@freedomnas-daily-20260101-000000\t0\t0",
    ]
    existing = older("tank/home", "hourly", 4) + foreign
    py, native = scheduler_pair(existing)
    policies = [{"dataset": "tank/home", "hourly": 1}]
    py.config.policies = policies
    native.config.policies = policies

    assert (outcome(py.run, "hourly", WHEN) == outcome(native.run, "hourly", WHEN))


def test_multiple_policies_in_one_run(scheduler_pair):
    py, native = scheduler_pair()
    policies = [
        {"dataset": "tank/home", "daily": 2},
        {"dataset": "tank/backup", "daily": 1},
        {"dataset": "", "daily": 5},
        {"dataset": "tank/ignored", "daily": 0},
    ]
    py.config.policies = policies
    native.config.policies = policies

    assert (outcome(py.run, "daily", WHEN) == outcome(native.run, "daily", WHEN))


def test_translation_error_is_raised_identically(tmp_path):
    # No "zfs" section at all: get_command fails to translate before any I/O.
    root = tmp_path / "empty"
    (root / "os").mkdir(parents=True)
    (root / "os" / "os-empty.json").write_text(json.dumps({
        "os_name": "Empty", "os_family": "linux", "os_version": "1",
        "init_system": "systemd",
    }))
    py_engine = engine_py.FreedomNASEngine.for_target("empty", profile_dir=str(root))
    native_engine = engine_native.FreedomNASEngine.for_target("empty", profile_dir=str(root))
    policies = [{"dataset": "tank/home", "hourly": 1}]
    py_scheduler = snapshots_py.SnapshotScheduler(py_engine, FakeConfig(policies))
    native_scheduler = snapshots_native.SnapshotScheduler(native_engine, FakeConfig(policies))

    assert (outcome(py_scheduler.run, "hourly", WHEN)
            == outcome(native_scheduler.run, "hourly", WHEN))
