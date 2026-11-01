"""
The scheduler's two promises: it keeps the number of snapshots the policy
asks for, and it destroys nothing it did not create.
"""

from datetime import datetime

import pytest

# The exception class comes from the module under test, not from `engine`:
# core modules import it as `freedomnas.engine` when that resolves and as
# `engine` otherwise, and the two are different classes to `except`.
from snapshots import CommandExecutionError, PREFIX, SnapshotScheduler, snapshot_name


class FakeEngine:
    """Remembers snapshots the way ZFS would list them back."""

    def __init__(self, existing=()):
        self.snapshots = list(existing)
        self.destroyed = []
        self.calls = []

    def execute(self, category, action, **kwargs):
        if action in ("snapshot_create", "snapshot_create_recursive"):
            self.calls.append(action)
            self.snapshots.append(f"{kwargs['dataset']}@{kwargs['snap_name']}")
            return "", ""
        if action == "snapshot_list":
            return "\n".join(f"{s}\t1700000000\t0" for s in self.snapshots), ""
        if action in ("snapshot_destroy", "snapshot_destroy_recursive"):
            self.calls.append(action)
            full = f"{kwargs['dataset']}@{kwargs['snap_name']}"
            self.snapshots.remove(full)
            self.destroyed.append(full)
            return "", ""
        raise AssertionError(f"unexpected call: {category}.{action}")


class FakeConfig:
    def __init__(self, policies):
        self.policies = policies

    def get_snapshot_policies(self):
        return self.policies


def older(dataset, interval, count):
    """`count` snapshots of ours, timestamped a day apart."""
    return [
        f"{dataset}@{PREFIX}-{interval}-2026090{i}-030000"
        for i in range(1, count + 1)
    ]


def test_name_sorts_by_age():
    early = snapshot_name("hourly", datetime(2026, 9, 10, 3, 0, 0))
    late = snapshot_name("hourly", datetime(2026, 9, 10, 4, 0, 0))
    assert early < late


def test_creates_one_snapshot_per_policy():
    engine = FakeEngine()
    config = FakeConfig([{"dataset": "tank/home", "hourly": 3}])
    results = SnapshotScheduler(engine, config).run("hourly", datetime(2026, 9, 10, 3, 0, 0))

    assert results[0]["created"] == "tank/home@freedomnas-hourly-20260910-030000"
    assert engine.snapshots == ["tank/home@freedomnas-hourly-20260910-030000"]


def test_an_interval_the_policy_does_not_ask_for_is_skipped():
    engine = FakeEngine()
    config = FakeConfig([{"dataset": "tank/home", "hourly": 3, "daily": 0}])
    assert SnapshotScheduler(engine, config).run("daily") == []
    assert engine.snapshots == []


def test_prunes_down_to_the_kept_count():
    engine = FakeEngine(older("tank/home", "hourly", 5))
    config = FakeConfig([{"dataset": "tank/home", "hourly": 3}])
    result = SnapshotScheduler(engine, config).run("hourly", datetime(2026, 9, 10, 3, 0, 0))[0]

    assert len(engine.snapshots) == 3
    assert len(result["destroyed"]) == 3      # 5 old + 1 new - 3 kept
    assert engine.snapshots[-1] == "tank/home@freedomnas-hourly-20260910-030000"
    # the oldest go first
    assert result["destroyed"][0].endswith("20260901-030000")


def test_never_destroys_what_it_did_not_create():
    foreign = [
        "tank/home@pre-update-20260101-000000",   # the update terminal's
        "tank/home@manuale",                      # somebody's own
        "tank/other@freedomnas-hourly-20260101-000000",   # another dataset
        "tank/home@freedomnas-daily-20260101-000000",     # another interval
    ]
    engine = FakeEngine(older("tank/home", "hourly", 4) + foreign)
    config = FakeConfig([{"dataset": "tank/home", "hourly": 1}])
    SnapshotScheduler(engine, config).run("hourly", datetime(2026, 9, 10, 3, 0, 0))

    for name in foreign:
        assert name in engine.snapshots, f"{name} was destroyed and must not have been"


def test_a_failed_snapshot_does_not_stop_the_other_policies():
    class Failing(FakeEngine):
        def execute(self, category, action, **kwargs):
            if action == "snapshot_create" and kwargs["dataset"] == "tank/broken":
                raise CommandExecutionError("dataset does not exist")
            return super().execute(category, action, **kwargs)

    engine = Failing()
    config = FakeConfig([
        {"dataset": "tank/broken", "daily": 2},
        {"dataset": "tank/home", "daily": 2},
    ])
    results = SnapshotScheduler(engine, config).run("daily")

    assert results[0]["error"] and results[0]["created"] is None
    assert results[1]["created"] is not None


def test_unknown_interval_is_refused():
    with pytest.raises(ValueError):
        SnapshotScheduler(FakeEngine(), FakeConfig([])).run("yearly")


def test_a_recursive_policy_takes_and_prunes_recursively():
    engine = FakeEngine(older("tank", "daily", 3))
    config = FakeConfig([{"dataset": "tank", "daily": 2, "recursive": True}])
    SnapshotScheduler(engine, config).run("daily", datetime(2026, 9, 10, 3, 0, 0))
    assert engine.calls == ["snapshot_create_recursive"] + ["snapshot_destroy_recursive"] * 2
    assert len([s for s in engine.snapshots if s.startswith("tank@")]) == 2


def test_a_policy_without_the_flag_stays_on_one_dataset():
    engine = FakeEngine()
    config = FakeConfig([{"dataset": "tank", "daily": 2}])
    SnapshotScheduler(engine, config).run("daily")
    assert engine.calls == ["snapshot_create"]
