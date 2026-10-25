"""The Datasets tab: never the system's own pool, never over a directory in use."""

import posixpath

import pytest

import datasets
import pool_import
from datasets import DatasetProblem


class FakeEngine:
    """Datasets with an explicit mountpoint or None (inherited), like ZFS."""

    def __init__(self, explicit=None, snapshots=()):
        self.explicit = dict(explicit or {"rpool": "none", "rpool/ROOT": "/", "tank": None,
                                          "tank/media": None, "tank/.freedomnas": "/etc/freedomnas/state"})
        self.snapshots = list(snapshots)
        self.calls = []

    def effective(self, name):
        mp = self.explicit[name]
        if mp is not None:
            return mp, "local"
        if "/" not in name:
            return "/" + name, "default"
        parent, _ = self.effective(name.rsplit("/", 1)[0])
        if parent in ("none", "legacy"):
            return parent, f"inherited from {name.rsplit('/', 1)[0]}"
        return posixpath.join(parent, name.rsplit("/", 1)[1]), f"inherited from {name.rsplit('/', 1)[0]}"

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        if action == "dataset_at_path":
            if kw["path"] == "/":
                return "rpool/ROOT", ""
            return (kw["path"] if kw["path"] in self.explicit else ""), ""
        if action == "dataset_get_property":
            value, source = self.effective(kw["dataset"])
            return f"{value}\t{source}", ""
        if action == "dataset_create_with":
            opts = dict(o.split("=", 1) for o in kw["options"].split() if "=" in o)
            self.explicit[kw["dataset"]] = opts.get("mountpoint")
            return "", ""
        if action == "dataset_set":
            self.explicit[kw["dataset"]] = kw["value"]
            return "", ""
        if action == "dataset_inherit":
            self.explicit[kw["dataset"]] = None
            return "", ""
        if action == "dataset_rename":
            for name in sorted(self.explicit):
                if name == kw["dataset"] or name.startswith(kw["dataset"] + "/"):
                    self.explicit[kw["new_name"] + name[len(kw["dataset"]):]] = self.explicit.pop(name)
            return "", ""
        if action == "dataset_list":
            return "\n".join(f"{n}\t1\t2\t3\t{self.effective(n)[0]}" for n in self.explicit), ""
        if action == "snapshot_list":
            return "\n".join(f"{s}\t0\t0" for s in self.snapshots), ""
        if action in ("dataset_destroy", "dataset_destroy_recursive"):
            for name in list(self.explicit):
                if name == kw["dataset"] or name.startswith(kw["dataset"] + "/"):
                    del self.explicit[name]
            return "", ""
        raise AssertionError(f"unexpected {action}")

    def actions(self):
        return [a for a, _ in self.calls]


@pytest.fixture(autouse=True)
def nothing_occupied(monkeypatch):
    monkeypatch.setattr(pool_import, "_occupied", lambda path: None)


def test_the_system_pool_and_the_state_dataset_are_protected():
    assert datasets.protected_reason("rpool/ROOT", "rpool")
    assert datasets.protected_reason("tank/.freedomnas", "rpool")
    assert datasets.protected_reason("tank/media", "rpool") is None
    assert datasets.protected_reason("rpoolx/a", "rpool") is None


def test_a_dataset_inherits_its_mountpoint_by_default():
    engine = FakeEngine()
    assert datasets.create(engine, "tank", "films") == "tank/films"
    [(_, kw)] = [c for c in engine.calls if c[0] == "dataset_create_with"]
    assert kw["options"] == "" and kw["dataset"] == "tank/films"
    assert engine.effective("tank/films")[0] == "/tank/films"


def test_a_chosen_mountpoint_and_properties_become_options():
    engine = FakeEngine()
    datasets.create(engine, "tank/media", "films", "path", "/srv/films",
                    {"compression": "zstd", "casesensitivity": "insensitive"})
    [(_, kw)] = [c for c in engine.calls if c[0] == "dataset_create_with"]
    assert kw["options"] == "-o mountpoint=/srv/films -o compression=zstd -o casesensitivity=insensitive"


@pytest.mark.parametrize("parent, name, detail", [
    ("rpool", "x", "runs from"),
    ("tank", ".freedomnas", "configuration"),
    ("tank", "a/b", "one part"),
    ("tank", "-o", "each part"),
    ("tank", "media", "already exists"),
    ("ghost", "x", "does not exist"),
])
def test_bad_creations_are_refused(parent, name, detail):
    engine = FakeEngine()
    with pytest.raises(DatasetProblem, match=detail):
        datasets.create(engine, parent, name)
    assert "dataset_create_with" not in engine.actions()


def test_a_system_directory_or_a_busy_one_is_refused(monkeypatch):
    engine = FakeEngine()
    with pytest.raises(DatasetProblem, match="system directory"):
        datasets.create(engine, "tank", "x", "path", "/usr/x")
    monkeypatch.setattr(pool_import, "_occupied", lambda p: "the directory is not empty" if p == "/tank/films" else None)
    with pytest.raises(DatasetProblem, match="not empty"):
        datasets.create(engine, "tank", "films")


def test_the_mountpoint_moves_and_comes_back():
    engine = FakeEngine()
    assert datasets.set_mountpoint(engine, "tank/media", "path", "/srv/media") == {
        "old": "/tank/media", "new": "/srv/media"}
    assert datasets.set_mountpoint(engine, "tank/media", "inherit") == {
        "old": "/srv/media", "new": "/tank/media"}
    assert datasets.set_mountpoint(engine, "tank/media", "none")["new"] == "none"


def test_the_system_pool_keeps_its_mountpoints():
    engine = FakeEngine()
    with pytest.raises(DatasetProblem, match="runs from"):
        datasets.set_mountpoint(engine, "rpool/ROOT", "path", "/mnt/x")
    assert "dataset_set" not in engine.actions()


def test_a_rename_stays_in_its_pool_and_moves_an_inherited_mountpoint():
    engine = FakeEngine()
    engine.explicit["tank/films"] = None
    assert datasets.rename(engine, "tank/films", "tank/media/films") == {
        "old": "/tank/films", "new": "/tank/media/films"}
    with pytest.raises(DatasetProblem, match="stays in its pool"):
        datasets.rename(engine, "tank/media/films", "other/films")
    with pytest.raises(DatasetProblem, match="inside itself"):
        datasets.rename(engine, "tank/media", "tank/media/x")


def test_destroying_needs_the_tick_for_what_goes_along():
    engine = FakeEngine(snapshots=["tank/media@daily"])
    engine.explicit["tank/media/films"] = None
    with pytest.raises(DatasetProblem, match="1 child dataset.*1 snapshot"):
        datasets.destroy(engine, "tank/media")
    datasets.destroy(engine, "tank/media", recursive=True)
    assert engine.actions()[-1] == "dataset_destroy_recursive"
    assert "tank/media" not in engine.explicit


def test_a_pool_dataset_and_protected_ones_are_never_destroyed():
    engine = FakeEngine()
    for name, detail in (("tank", "goes with the pool"), ("tank/.freedomnas", "configuration"),
                         ("rpool/ROOT", "runs from")):
        with pytest.raises(DatasetProblem, match=detail):
            datasets.destroy(engine, name, recursive=True)
    assert "dataset_destroy_recursive" not in engine.actions()


def test_what_mounts_on_system_directories_is_protected_with_what_holds_it():
    rows = [("mainpool", "none"), ("mainpool/Home", "none"), ("mainpool/Home/home", "/home"),
            ("mainpool/config", "legacy"), ("mainpool/media", "/tank/media")]
    assert "system directory" in datasets.subtree_reason(rows, "mainpool/Home/home", "rpool")
    assert "mainpool/Home/home inside it" in datasets.subtree_reason(rows, "mainpool", "rpool")
    assert "fstab" in datasets.subtree_reason(rows, "mainpool/config", "rpool")
    assert datasets.subtree_reason(rows, "mainpool/media", "rpool") is None


def test_a_dataset_holding_home_is_neither_moved_nor_destroyed():
    engine = FakeEngine()
    engine.explicit["tank/home"] = "/home"
    for call in (lambda: datasets.destroy(engine, "tank/home", recursive=True),
                 lambda: datasets.set_mountpoint(engine, "tank", "path", "/mnt/tank"),
                 lambda: datasets.rename(engine, "tank/home", "tank/h")):
        with pytest.raises(DatasetProblem, match="system directory"):
            call()
    assert not {"dataset_destroy_recursive", "dataset_set", "dataset_rename"} & set(engine.actions())


class MountEngine:
    """One dataset and its mountpoint, for dataset_mounted_at."""

    def __init__(self, name, mountpoint):
        self.name, self.mountpoint = name, mountpoint

    def execute(self, category, action, timeout=30, check=True, **kw):
        if action == "dataset_at_path":
            if kw["path"] == "/":
                return "rpool/ROOT", ""
            return (self.name if kw["path"] == self.mountpoint.rstrip("/") else ""), ""
        if action == "dataset_get_property":
            return f"{self.mountpoint}\tlocal", ""
        return "", ""


@pytest.mark.parametrize("mountpoint, shared", [
    ("/home/sarah/Video/", True), ("/home/sarah/Video", True), ("/home/sarah/media/Film", True),
    ("/home/sarah", False), ("/home", False), ("/usr/local/media", False), ("/tank/media/", True)])
def test_a_dataset_inside_a_home_may_be_shared_but_not_a_home(mountpoint, shared):
    engine = MountEngine("mainpool/x", mountpoint)
    found = datasets.dataset_mounted_at(engine, mountpoint.rstrip("/") or "/")
    assert (found == "mainpool/x") is shared
