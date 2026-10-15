"""
The root pool's `.freedomnas` is this install's truth; a data pool's is a
portable copy that never competes with it, never overwrites what belongs to
this install, and brings back a reinstall.
"""

from pathlib import Path

import pytest
import yaml

import shadow


class Zfs:
    """
    Answers like ZFS for the commands shadow.py uses. A dataset keeps its
    files and its mountpoint; mounting one over a directory hides what the
    directory held until it is unmounted again.
    """

    def __init__(self, tmp_path, pools=("rpool", "tank"), datasets=None, active=None):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.pools = list(pools)
        self.datasets = {}
        for name, files in (datasets or {}).items():
            self.datasets[name] = {"mountpoint": str(self.state), "files": dict(files)}
        self.active = None
        self.hidden = {}
        self.mounted_at = {}
        self.fail_mount = set()
        self.destroyed = []
        self.profile = {"paths": {"config_dir": str(tmp_path)}}
        if active:
            self._mount_state(active)

    def _mount_state(self, dataset):
        self.hidden = {f.name: f.read_bytes() for f in self.state.iterdir()}
        for f in self.state.iterdir():
            f.unlink()
        for name, data in self.datasets[dataset]["files"].items():
            (self.state / name).write_bytes(data)
        self.active = dataset

    def _unmount_state(self):
        self.datasets[self.active]["files"] = {f.name: f.read_bytes() for f in self.state.iterdir()}
        for f in self.state.iterdir():
            f.unlink()
        for name, data in self.hidden.items():
            (self.state / name).write_bytes(data)
        self.hidden = {}
        self.active = None

    def execute(self, category, action, check=True, **kw):
        if action == "pool_list":
            return "\n".join(f"{p}\t1\t2" for p in self.pools), ""
        if action == "dataset_at_path":
            path = kw["path"]
            if path == "/":
                return "rpool/ROOT/gentoo\n", ""
            if path == str(self.state):
                return (self.active or "rpool/ROOT/gentoo") + "\n", ""
            return (path if path in self.datasets else ""), ""
        if action == "create_state_dataset":
            name = f"{kw['pool_name']}/.freedomnas"
            self.datasets[name] = {"mountpoint": str(self.state), "files": {}}
            return "", ""
        if action == "dataset_get_property":
            return f"{self.datasets[kw['dataset']]['mountpoint']}\tlocal\n", ""
        if action == "dataset_set":
            assert kw["dataset"] != self.active, "moved the mountpoint of the dataset in use"
            self.datasets[kw["dataset"]][kw["property"]] = kw["value"]
            return "", ""
        if action == "mount_dataset_at":
            if kw["dataset"] in self.fail_mount:
                raise shadow.CommandExecutionError("cannot mount")
            for name, data in self.datasets[kw["dataset"]]["files"].items():
                (Path(kw["mountpoint"]) / name).write_bytes(data)
            self.mounted_at[kw["mountpoint"]] = kw["dataset"]
            return "", ""
        if action == "unmount_at":
            dataset = self.mounted_at.pop(kw["mountpoint"])
            directory = Path(kw["mountpoint"])
            self.datasets[dataset]["files"] = {f.name: f.read_bytes() for f in directory.iterdir()}
            for f in directory.iterdir():
                f.unlink()
            return "", ""
        if action == "mount_dataset":
            if kw["dataset"] in self.fail_mount:
                raise shadow.CommandExecutionError("cannot mount")
            assert self.datasets[kw["dataset"]]["mountpoint"] == str(self.state)
            self._mount_state(kw["dataset"])
            return "", ""
        if action == "unmount_dataset":
            assert kw["dataset"] == self.active
            self._unmount_state()
            return "", ""
        if action == "group_exists":
            return "", ""
        if action == "dataset_destroy":
            self.destroyed.append(kw["dataset"])
            return "", ""
        raise AssertionError(f"unexpected call: {category}.{action}")

    def config(self, dataset):
        return yaml.safe_load(self.datasets[dataset]["files"]["config.yaml"])

    @property
    def os_type(self):
        return "gentoo"

    init_system = "openrc"
    profile_dir = "/nonexistent"


@pytest.fixture(autouse=True)
def _paths(monkeypatch):
    monkeypatch.setattr(shadow.paths, "config_dir", lambda engine=None: Path(engine.profile["paths"]["config_dir"]))


def dump(config):
    return yaml.safe_dump(config).encode()


def system(saved_at, **extra):
    return {"initialized": True, "saved_at": saved_at, "edition": "gentoo", **extra}


GENTOO = {
    "system": system("2026-10-14T20:00:00+00:00", hostname="sarahs-box"),
    "users": [{"username": "sarah", "groups": ["docker"], "admin": True, "uid": 1001}],
    "shares": {"smb": [{"name": "foto"}], "nfs": []},
    "storage": {"pools": [{"name": "mainpool"}]},
    "network": {"interfaces": [{"logical_name": "fnas_lan1"}], "interfaces_mapping": {}},
    "updates": {},
}


def test_a_data_pool_gets_a_copy_that_never_mounts_over_the_state(tmp_path):
    zfs = Zfs(tmp_path)
    assert shadow.ensure_dataset(zfs, "tank") is True
    assert zfs.datasets["tank/.freedomnas"]["mountpoint"] == str(tmp_path / "shared")
    assert shadow.ensure_dataset(zfs, "tank") is False


def test_the_root_pool_keeps_the_state_mountpoint(tmp_path):
    zfs = Zfs(tmp_path)
    shadow.ensure_dataset(zfs, "rpool")
    assert zfs.datasets["rpool/.freedomnas"]["mountpoint"] == str(zfs.state)


def test_the_configuration_reaches_the_data_pool_and_not_its_source(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": b"system: {}\n"},
                                  "tank/.freedomnas": {}},
              active="rpool/.freedomnas")
    results = shadow.sync(zfs, str(zfs.state / "config.yaml"))
    assert [(r["dataset"], r["copied"]) for r in results] == [("tank/.freedomnas", True)]
    assert zfs.datasets["tank/.freedomnas"]["files"]["config.yaml"] == b"system: {}\n"
    assert zfs.datasets["tank/.freedomnas"]["mountpoint"] == str(tmp_path / "shared")


def test_a_fresh_install_keeps_its_answers_on_the_root_pool(tmp_path):
    zfs = Zfs(tmp_path, pools=["rpool"])
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n")
    (zfs.state / "setup-pending").write_text("install\n")

    result = shadow.boot(zfs)

    assert result == {"internal": "rpool/.freedomnas", "action": "none", "source": None, "keep": []}
    assert zfs.active == "rpool/.freedomnas"
    assert set(zfs.datasets["rpool/.freedomnas"]["files"]) == {"config.yaml", "setup-pending"}
    assert zfs.hidden == {}, "copies left on the root filesystem under the mount"


def test_a_reinstall_restores_the_data_pools_copy(tmp_path):
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n")
    (zfs.state / "setup-pending").write_text("image\n")

    result = shadow.boot(zfs)

    assert (result["action"], result["source"], result["keep"]) == ("restored", "tank", ["tank"])
    assert yaml.safe_load((zfs.state / "config.yaml").read_text())["system"]["hostname"] == "sarahs-box"
    assert (zfs.state / "setup-pending").read_text() == "image\n"


def test_another_installs_newer_copy_brings_only_what_belongs_to_the_pool(tmp_path):
    debian = {
        "system": system("2026-10-15T01:00:00+00:00", hostname="debian-test", edition="debian"),
        "users": [{"username": "sarah", "groups": ["sudo", "docker"], "admin": True, "uid": 1000},
                  {"username": "luca", "groups": ["sudo"], "uid": 1003}],
        "shares": {"smb": [{"name": "foto"}, {"name": "documenti"}], "nfs": []},
        "storage": {"pools": [{"name": "mainpool"}, {"name": "tank"}]},
        "network": {"interfaces": [{"logical_name": "fnas_lan9"}], "interfaces_mapping": {}},
    }
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": {"config.yaml": dump(debian)}})

    result = shadow.boot(zfs)

    assert (result["action"], result["source"], result["keep"]) == ("merged", "tank", ["tank"])
    merged = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert merged["system"]["hostname"] == "sarahs-box"
    assert merged["system"]["edition"] == "gentoo"
    assert merged["network"]["interfaces"] == [{"logical_name": "fnas_lan1"}]
    assert merged["users"][0] == {"username": "sarah", "groups": ["docker"], "admin": True, "uid": 1001}
    assert merged["users"][1] == {"username": "luca", "groups": [], "admin": False, "uid": 1003}
    assert [s["name"] for s in merged["shares"]["smb"]] == ["foto", "documenti"]
    assert [p["name"] for p in merged["storage"]["pools"]] == ["mainpool", "tank"]
    assert merged["system"]["saved_at"] > debian["system"]["saved_at"]


def test_an_older_copy_changes_nothing(tmp_path):
    old = {**GENTOO, "system": system("2026-10-14T08:00:00+00:00"), "shares": {"smb": [], "nfs": []}}
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": {"config.yaml": dump(old)}})
    before = zfs.datasets["rpool/.freedomnas"]["files"]["config.yaml"]

    assert shadow.boot(zfs)["action"] == "local"
    assert (zfs.state / "config.yaml").read_bytes() == before


def test_a_state_served_from_a_data_pool_moves_home_and_nothing_is_destroyed(tmp_path):
    """Every install set up before the internal dataset: the wizard put the
    state on the data pool and destroyed the root pool's."""
    zfs = Zfs(tmp_path, datasets={"mainpool/.freedomnas": {"config.yaml": dump(GENTOO)}},
              pools=["rpool", "mainpool"], active="mainpool/.freedomnas")

    assert shadow.boot(zfs)["action"] == "local"

    assert zfs.active == "rpool/.freedomnas"
    assert zfs.config("rpool/.freedomnas") == yaml.safe_load(dump(GENTOO))
    assert zfs.config("mainpool/.freedomnas") == yaml.safe_load(dump(GENTOO))
    assert zfs.datasets["mainpool/.freedomnas"]["mountpoint"] == str(tmp_path / "shared")
    assert zfs.destroyed == []


def test_an_internal_dataset_that_cannot_mount_gives_the_old_one_back(tmp_path):
    zfs = Zfs(tmp_path, datasets={"mainpool/.freedomnas": {"config.yaml": dump(GENTOO)}},
              pools=["rpool", "mainpool"], active="mainpool/.freedomnas")
    zfs.fail_mount = {"rpool/.freedomnas"}
    zfs.datasets["rpool/.freedomnas"] = {"mountpoint": str(zfs.state), "files": {}}

    with pytest.raises(shadow.CommandExecutionError):
        shadow.activate(zfs)

    assert zfs.active == "mainpool/.freedomnas"
    assert yaml.safe_load((zfs.state / "config.yaml").read_text())["system"]["hostname"] == "sarahs-box"


def test_the_wizard_gives_the_chosen_pool_a_copy(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {}}, active="rpool/.freedomnas")
    assert shadow.activate(zfs, "tank") == "rpool/.freedomnas"
    assert zfs.active == "rpool/.freedomnas"
    assert zfs.datasets["tank/.freedomnas"]["mountpoint"] == str(tmp_path / "shared")


def test_a_root_filesystem_outside_zfs_is_left_alone(tmp_path):
    class NoZfsRoot(Zfs):
        def execute(self, category, action, check=True, **kw):
            if action == "dataset_at_path" and kw["path"] == "/":
                return "", ""
            return super().execute(category, action, check=check, **kw)

    zfs = NoZfsRoot(tmp_path, pools=[])
    assert shadow.boot(zfs) == {"internal": None, "action": "none", "source": None, "keep": []}
