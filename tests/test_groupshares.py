"""Group shares: a dataset's directory that belongs to a Unix group."""

import pytest

import datasets
import groupshares


class FakeEngine:
    def __init__(self, groups=(), directory_group=""):
        self.groups = set(groups)
        self.directory_group = directory_group
        self.calls = []

    def get_profile_value(self, category, key):
        return {"group_share_properties": {"acltype": "posixacl"},
                "group_share_mode": "2770", "owner_group": "{owner}"}[key]

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        if action == "group_exists":
            return (f"{kw['group']}:x:1000:" if kw["group"] in self.groups else ""), ""
        if action == "group_create":
            self.groups.add(kw["group"])
        if action == "group_get":
            return self.directory_group, ""
        return "", ""

    def actions(self):
        return [a for a, _ in self.calls]


@pytest.fixture(autouse=True)
def mounted(monkeypatch):
    monkeypatch.setattr(datasets, "dataset_mounted_at", lambda engine, path: "tank/foto")


SHARE = {"name": "Famiglia", "path": "/tank/foto", "group": "famiglia",
         "apps": [{"account": "33", "write": True}]}


def test_a_restored_share_gets_its_group_and_its_directory_back():
    engine = FakeEngine(directory_group="1004")
    assert groupshares.restore(engine, SHARE) == "/tank/foto given to the group famiglia"
    assert engine.actions() == ["group_exists", "group_create", "group_get", "acl_group_remove",
                                "dataset_set", "owner_set", "group_set_recursive", "mode_set",
                                "acl_group_set", "acl_user_write"]
    assert engine.calls[3][1]["group"] == "1004"


def test_a_share_that_already_matches_is_left_alone():
    engine = FakeEngine(groups={"famiglia"}, directory_group="famiglia")
    assert groupshares.restore(engine, SHARE) is None
    assert engine.actions() == ["group_exists", "group_get"]


def test_a_directory_that_is_not_a_dataset_cannot_be_a_group_share(monkeypatch):
    monkeypatch.setattr(datasets, "dataset_mounted_at", lambda engine, path: None)
    with pytest.raises(datasets.DatasetProblem):
        groupshares.give_to_group(FakeEngine(), "/tank/foto/sub", "famiglia")
