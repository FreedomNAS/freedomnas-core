"""Snapshot names, listings, holds and diffs; volumes and quotas."""

import pytest

import datasets
import snapshots
from datasets import DatasetProblem


def test_split_name():
    assert snapshots.split_name("tank/a/b@daily-1") == ("tank/a/b", "daily-1")


@pytest.mark.parametrize("bad", ["tank", "tank@", "@x", "tank@a@b", "tank/@x", "tank@-r",
                                 "tank/a b@x", "tank@x y", "-tank@x"])
def test_split_name_refuses(bad):
    with pytest.raises(ValueError):
        snapshots.split_name(bad)


def test_check_part_and_tag():
    assert snapshots.check_part("manual-2026") == "manual-2026"
    assert snapshots.check_tag("keep+backup") == "keep+backup"
    for bad in ("", "-x", "a b", "a/b", "a@b"):
        with pytest.raises(ValueError):
            snapshots.check_part(bad)
    with pytest.raises(ValueError):
        snapshots.check_tag("-r")


def test_parse_list():
    rows = snapshots.parse_list(
        "tank/a@s1\t1700000000\t1024\t4096\t1\ttank/clone,tank/clone2\n"
        "tank/a@s2\t1700000100\t0\t4096\t0\t\n"
        "old@x\tMon Sep 22 2026\t0B\n")
    assert rows[0] == {"name": "tank/a@s1", "dataset": "tank/a", "snapshot": "s1",
                       "creation": 1700000000, "used": 1024, "referenced": 4096,
                       "holds": 1, "clones": ["tank/clone", "tank/clone2"]}
    assert rows[1]["clones"] == [] and rows[1]["holds"] == 0
    assert rows[2] == {"name": "old@x", "dataset": "old", "snapshot": "x",
                       "creation": "Mon Sep 22 2026", "used": "0B"}


def test_parse_bookmarks_and_holds():
    assert snapshots.parse_bookmarks("tank/a#b1\t1700000000\n") == [
        {"name": "tank/a#b1", "dataset": "tank/a", "bookmark": "b1", "creation": 1700000000}]
    assert snapshots.parse_holds("tank/a@s1\tkeep\t1700000000\n") == [
        {"tag": "keep", "time": 1700000000}]


def test_parse_diff_and_limit():
    text = "1.0\tM\t/\t/mnt/a\n1.1\t+\tF\t/mnt/a/x\\040y\n1.2\tR\tF\t/mnt/a/o\t/mnt/a/n\n"
    d = snapshots.parse_diff(text)
    assert d["total"] == 3 and not d["truncated"]
    assert d["changes"][1] == {"change": "created", "type": "file", "path": "/mnt/a/x y"}
    assert d["changes"][2]["new_path"] == "/mnt/a/n"
    assert snapshots.parse_diff(text, limit=1)["truncated"]


class Engine:
    def __init__(self, existing=("tank",), root="rpool"):
        self.existing = set(existing)
        self.root = root
        self.calls = []

    def execute(self, category, action, **kw):
        self.calls.append((action, kw))
        if action == "dataset_at_path":
            if kw["path"] == "/":
                return f"{self.root}/ROOT/default", ""
            return (kw["path"] if kw["path"] in self.existing else ""), ""
        if action == "dataset_list":
            return "".join(f"{d}\t1\t1\t1\t/{d}\n" for d in sorted(self.existing)), ""
        return "", ""


def test_create_volume():
    engine = Engine()
    assert datasets.create_volume(engine, "tank", "vm1", "20G", sparse=True, blocksize="16K",
                                  properties={"compression": "lz4"}) == "tank/vm1"
    action, kw = engine.calls[-1]
    assert action == "volume_create_sparse"
    assert kw["size"] == "20G" and kw["options"] == "-o compression=lz4 -o volblocksize=16K"


@pytest.mark.parametrize("args", [
    ("tank", "a/b", "1G"), ("tank", "vm", "lots"), ("tank", "vm", "1G", False, "3K"),
    ("rpool", "vm", "1G"), ("missing", "vm", "1G"), ("tank", "-x", "1G"),
])
def test_create_volume_refuses(args):
    with pytest.raises(DatasetProblem):
        datasets.create_volume(Engine(existing=("tank", "rpool")), *args)


def test_change_problem():
    engine = Engine(existing=("tank", "tank/a", "rpool", "rpool/ROOT"))
    assert datasets.change_problem(engine, "tank/a") is None
    assert "runs from" in datasets.change_problem(engine, "rpool/ROOT")


def test_quota_property_and_space():
    assert datasets.quota_property("user", "sarah") == "userquota@sarah"
    assert datasets.quota_property("group", "wheel", objects=True) == "groupobjquota@wheel"
    for kind, who in (("project", "x"), ("user", "-x"), ("user", "a b"), ("user", "")):
        with pytest.raises(DatasetProblem):
            datasets.quota_property(kind, who)
    assert datasets.parse_space("POSIX User\tsarah\t1024\tnone\t3\tnone\n") == [
        {"type": "POSIX User", "name": "sarah", "used": 1024, "quota": None,
         "objused": 3, "objquota": None}]


def test_clone_problem():
    engine = Engine(existing=("tank", "tank/a", "rpool"))
    assert datasets.clone_problem(engine, "tank/a", "tank/copy") is None
    assert "same pool" in datasets.clone_problem(engine, "tank/a", "other/copy")
    assert "already exists" in datasets.clone_problem(engine, "tank/a", "tank/a")
    assert "does not exist" in datasets.clone_problem(engine, "tank/a", "tank/x/y")
    assert datasets.clone_problem(engine, "tank/a", "tank/-x")
    assert "runs from" in datasets.clone_problem(engine, "rpool/a", "rpool/copy")
