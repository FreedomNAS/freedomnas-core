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


class Shares:
    def __init__(self, shares, users=()):
        self.smb = shares
        self.users = list(users)

    def get_users(self):
        return self.users

    def get_smb_shares(self):
        return self.smb

    def add_smb_share(self, share):
        self.smb = [s for s in self.smb if s["name"] != share["name"]] + [share]


def test_an_app_is_let_in_and_recorded_on_a_group_share():
    config, engine = Shares([{"name": "media", "path": "/tank/media", "group": "media"}]), FakeEngine()
    assert groupshares.add_app(engine, config, "/tank/media", "jellyfin", "1005", False)
    assert engine.calls == [("acl_user_read", {"user": "1005", "path": "/tank/media"})]
    assert config.smb[0]["apps"] == [{"app": "jellyfin", "account": "1005", "write": False, "granted": "read"}]
    assert not groupshares.add_app(engine, config, "/tank/other", "jellyfin", "1005", False)


def test_a_removed_app_leaves_the_acl_unless_another_app_needs_the_same_entry():
    config = Shares([{"name": "media", "path": "/tank/media", "group": "media", "apps": [
        {"app": "jellyfin", "account": "1005", "write": False},
        {"app": "jellyfin2", "account": "1005", "write": False},
        {"app": "nextcloud", "account": "33", "write": True}]}])
    engine = FakeEngine()
    groupshares.remove_app(engine, config, "jellyfin")
    assert engine.calls == []
    groupshares.remove_app(engine, config, "nextcloud")
    assert engine.calls == [("acl_user_write_remove", {"user": "33", "path": "/tank/media"})]
    assert [a["app"] for a in config.smb[0]["apps"]] == ["jellyfin2"]


def test_an_app_running_as_a_member_gets_the_member_s_rights():
    config = Shares([{"name": "media", "path": "/home/sarah/Video", "group": "media"}],
                    users=[{"username": "sarah", "uid": 1001, "groups": ["media"]}])
    engine = FakeEngine()
    groupshares.add_app(engine, config, "/home/sarah/Video", "jellyfin", "1001", False)
    assert engine.calls == [("acl_user_write", {"user": "1001", "path": "/home/sarah/Video"})]
    assert config.smb[0]["apps"][0]["granted"] == "write"
    config.users[0]["groups"] = []
    engine.calls.clear()
    groupshares._apply_apps(engine, config, "media")
    assert engine.calls == [("acl_user_write_remove", {"user": "1001", "path": "/home/sarah/Video"}),
                            ("acl_user_read", {"user": "1001", "path": "/home/sarah/Video"})]
    assert config.smb[0]["apps"][0]["granted"] == "read"
