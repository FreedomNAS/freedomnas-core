"""
The root pool's `.freedomnas` is this install's truth; a data pool's is a
portable copy that never competes with it, never overwrites what belongs to
this install, and brings back a reinstall.
"""

from pathlib import Path

import shutil

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
        self.bootfs = {}
        self.passwd = {}
        self.cards = []
        self.profile = {"paths": {"config_dir": str(tmp_path)}}
        self.values = {}
        self.snapshots = {}
        self.groups = {}
        self.renumbered = []
        if active:
            self._mount_state(active)

    def get_profile_value(self, category, key):
        try:
            return self.values[(category, key)]
        except KeyError:
            raise shadow.CommandTranslationError(f"{category}.{key}")

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
            value = self.datasets[kw["dataset"]].get(kw["property"], "-")
            return f"{value}\tlocal\n", ""
        if action == "pool_get_all":
            return f"bootfs\t{self.bootfs.get(kw['pool_name'], '-')}\tdefault\n", ""
        if action == "get_user_info":
            name = kw["username"]
            if name.isdigit():
                name = next((n for n, e in self.passwd.items() if e[0] == int(name)), name)
            entry = self.passwd.get(name)
            return (f"{name}:x:{entry[0]}:{entry[1]}::/home/{name}:/bin/sh\n" if entry else ""), ""
        if action == "dataset_set":
            assert kw["property"] != "mountpoint" or kw["dataset"] != self.active, \
                "moved the mountpoint of the dataset in use"
            self.datasets[kw["dataset"]][kw["property"]] = kw["value"]
            return "", ""
        if action == "mount_dataset_at":
            if kw["dataset"] in self.fail_mount:
                raise shadow.CommandExecutionError("cannot mount")
            for name, data in self.datasets[kw["dataset"]]["files"].items():
                (Path(kw["mountpoint"]) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(kw["mountpoint"]) / name).write_bytes(data)
            for snap, files in self.snapshots.get(kw["dataset"], {}).items():
                for name, data in files.items():
                    target = Path(kw["mountpoint"]) / ".zfs" / "snapshot" / snap / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            self.mounted_at[kw["mountpoint"]] = kw["dataset"]
            return "", ""
        if action == "unmount_at":
            dataset = self.mounted_at.pop(kw["mountpoint"])
            directory = Path(kw["mountpoint"])
            self.datasets[dataset]["files"] = {str(f.relative_to(directory)): f.read_bytes()
                                               for f in directory.rglob("*") if f.is_file()
                                               and f.relative_to(directory).parts[0] != ".zfs"}
            for f in directory.iterdir():
                shutil.rmtree(f) if f.is_dir() else f.unlink()
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
        if action == "list_physical":
            return "".join(f"{name} {mac} e1000e\n" for name, mac in self.cards), ""
        if action == "group_exists":
            group = kw["group"]
            if group.isdigit():
                group = next((n for n, g in self.groups.items() if g == int(group)), None)
            return (f"{group}:x:{self.groups[group]}:\n" if group in self.groups else ""), ""
        if action == "group_gid_set":
            old = self.groups[kw["group"]]
            self.groups[kw["group"]] = int(kw["gid"])
            self.passwd = {n: (u, int(kw["gid"]) if g == old else g) for n, (u, g) in self.passwd.items()}
            self.renumbered.append(("group", kw["group"], int(kw["gid"])))
            return "", ""
        if action == "snapshot_create":
            snaps = self.snapshots.setdefault(kw["dataset"], {})
            snaps[kw["snap_name"]] = dict(self.datasets[kw["dataset"]]["files"])
            return "", ""
        if action == "snapshot_list":
            return "".join(f"{ds}@{name}\t{1000 + i}\t0\t0\t0\t\n"
                           for ds, snaps in self.snapshots.items() for i, name in enumerate(snaps)), ""
        if action == "snapshot_destroy":
            del self.snapshots[kw["dataset"]][kw["snap_name"]]
            return "", ""
        if action == "uid_set":
            self.passwd[kw["username"]] = (int(kw["uid"]), self.passwd[kw["username"]][1])
            self.renumbered.append(("uid", kw["username"], int(kw["uid"])))
            return "", ""
        if action == "gid_set":
            self.passwd[kw["username"]] = (self.passwd[kw["username"]][0], int(kw["gid"]))
            self.groups[kw["group"]] = int(kw["gid"])
            self.renumbered.append(("gid", kw["username"], int(kw["gid"])))
            return "", ""
        if action == "dataset_destroy":
            self.destroyed.append(kw["dataset"])
            return "", ""
        if (category, action) == ("services", "restart"):
            self.restarted = kw["service_name"]
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
    restored = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert restored["system"]["hostname"] == "sarahs-box"
    assert (zfs.state / "setup-pending").read_text() == "image\n"


def test_the_copy_the_installer_was_told_to_restore_wins_over_a_newer_one(tmp_path):
    older = {**GENTOO, "system": system("2026-10-01T10:00:00+00:00", hostname="old-box")}
    newer = {**GENTOO, "system": system("2026-10-20T10:00:00+00:00", hostname="new-box")}
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(older)},
                                  "backup/.freedomnas": {"config.yaml": dump(newer)}})
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n  restore_from: tank\n")

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("restored", "tank")
    restored = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert restored["system"]["hostname"] == "old-box" and "restore_from" not in restored["system"]


def test_a_new_nas_chosen_in_the_installer_restores_nothing(tmp_path):
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n  restore: false\n")

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("none", None)
    assert yaml.safe_load((zfs.state / "config.yaml").read_text())["system"]["initialized"] is False


def test_a_restore_from_another_edition_records_the_running_one(tmp_path):
    debian = {**GENTOO, "system": system("2026-10-14T20:00:00+00:00", edition="debian")}
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(debian)}})
    assert shadow.boot(zfs)["action"] == "restored"
    assert yaml.safe_load((zfs.state / "config.yaml").read_text())["system"]["edition"] == "gentoo"


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


def test_an_older_copy_still_brings_what_belongs_to_the_pool(tmp_path):
    old = {**GENTOO, "system": system("2026-10-14T08:00:00+00:00"),
           "shares": {"smb": [{"name": "foto"}, {"name": "documenti"}], "nfs": []},
           "storage": {"pools": [{"name": "tank"}]},
           "users": GENTOO["users"] + [{"username": "luca", "groups": [], "uid": 1003}]}
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": {"config.yaml": dump(old)}})

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("merged", "tank")
    merged = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert [s["name"] for s in merged["shares"]["smb"]] == ["foto", "documenti"]
    assert [p["name"] for p in merged["storage"]["pools"]] == ["mainpool", "tank"]
    assert [(u["username"], u["admin"]) for u in merged["users"]] == [("sarah", True), ("luca", False)]


def test_a_copy_with_nothing_new_changes_nothing(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
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


def test_the_internal_dataset_is_marked_as_such(tmp_path):
    zfs = Zfs(tmp_path)

    shadow.boot(zfs)

    assert zfs.datasets["rpool/.freedomnas"][shadow.ROLE_PROPERTY] == "internal"


def test_another_installs_root_pool_is_never_read_moved_kept_or_written(tmp_path):
    debian = {**GENTOO, "system": system("2026-10-15T05:15:00+00:00", edition="debian")}
    zfs = Zfs(tmp_path, pools=("rpool", "poolj", "tank"), datasets={
        "rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
        "poolj/.freedomnas": {"config.yaml": dump(debian)},
        "tank/.freedomnas": {"config.yaml": dump(GENTOO)},
    })
    zfs.bootfs["poolj"] = "poolj/ROOT/debian"

    result = shadow.boot(zfs)
    shadow.sync(zfs, str(zfs.state / "config.yaml"))
    shadow.ensure_dataset(zfs, "poolj")

    assert (result["action"], result["source"]) == ("local", None)
    assert "poolj" not in result["keep"]
    assert zfs.datasets["poolj/.freedomnas"]["mountpoint"] == str(zfs.state)
    assert zfs.config("poolj/.freedomnas")["system"]["edition"] == "debian"


def test_a_root_pool_marked_internal_is_foreign_even_without_bootfs(tmp_path):
    zfs = Zfs(tmp_path, pools=("poolj", "rpool"), datasets={
        "rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
    })
    zfs.datasets["rpool/.freedomnas"][shadow.ROLE_PROPERTY] = "internal"

    assert shadow.foreign_root(zfs, "rpool", root="poolj")
    assert not shadow.foreign_root(zfs, "rpool")


def test_pools_with_a_copy_or_a_module_stay_imported(tmp_path):
    config = {**GENTOO, "storage": {"pools": []},
              "modules": {"containers": {"pool": "strataos"}}}
    zfs = Zfs(tmp_path, pools=("rpool", "mainpool", "strataos", "gamespool"), datasets={
        "rpool/.freedomnas": {"config.yaml": dump(config)},
        "mainpool/.freedomnas": {"config.yaml": dump(config)},
    })

    result = shadow.boot(zfs)

    assert result["keep"] == ["mainpool", "strataos"]


def test_a_restored_user_who_exists_here_gets_the_copys_uid(tmp_path):
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n")
    zfs.passwd["sarah"] = (1000, 1000)

    result = shadow.boot(zfs)

    sarah = yaml.safe_load((zfs.state / "config.yaml").read_text())["users"][0]
    assert (sarah["uid"], sarah["gid"]) == (1001, 1000)
    assert result["realigned"] == ["sarah uid 1000 -> 1001"]


def test_the_account_holding_a_restored_users_uid_moves_to_a_free_one(tmp_path):
    zfs = Zfs(tmp_path, datasets={"tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n")
    zfs.passwd = {"sarah": (1000, 1000), "admin": (1001, 1001)}

    result = shadow.boot(zfs)

    sarah = yaml.safe_load((zfs.state / "config.yaml").read_text())["users"][0]
    assert sarah["uid"] == 1001 and zfs.passwd["admin"][0] == 1002
    assert result["realigned"] == ["admin uid 1001 -> 1002", "sarah uid 1000 -> 1001"]


BOX = {
    "system": system("2026-10-13T10:00:00+00:00", hostname="box", edition="artix"),
    "users": [{"username": "sarah", "groups": [], "admin": True, "uid": 1001}],
    "shares": {"smb": [], "nfs": []},
    "storage": {"pools": [{"name": "tank"}]},
    "network": {
        "interfaces": [
            {"logical_name": "fnas_lan2", "mac_address": "1c:86:0b:32:98:ae", "master": "fnas_bridge1"},
            {"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan2"],
             "config": {"method": "static", "ip": "192.168.1.200", "cidr": 24}},
        ],
        "interfaces_mapping": {"1c:86:0b:32:98:ae": "fnas_lan2", "00:1e:4f:aa:bb:cc": "fnas_lan1"},
    },
}

DEBIAN_VM = {
    "system": system("2026-10-15T09:15:00+00:00", hostname="debian-vm", edition="debian"),
    "users": [{"username": "sarah", "groups": ["docker"], "admin": True, "uid": 1000}],
    "shares": {"smb": [], "nfs": []},
    "storage": {"pools": []},
    "network": {
        "interfaces": [{"logical_name": "fnas_lan1", "mac_address": "52:54:00:fa:80:01",
                        "config": {"method": "dhcp"}},
                       {"logical_name": "fnas_lan3", "config": {"method": "dhcp"}}],
        "interfaces_mapping": {"52:54:00:fa:80:01": "fnas_lan1"},
    },
}


def here(config):
    return {**config, "system": {**config["system"], "edition": "gentoo"}}


def test_the_machines_network_comes_from_the_data_pool_even_when_older(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(here(DEBIAN_VM))},
                                  "tank/.freedomnas": {"config.yaml": dump(BOX)}})
    zfs.cards = [("enp3s0", "1c:86:0b:32:98:ae"), ("enp0s25", "00:1e:4f:aa:bb:cc")]

    result = shadow.boot(zfs)

    assert (result["action"], result["network"]) == ("merged", "tank")
    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert config["network"] == BOX["network"]
    assert config["system"]["hostname"] == "box"
    assert config["users"] == DEBIAN_VM["users"]
    marker = yaml.safe_load((zfs.state / shadow.NETWORK_MARKER).read_text())
    assert marker == {"source": "tank", "forget": ["fnas_lan1", "fnas_lan3"]}


def test_a_copy_from_another_machine_leaves_the_network_alone(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(DEBIAN_VM)},
                                  "tank/.freedomnas": {"config.yaml": dump(BOX)}})
    zfs.cards = [("enp1s0", "52:54:00:fa:80:01")]

    result = shadow.boot(zfs)

    assert "network" not in result
    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert config["network"] == DEBIAN_VM["network"]
    assert config["system"]["hostname"] == "debian-vm"
    assert not (zfs.state / shadow.NETWORK_MARKER).exists()


def test_the_same_network_is_not_taken_twice(tmp_path):
    same = {**DEBIAN_VM, "network": BOX["network"], "system": {**DEBIAN_VM["system"], "hostname": "box"}}
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(same)},
                                  "tank/.freedomnas": {"config.yaml": dump(BOX)}})
    zfs.cards = [("fnas_lan2", "1c:86:0b:32:98:ae")]

    assert "network" not in shadow.boot(zfs)
    assert not (zfs.state / shadow.NETWORK_MARKER).exists()


def test_the_newest_copy_that_knows_this_machine_gives_the_network(tmp_path):
    elsewhere = {**BOX, "system": system("2026-10-15T12:00:00+00:00", hostname="elsewhere"),
                 "network": {"interfaces": [], "interfaces_mapping": {"aa:bb:cc:dd:ee:ff": "fnas_lan1"}}}
    zfs = Zfs(tmp_path, pools=("rpool", "tank", "backup"),
              datasets={"rpool/.freedomnas": {"config.yaml": dump(here(DEBIAN_VM))},
                        "tank/.freedomnas": {"config.yaml": dump(BOX)},
                        "backup/.freedomnas": {"config.yaml": dump(elsewhere)}})
    zfs.cards = [("enp3s0", "1c:86:0b:32:98:ae")]

    result = shadow.boot(zfs)

    assert (result["action"], result["source"], result["network"]) == ("merged", "backup", "tank")
    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert config["system"]["hostname"] == "box"
    assert config["network"] == BOX["network"]


def test_a_user_the_configuration_has_no_ids_for_gets_this_systems(tmp_path):
    old = {**GENTOO, "users": GENTOO["users"] + [{"username": "luca", "groups": []}]}
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(old)}})
    zfs.passwd = {"luca": (1003, 1003), "sarah": (1001, 1001)}

    shadow.boot(zfs)

    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert [(u["username"], u.get("uid"), u.get("gid")) for u in config["users"]] == [
        ("sarah", 1001, 1001), ("luca", 1003, 1003)]


def _mirror_setup(tmp_path, config):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(config)},
                                  "tank/.freedomnas": {"config.yaml": b"old\n"}},
              active="rpool/.freedomnas")
    return zfs, zfs.state / "config.yaml"


def _wait(mirror):
    mirror._timer.join(5)


def test_a_save_reaches_the_data_pool_moments_later(tmp_path):
    zfs, path = _mirror_setup(tmp_path, GENTOO)
    mirror = shadow.mirror_after_save(zfs, delay=0.05)
    mirror(path)
    _wait(mirror)
    assert zfs.config("tank/.freedomnas") == GENTOO


def test_saves_close_together_give_one_copy_of_the_last(tmp_path, monkeypatch):
    zfs, path = _mirror_setup(tmp_path, GENTOO)
    copies = []
    real = shadow._copy_into
    monkeypatch.setattr(shadow, "_copy_into", lambda e, d, s: copies.append(s.read_bytes()) or real(e, d, s))
    mirror = shadow.mirror_after_save(zfs, delay=0.2)
    mirror(path)
    last = {**GENTOO, "shares": {"smb": [], "nfs": []}}
    path.write_bytes(dump(last))
    mirror(path)
    _wait(mirror)
    assert copies == [dump(last)]
    assert zfs.config("tank/.freedomnas")["shares"] == {"smb": [], "nfs": []}


def test_a_configuration_not_initialized_never_overwrites_the_copy(tmp_path):
    zfs, path = _mirror_setup(tmp_path, {"system": {"initialized": False}})
    mirror = shadow.mirror_after_save(zfs, delay=0.05)
    mirror(path)
    _wait(mirror)
    assert zfs.datasets["tank/.freedomnas"]["files"]["config.yaml"] == b"old\n"


def test_a_change_mirrored_after_saving_survives_the_next_boot(tmp_path):
    zfs, path = _mirror_setup(tmp_path, GENTOO)
    changed = {**GENTOO, "system": system("2026-10-24T04:24:00+00:00", hostname="sarahs-box"),
               "modules": {"proxy": {"enabled": False}}}
    path.write_bytes(dump(changed))
    mirror = shadow.mirror_after_save(zfs, delay=0.05)
    mirror(path)
    _wait(mirror)
    zfs._unmount_state()
    shadow.boot(zfs)
    assert yaml.safe_load((zfs.state / "config.yaml").read_text())["modules"]["proxy"] == {"enabled": False}


def test_the_nas_identity_travels_with_the_copy_and_comes_back_on_a_reinstall(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)}, "tank/.freedomnas": {}},
              active="rpool/.freedomnas")
    ssh = tmp_path / "ssh"
    ssh.mkdir()
    zfs.values = {("system", "ssh_host_key_dir"): str(ssh), ("system", "ssh_service"): "sshd"}
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "ca.pem").write_text("CA CERT")
    (tls / "ca-key.pem").write_text("CA KEY")
    (tls / "key.pem").write_text("server key, not part of it")
    (ssh / "ssh_host_ed25519_key").write_text("HOST KEY")
    (ssh / "ssh_host_ed25519_key.pub").write_text("HOST PUB")

    shadow.sync(zfs, str(zfs.state / "config.yaml"))
    kept = zfs.datasets["tank/.freedomnas"]["files"]
    assert {k for k in kept if k.startswith("identity/")} == {
        "identity/tls/ca.pem", "identity/tls/ca-key.pem",
        "identity/ssh/ssh_host_ed25519_key", "identity/ssh/ssh_host_ed25519_key.pub"}

    for f in (tls / "ca.pem", tls / "ca-key.pem", ssh / "ssh_host_ed25519_key"):
        f.write_text("made by the fresh install")
    zfs._unmount_state()
    del zfs.datasets["rpool/.freedomnas"]
    zfs.datasets["rpool/.freedomnas"] = {"mountpoint": str(zfs.state), "files": {}}
    (zfs.state / "config.yaml").write_text("system:\n  initialized: false\n")
    result = shadow.boot(zfs)

    assert result["action"] == "restored"
    assert (tls / "ca-key.pem").read_text() == "CA KEY" and (ssh / "ssh_host_ed25519_key").read_text() == "HOST KEY"
    assert oct((tls / "ca-key.pem").stat().st_mode & 0o777) == "0o600"
    assert oct((ssh / "ssh_host_ed25519_key.pub").stat().st_mode & 0o777) == "0o644"
    assert "ssh/ssh_host_ed25519_key" in result["identity"]
    assert zfs.restarted == "sshd"


def identity(tmp_path, zfs, ca="CA", key="KEY", host="HOST"):
    tls, ssh = tmp_path / "tls", tmp_path / "ssh"
    for d in (tls, ssh):
        d.mkdir(exist_ok=True)
    zfs.values = {("system", "ssh_host_key_dir"): str(ssh), ("system", "ssh_service"): "sshd"}
    (tls / "ca.pem").write_text(ca)
    (tls / "ca-key.pem").write_text(key)
    (ssh / "ssh_host_ed25519_key").write_text(host)
    return tls, ssh


def kept_identity(ca="CA", key="KEY", host="HOST"):
    return {"identity/tls/ca.pem": ca.encode(), "identity/tls/ca-key.pem": key.encode(),
            "identity/ssh/ssh_host_ed25519_key": host.encode()}


FREEBSD_BOX = {
    "system": system("2026-10-29T04:38:00+00:00", hostname="skewbox", edition="freebsd"),
    "users": [{"username": "sarah", "groups": [], "admin": True, "uid": 1002, "gid": 1002},
              {"username": "luca", "groups": [], "admin": False, "uid": 1003, "gid": 1003}],
    "shares": {"smb": [{"name": "media"}], "nfs": []},
    "storage": {"pools": [{"name": "tank"}]},
    "network": {"interfaces": [], "interfaces_mapping": {"52:54:00:12:34:56": "fnas_lan1"}},
    "modules": {"containers": {"pool": "tank", "apps": {"jellyfin": {"recipe": "freebsd"}}}},
}

RESTORED_BY_OLD_CODE = {
    "system": system("2026-10-29T04:40:00+00:00", hostname="skewbox", edition="gentoo"),
    "users": [{"username": "admin", "groups": [], "admin": True, "uid": 1000, "gid": 1000},
              {"username": "sarah", "groups": ["wheel"], "admin": True, "uid": 1001, "gid": 1001}],
    "shares": {"smb": [], "nfs": []},
    "storage": {"pools": [{"name": "tank"}]},
    "network": {"interfaces": [], "interfaces_mapping": {"52:54:00:12:34:56": "fnas_lan1"}},
}


def test_every_boot_snapshots_the_copies_and_keeps_the_newest_ten(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": {"config.yaml": dump(GENTOO)}})
    zfs.snapshots["tank/.freedomnas"] = {f"boot-2026010{i}-000000": {} for i in range(10)}
    zfs.snapshots["tank/.freedomnas"]["pre-artix"] = {}

    shadow.boot(zfs)

    names = list(zfs.snapshots["tank/.freedomnas"])
    assert len([n for n in names if n.startswith("boot-")]) == 10
    assert "boot-20260100-000000" not in names and "pre-artix" in names
    assert "rpool/.freedomnas" not in zfs.snapshots


def test_a_copy_left_incomplete_comes_back_from_its_newest_good_snapshot(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": b"system:\n  initialized: false\n"},
                                  "tank/.freedomnas": {"config.yaml": b"system: {"}})
    zfs.snapshots["tank/.freedomnas"] = {
        "boot-20261028-000000": {"config.yaml": dump(GENTOO), **kept_identity()},
        "boot-20261029-000000": {"config.yaml": b"system:\n  initialized: false\n"}}
    tls, _ = identity(tmp_path, zfs, ca="NEW", key="NEW", host="NEW")

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("restored", "tank")
    assert zfs.config("tank/.freedomnas")["system"]["hostname"] == "sarahs-box"
    assert (tls / "ca-key.pem").read_text() == "KEY"


def test_a_configuration_of_another_edition_is_set_up_again_from_the_copy(tmp_path):
    local = {**RESTORED_BY_OLD_CODE, "system": {**RESTORED_BY_OLD_CODE["system"], "edition": "debian"}}
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(local)},
                                  "tank/.freedomnas": {"config.yaml": dump(FREEBSD_BOX)}})
    zfs.passwd = {"admin": (1000, 1000), "sarah": (1001, 1001)}

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("reinitialized", "tank")
    assert "debian" in result["reason"]
    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    assert config["system"]["edition"] == "gentoo"
    assert config["modules"] == FREEBSD_BOX["modules"] and config["shares"] == FREEBSD_BOX["shares"]


def test_an_identity_other_than_the_one_this_machine_keeps_sets_the_install_up_again(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(RESTORED_BY_OLD_CODE)},
                                  "tank/.freedomnas": {"config.yaml": dump(FREEBSD_BOX), **kept_identity()}})
    zfs.cards = [("enp1s0", "52:54:00:12:34:56")]
    zfs.passwd = {"admin": (1000, 1000), "sarah": (1001, 1001)}
    zfs.groups = {"admin": 1000, "sarah": 1001}
    tls, ssh = identity(tmp_path, zfs, ca="IMAGE CA", key="IMAGE KEY", host="IMAGE HOST")

    result = shadow.boot(zfs)

    assert (result["action"], result["source"]) == ("reinitialized", "tank")
    assert (tls / "ca.pem").read_text() == "CA" and (tls / "ca-key.pem").read_text() == "KEY"
    assert (ssh / "ssh_host_ed25519_key").read_text() == "HOST" and zfs.restarted == "sshd"
    config = yaml.safe_load((zfs.state / "config.yaml").read_text())
    users = {u["username"]: u for u in config["users"]}
    assert set(users) == {"admin", "sarah", "luca"}
    assert users["sarah"]["admin"] and users["sarah"]["groups"] == ["wheel"]
    assert users["luca"]["admin"] is False
    assert zfs.renumbered == [("uid", "sarah", 1002), ("gid", "sarah", 1002)]
    assert (users["sarah"]["uid"], users["sarah"]["gid"]) == (1002, 1002)
    assert config["shares"] == FREEBSD_BOX["shares"] and config["modules"] == FREEBSD_BOX["modules"]
    assert config["system"]["edition"] == "gentoo"


def test_the_same_identity_keeps_the_configuration_of_this_install(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(RESTORED_BY_OLD_CODE)},
                                  "tank/.freedomnas": {"config.yaml": dump(FREEBSD_BOX), **kept_identity()}})
    zfs.cards = [("enp1s0", "52:54:00:12:34:56")]
    identity(tmp_path, zfs)

    assert shadow.boot(zfs)["action"] == "merged"
    assert zfs.renumbered == []


def test_a_pool_from_another_machine_never_brings_its_identity(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(RESTORED_BY_OLD_CODE)},
                                  "tank/.freedomnas": {"config.yaml": dump(FREEBSD_BOX), **kept_identity()}})
    zfs.cards = [("enp1s0", "52:54:00:aa:bb:cc")]
    tls, _ = identity(tmp_path, zfs, ca="MINE")

    assert shadow.boot(zfs)["action"] == "merged"
    assert (tls / "ca.pem").read_text() == "MINE"


def test_whoever_holds_the_copys_numbers_moves_to_free_ones(tmp_path):
    zfs = Zfs(tmp_path)
    zfs.passwd = {"sarah": (1001, 0), "luca": (1005, 1005), "anna": (1007, 1007), "daemon": (2, 2)}
    zfs.groups = {"luca": 1005, "anna": 1007, "media": 1003, "daemon": 2}
    users = [{"username": "sarah", "uid": 1001, "gid": 1001},
             {"username": "luca", "uid": 1007, "gid": 1003},
             {"username": "anna", "uid": 2, "gid": 1007},
             {"username": "nobody-here", "uid": 1009, "gid": 1009}]

    done = shadow._realign_ids(zfs, users)

    assert zfs.passwd["sarah"] == (1001, 0)
    assert zfs.passwd["luca"] == (1007, 1003) and zfs.groups["luca"] == 1003
    assert zfs.passwd["anna"] == (1000, 1007) and zfs.groups["media"] == 1000
    assert zfs.passwd["daemon"] == (2, 2)
    assert done == ["anna uid 1007 -> 1000", "luca uid 1005 -> 1007",
                    "media gid 1003 -> 1000", "luca gid 1005 -> 1003"]


def test_a_differing_identity_in_the_copy_is_never_overwritten(tmp_path):
    zfs = Zfs(tmp_path, datasets={"rpool/.freedomnas": {"config.yaml": dump(GENTOO)},
                                  "tank/.freedomnas": kept_identity()},
              active="rpool/.freedomnas")
    identity(tmp_path, zfs, ca="IMAGE CA", key="IMAGE KEY", host="HOST")
    (tmp_path / "ssh" / "ssh_host_rsa_key").write_text("NEW RSA")

    shadow.sync(zfs, str(zfs.state / "config.yaml"))

    kept = zfs.datasets["tank/.freedomnas"]["files"]
    assert kept["identity/tls/ca.pem"] == b"CA" and kept["identity/tls/ca-key.pem"] == b"KEY"
    assert kept["identity/ssh/ssh_host_rsa_key"] == b"NEW RSA"
