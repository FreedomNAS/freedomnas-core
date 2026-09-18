"""
Reference behaviour of the pure-Python ReconciliationEngine.

White-box: it reaches into `_do_*`/`_apply_*`, which only exist on the pure
implementation — the native backend exposes `reconcile()` alone (see
reconcile_native.py), so this file imports reconcile_py directly rather
than through the reconcile.py backend switch. The two backends are held to
identical `reconcile()` output in tests/test_reconcile_parity.py.
"""

import pytest

from reconcile_py import (
    Change,
    CommandExecutionError,
    ReconcileMode,
    ReconciliationEngine,
)


class FakeEngine:
    def __init__(self, imported_pools=(), existing_users=(), service_names=None,
                 failing=()):
        self.imported_pools = set(imported_pools)
        self.existing_users = set(existing_users)
        self.service_names = service_names or {}
        self.failing = set(failing)
        self.calls = []

    def execute(self, category, action, **kwargs):
        self.calls.append((category, action, kwargs))
        if (category, action) in self.failing:
            raise CommandExecutionError(f"{category}.{action} failed")

        if (category, action) == ("zfs", "pool_list"):
            return "\n".join(f"{p}\t1\t2" for p in self.imported_pools), ""
        if (category, action) == ("users", "get_user_info"):
            exists = kwargs["username"] in self.existing_users
            return (kwargs["username"] if exists else ""), ""
        return "", ""

    def get_profile_value(self, category, key):
        try:
            return self.service_names[(category, key)]
        except KeyError:
            from reconcile_py import CommandTranslationError
            raise CommandTranslationError(f"no such value: {category}.{key}")


class FakeConfig:
    def __init__(self, state=None, nfs_shares=None):
        self.state = state or {}
        self._nfs_shares = nfs_shares if nfs_shares is not None else self.state.get("nfs_shares", [])

    def to_dict(self):
        return self.state

    def get_nfs_shares(self):
        return self._nfs_shares


def reconciler(engine=None, config=None):
    return ReconciliationEngine(engine or FakeEngine(), config or FakeConfig())


def action_names(results):
    return [r.action for r in results]


# ---------------------------------------------------------------------------
# compute_diff — system
# ---------------------------------------------------------------------------

def test_hostname_added_when_absent():
    r = reconciler()
    changes = r.compute_diff({"system": {"hostname": "nas"}}, {})
    assert changes == [Change("system.hostname", None, "nas", "add")]


def test_hostname_modified_when_different():
    r = reconciler()
    changes = r.compute_diff(
        {"system": {"hostname": "nas2"}}, {"system": {"hostname": "nas1"}}
    )
    assert changes == [Change("system.hostname", "nas1", "nas2", "modify")]


def test_hostname_unchanged_produces_no_diff():
    r = reconciler()
    assert r.compute_diff(
        {"system": {"hostname": "nas"}}, {"system": {"hostname": "nas"}}
    ) == []


def test_timezone_added_and_modified_like_hostname():
    r = reconciler()
    changes = r.compute_diff(
        {"system": {"timezone": "Europe/Rome"}}, {"system": {}}
    )
    assert changes == [Change("system.timezone", None, "Europe/Rome", "add")]


# ---------------------------------------------------------------------------
# compute_diff — users
# ---------------------------------------------------------------------------

def test_user_added():
    r = reconciler()
    alice = {"username": "alice", "groups": ["sudo"]}
    changes = r.compute_diff({"users": [alice]}, {"users": []})
    assert changes == [Change("users.alice", None, alice, "add")]


def test_user_modified_when_config_differs():
    r = reconciler()
    old = {"username": "alice", "groups": ["sudo"]}
    new = {"username": "alice", "groups": ["sudo", "docker"]}
    changes = r.compute_diff({"users": [new]}, {"users": [old]})
    assert changes == [Change("users.alice", old, new, "modify")]


def test_hybrid_mode_never_deletes_users():
    r = reconciler()
    alice = {"username": "alice", "groups": []}
    changes = r.compute_diff(
        {"users": []}, {"users": [alice]}, mode=ReconcileMode.HYBRID
    )
    assert changes == []


def test_declarative_mode_deletes_users_missing_from_desired():
    r = reconciler()
    alice = {"username": "alice", "groups": []}
    changes = r.compute_diff(
        {"users": []}, {"users": [alice]}, mode=ReconcileMode.DECLARATIVE
    )
    assert changes == [Change("users.alice", alice, None, "remove")]


# ---------------------------------------------------------------------------
# compute_diff — pools (add-only, never modify or remove)
# ---------------------------------------------------------------------------

def test_pool_added():
    r = reconciler()
    pool = {"name": "tank", "vdev_type": "mirror"}
    changes = r.compute_diff(
        {"storage": {"pools": [pool]}}, {"storage": {"pools": []}}
    )
    assert changes == [Change("storage.pools.tank", None, pool, "add")]


def test_pool_changes_are_never_emitted_as_modify_or_remove():
    r = reconciler()
    old = {"name": "tank", "vdev_type": "mirror"}
    new = {"name": "tank", "vdev_type": "raidz2"}
    # present in both desired and current: no add, and pools never modify/remove
    assert r.compute_diff(
        {"storage": {"pools": [new]}}, {"storage": {"pools": [old]}}
    ) == []
    # absent from desired: not a "remove" — pool removal is out of scope
    assert r.compute_diff(
        {"storage": {"pools": []}}, {"storage": {"pools": [old]}}
    ) == []


# ---------------------------------------------------------------------------
# compute_diff — SMB/NFS shares (add, modify AND remove, in every mode)
# ---------------------------------------------------------------------------

def test_smb_share_added_modified_and_removed():
    r = reconciler()
    media_old = {"name": "media", "path": "/tank/media"}
    media_new = {"name": "media", "path": "/tank/media2"}
    backup = {"name": "backup", "path": "/tank/backup"}

    added = r.compute_diff({"smb_shares": [backup]}, {"smb_shares": []})
    assert added == [Change("smb_shares.backup", None, backup, "add")]

    modified = r.compute_diff(
        {"smb_shares": [media_new]}, {"smb_shares": [media_old]}
    )
    assert modified == [Change("smb_shares.media", media_old, media_new, "modify")]

    removed = r.compute_diff({"smb_shares": []}, {"smb_shares": [media_old]})
    assert removed == [Change("smb_shares.media", media_old, None, "remove")]


def test_nfs_share_added_modified_and_removed():
    r = reconciler()
    home_old = {"name": "home", "path": "/tank/home", "clients": "*"}
    home_new = {"name": "home", "path": "/tank/home", "clients": "10.0.0.0/24"}

    added = r.compute_diff({"nfs_shares": [home_new]}, {"nfs_shares": []})
    assert added == [Change("nfs_shares.home", None, home_new, "add")]

    modified = r.compute_diff(
        {"nfs_shares": [home_new]}, {"nfs_shares": [home_old]}
    )
    assert modified == [Change("nfs_shares.home", home_old, home_new, "modify")]

    removed = r.compute_diff({"nfs_shares": []}, {"nfs_shares": [home_old]})
    assert removed == [Change("nfs_shares.home", home_old, None, "remove")]


# ---------------------------------------------------------------------------
# reconcile() dispatch
# ---------------------------------------------------------------------------

def test_disaster_recovery_uses_config_state_directly(tmp_path):
    engine = FakeEngine()
    config = FakeConfig(state={"system": {"hostname": "nas"}})
    r = reconciler(engine, config)

    results = r.reconcile(mode="disaster_recovery")

    assert action_names(results) == ["system.hostname_set"]


def test_hybrid_with_no_desired_path_diffs_config_against_itself():
    engine = FakeEngine()
    config = FakeConfig(state={"system": {"hostname": "nas"}})
    r = reconciler(engine, config)

    assert r.reconcile(mode="hybrid") == []


def test_hybrid_with_desired_path_applies_the_diff(tmp_path):
    desired_file = tmp_path / "desired.yaml"
    desired_file.write_text("system:\n  hostname: new-name\n")
    engine = FakeEngine()
    config = FakeConfig(state={"system": {"hostname": "old-name"}})
    r = reconciler(engine, config)

    results = r.reconcile(desired_path=str(desired_file), mode="hybrid")

    assert action_names(results) == ["system.hostname_set"]
    assert engine.calls == [("system", "hostname_set", {"hostname": "new-name"})]


# ---------------------------------------------------------------------------
# _apply_disaster_recovery — fixed order (spec §6.4)
# ---------------------------------------------------------------------------

def test_disaster_recovery_applies_in_fixed_order():
    state = {
        "system": {"hostname": "nas", "timezone": "UTC"},
        "storage": {"pools": [{"name": "tank"}]},
        "users": [{"username": "alice", "groups": []}],
        "installed_features": [{"id": "samba-cifs", "enabled": True}],
        "smb_shares": [{"name": "media", "path": "/tank/media"}],
        "nfs_shares": [{"name": "home", "path": "/tank/home"}],
    }
    engine = FakeEngine(service_names={("samba", "smb_service"): "smbd"})
    config = FakeConfig(state=state)
    r = reconciler(engine, config)

    results = r.reconcile(mode="disaster_recovery")

    assert action_names(results) == [
        "system.hostname_set",
        "system.timezone_set",
        "zfs.pool_import",
        "users.create",
        "services.enable",
        "services.start",
        "samba.create_share",
        "nfs.export_add",
    ]


def test_disaster_recovery_skips_disabled_features():
    state = {"installed_features": [{"id": "nfs", "enabled": False}]}
    r = reconciler(config=FakeConfig(state=state))
    assert r.reconcile(mode="disaster_recovery") == []


# ---------------------------------------------------------------------------
# Atomic actions — idempotence (safe to re-run)
# ---------------------------------------------------------------------------

def test_pool_already_imported_is_skipped_not_reimported():
    engine = FakeEngine(imported_pools=["tank"])
    r = reconciler(engine)

    result = r._do_pool_import({"name": "tank"})

    assert result.target_state == "skipped"
    assert engine.calls == [("zfs", "pool_list", {"check": False})]


def test_missing_pool_is_imported():
    engine = FakeEngine()
    r = reconciler(engine)

    result = r._do_pool_import({"name": "tank"})

    assert result.target_state == "applied"
    assert ("zfs", "pool_import", {"pool_name": "tank"}) in engine.calls


def test_existing_user_is_skipped_not_recreated():
    engine = FakeEngine(existing_users=["alice"])
    r = reconciler(engine)

    result = r._do_user_create({"username": "alice", "groups": []})

    assert result.target_state == "skipped"
    assert not any(c[1] == "create" for c in engine.calls)


def test_missing_user_is_created():
    engine = FakeEngine()
    r = reconciler(engine)

    result = r._do_user_create({"username": "alice", "groups": ["sudo"]})

    assert result.target_state == "applied"


def test_a_failed_action_is_reported_not_raised():
    engine = FakeEngine(failing={("system", "hostname_set")})
    r = reconciler(engine)

    result = r._do_hostname("nas")

    assert result.target_state == "failed"
    assert "failed" in result.error


# ---------------------------------------------------------------------------
# _do_user_modify — group membership reconciliation
# ---------------------------------------------------------------------------

def test_user_modify_adds_and_removes_groups():
    engine = FakeEngine()
    r = reconciler(engine)

    results = r._do_user_modify(
        {"username": "alice", "groups": ["sudo", "docker"]},
        {"username": "alice", "groups": ["sudo", "backup"]},
    )

    actions = {(res.action, res.params.get("group")) for res in results}
    assert ("users.add_to_group", "docker") in actions
    assert ("users.remove_from_group", "backup") in actions
    assert len(results) == 2


def test_user_modify_with_no_group_changes_is_skipped():
    engine = FakeEngine()
    r = reconciler(engine)

    results = r._do_user_modify(
        {"username": "alice", "groups": ["sudo"]},
        {"username": "alice", "groups": ["sudo"]},
    )

    assert len(results) == 1
    assert results[0].target_state == "skipped"


# ---------------------------------------------------------------------------
# NFS share deletion — looked up by path, not by name
# ---------------------------------------------------------------------------

def test_nfs_share_delete_looks_up_the_path_from_config():
    engine = FakeEngine()
    config = FakeConfig(nfs_shares=[{"name": "home", "path": "/tank/home"}])
    r = reconciler(engine, config)

    result = r._do_nfs_share_delete("home")

    assert result.target_state == "applied"
    assert ("nfs", "export_remove", {"path": "/tank/home"}) in engine.calls


def test_nfs_share_delete_skipped_when_path_unknown():
    engine = FakeEngine()
    config = FakeConfig(nfs_shares=[])
    r = reconciler(engine, config)

    result = r._do_nfs_share_delete("ghost")

    assert result.target_state == "skipped"
    assert engine.calls == []


# ---------------------------------------------------------------------------
# Service enablement — feature-to-service mapping
# ---------------------------------------------------------------------------

def test_service_enable_for_unmapped_feature_is_skipped():
    r = reconciler()
    results = r._do_service_enable("not-a-real-feature")
    assert len(results) == 1
    assert results[0].target_state == "skipped"


def test_service_enable_enables_then_starts():
    engine = FakeEngine(service_names={("nfs", "nfs_service"): "nfs-server"})
    r = reconciler(engine)

    results = r._do_service_enable("nfs")

    assert action_names(results) == ["services.enable", "services.start"]
    assert all(res.target_state == "applied" for res in results)
    assert all(res.params["service_name"] == "nfs-server" for res in results)


# ---------------------------------------------------------------------------
# _apply_changes — routing by path prefix and change type
# ---------------------------------------------------------------------------

def test_apply_changes_routes_every_change_type():
    engine = FakeEngine(
        imported_pools=[], existing_users=["bob"],
        service_names={},
    )
    changes = [
        Change("system.hostname", "old", "new", "modify"),
        Change("users.alice", None, {"username": "alice", "groups": []}, "add"),
        Change("users.bob", {"username": "bob", "groups": ["a"]},
               {"username": "bob", "groups": ["b"]}, "modify"),
        Change("users.carol", {"username": "carol"}, None, "remove"),
        Change("storage.pools.tank", None, {"name": "tank"}, "add"),
        Change("smb_shares.media", None, {"name": "media", "path": "/t"}, "add"),
        Change("smb_shares.old", {"name": "old", "path": "/o"}, None, "remove"),
        Change("nfs_shares.home", None, {"name": "home", "path": "/h"}, "add"),
    ]
    r = reconciler(engine)

    results = r._apply_changes(changes)

    assert action_names(results) == [
        "system.hostname_set",
        "users.create",
        "users.add_to_group",  # bob: "b" added
        "users.remove_from_group",  # bob: "a" removed
        "users.delete",
        "zfs.pool_import",
        "samba.create_share",
        "samba.delete_share",
        "nfs.export_add",
    ]
