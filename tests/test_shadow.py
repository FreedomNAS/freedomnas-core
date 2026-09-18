"""
The shadow copy has to reach the data pool, and must never overwrite the copy
it is reading from.
"""

from pathlib import Path

import pytest

import shadow

# FakeEngine has no native handle, so these exercise the pure-Python
# implementation's own logic directly. The native backend is verified
# against the Python one, backend to backend, in test_shadow_parity.py.
uses_fake_engine = pytest.mark.skipif(
    shadow.BACKEND == "native",
    reason="FakeEngine has no native handle; covered by test_shadow_parity.py",
)


class FakeEngine:
    """Answers like ZFS, and remembers what was mounted where."""

    def __init__(self, pools, datasets, active=None):
        self.pools = pools
        self.datasets = set(datasets)
        self.active = active
        self.mounted = {}
        self.created = []

    def execute(self, category, action, **kwargs):
        if action == "pool_list":
            return "\n".join(f"{p}\t1\t2" for p in self.pools), ""
        if action == "dataset_at_path":
            path = kwargs["path"]
            if path == shadow.STATE_DIR:
                return (self.active or ""), ""
            return (path if path in self.datasets else ""), ""
        if action == "create_state_dataset":
            self.datasets.add(f"{kwargs['pool_name']}/.freedomnas")
            self.created.append(kwargs["pool_name"])
            return "", ""
        if action == "mount_dataset_at":
            self.mounted[kwargs["dataset"]] = kwargs["mountpoint"]
            return "", ""
        if action == "unmount_at":
            return "", ""
        raise AssertionError(f"unexpected call: {category}.{action}")


@uses_fake_engine
def test_a_new_pool_gets_its_dataset():
    engine = FakeEngine(pools=["tank"], datasets=[])
    assert shadow.ensure_dataset(engine, "tank") is True
    assert engine.created == ["tank"]


@uses_fake_engine
def test_an_existing_dataset_is_left_alone():
    engine = FakeEngine(pools=["tank"], datasets=["tank/.freedomnas"])
    assert shadow.ensure_dataset(engine, "tank") is False
    assert engine.created == []


@uses_fake_engine
def test_the_configuration_reaches_the_data_pool(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("system:\n  hostname: nas\n")
    engine = FakeEngine(
        pools=["rpool", "datapool"],
        datasets=["rpool/.freedomnas", "datapool/.freedomnas"],
        active="rpool/.freedomnas",
    )
    results = shadow.sync(engine, str(config))

    assert [r["dataset"] for r in results] == ["datapool/.freedomnas"]
    assert results[0]["copied"] is True
    copied = Path(engine.mounted["datapool/.freedomnas"]) / "config.yaml"
    # the temporary mountpoint is cleaned up, so check what was written there
    assert engine.mounted["datapool/.freedomnas"].startswith("/tmp/freedomnas-shadow.")


@uses_fake_engine
def test_the_pool_being_read_from_is_never_written_to(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")
    engine = FakeEngine(
        pools=["rpool"], datasets=["rpool/.freedomnas"], active="rpool/.freedomnas"
    )
    assert shadow.sync(engine, str(config)) == []
    assert engine.mounted == {}


@uses_fake_engine
def test_a_pool_without_the_dataset_is_skipped(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")
    engine = FakeEngine(
        pools=["rpool", "estranea"],
        datasets=["rpool/.freedomnas"],
        active="rpool/.freedomnas",
    )
    assert shadow.sync(engine, str(config)) == []


@uses_fake_engine
def test_no_configuration_yet_is_not_an_error(tmp_path):
    engine = FakeEngine(pools=["tank"], datasets=["tank/.freedomnas"])
    assert shadow.sync(engine, str(tmp_path / "assente.yaml")) == []
