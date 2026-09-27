"""
The shadow copy has to reach the data pool, and must never overwrite the copy
it is reading from.
"""

from pathlib import Path

import shadow


class FakeEngine:
    """Answers like ZFS, and remembers what was mounted where."""

    profile = {"paths": {"config_dir": "/etc/freedomnas"}}

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
            if path == "/etc/freedomnas/state":
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


def test_a_new_pool_gets_its_dataset():
    engine = FakeEngine(pools=["tank"], datasets=[])
    assert shadow.ensure_dataset(engine, "tank") is True
    assert engine.created == ["tank"]


def test_an_existing_dataset_is_left_alone():
    engine = FakeEngine(pools=["tank"], datasets=["tank/.freedomnas"])
    assert shadow.ensure_dataset(engine, "tank") is False
    assert engine.created == []


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


def test_the_pool_being_read_from_is_never_written_to(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")
    engine = FakeEngine(
        pools=["rpool"], datasets=["rpool/.freedomnas"], active="rpool/.freedomnas"
    )
    assert shadow.sync(engine, str(config)) == []
    assert engine.mounted == {}


def test_a_pool_without_the_dataset_is_skipped(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")
    engine = FakeEngine(
        pools=["rpool", "estranea"],
        datasets=["rpool/.freedomnas"],
        active="rpool/.freedomnas",
    )
    assert shadow.sync(engine, str(config)) == []


def test_no_configuration_yet_is_not_an_error(tmp_path):
    engine = FakeEngine(pools=["tank"], datasets=["tank/.freedomnas"])
    assert shadow.sync(engine, str(tmp_path / "assente.yaml")) == []


class MountingEngine(FakeEngine):
    """Like FakeEngine, but a dataset keeps what was written into its mount."""

    def __init__(self, state, storage, **kwargs):
        super().__init__(**kwargs)
        self.state = state
        self.storage = storage

    def execute(self, category, action, **kwargs):
        if action == "unmount_at":
            for dataset, mountpoint in self.mounted.items():
                if mountpoint == kwargs["mountpoint"]:
                    target = self.storage / dataset.replace("/", "_")
                    target.mkdir(exist_ok=True)
                    for f in Path(mountpoint).iterdir():
                        (target / f.name).write_bytes(f.read_bytes())
                        f.unlink()
            return "", ""
        if action == "mount_dataset":
            source = self.storage / kwargs["dataset"].replace("/", "_")
            for f in source.iterdir():
                (self.state / f.name).write_bytes(f.read_bytes())
            self.active = kwargs["dataset"]
            return "", ""
        return super().execute(category, action, **kwargs)


def test_the_setup_moves_the_configuration_onto_the_chosen_pool(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "config.yaml").write_text("system:\n  hostname: nas\n")
    storage = tmp_path / "zfs"
    storage.mkdir()
    monkeypatch.setattr(shadow.paths, "state_dir", lambda engine=None: state)
    engine = MountingEngine(state, storage, pools=["rpool", "tank"], datasets=[])

    assert shadow.activate(engine, "tank") == "tank/.freedomnas"

    assert engine.created == ["tank"]
    assert (storage / "tank_.freedomnas" / "config.yaml").read_text() == "system:\n  hostname: nas\n"
    assert engine.active == "tank/.freedomnas"
    assert (state / "config.yaml").read_text() == "system:\n  hostname: nas\n"


def test_activating_the_pool_already_serving_the_state_does_nothing():
    engine = FakeEngine(pools=["tank"], datasets=["tank/.freedomnas"], active="tank/.freedomnas")
    assert shadow.activate(engine, "tank") == "tank/.freedomnas"
    assert engine.created == [] and engine.mounted == {}


def test_a_state_already_served_from_another_pool_is_not_moved():
    engine = FakeEngine(pools=["rpool", "tank"], datasets=["rpool/.freedomnas"], active="rpool/.freedomnas")
    try:
        shadow.activate(engine, "tank")
    except shadow.CommandExecutionError as exc:
        assert "rpool/.freedomnas" in str(exc)
    else:
        raise AssertionError("activate moved a configuration already served from another pool")
    assert engine.created == []
