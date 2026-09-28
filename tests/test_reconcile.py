"""
Behaviour of the ReconciliationEngine. White-box: it reaches into
`observe()`, `compute_diff()` and `_do_*` as well as `reconcile()`.
"""

import pytest

from reconcile import (
    Change,
    CommandExecutionError,
    CommandTranslationError,
    ReconcileMode,
    ReconciliationEngine,
    parse_mode,
)


ADMIN = {"username": "sarah", "groups": ["sudo"], "admin": True}


class FakeEngine:
    """
    A system with pools, accounts, usershares and exports. `outputs` holds
    what a command prints; anything listed in `failing` raises.
    """

    def __init__(self, imported_pools=(), users=None, smb_shares=(), exports_file=None,
                 hostname="nas", timezone="UTC", values=None, failing=()):
        self.imported_pools = list(imported_pools)
        self.users = dict(users or {})
        self.smb_shares = list(smb_shares)
        self.hostname = hostname
        self.timezone = timezone
        self.values = {("users", "sudo_group"): "sudo", **(values or {})}
        if exports_file is not None:
            self.values[("nfs", "config_file")] = str(exports_file)
        self.failing = set(failing)
        self.calls = []

    def execute(self, category, action, **kwargs):
        self.calls.append((category, action, kwargs))
        if (category, action) in self.failing:
            raise CommandExecutionError(f"{category}.{action} failed")
        key = (category, action)
        if key == ("zfs", "pool_list"):
            return "\n".join(f"{p}\t1\t2" for p in self.imported_pools), ""
        if key == ("network", "hostname_get"):
            return self.hostname + "\n", ""
        if key == ("system", "timezone_get"):
            return self.timezone + "\n", ""
        if key == ("users", "list"):
            return "\n".join(f"{u}:x:{1000 + i}:{1000 + i}::/home/{u}:/bin/bash"
                             for i, u in enumerate(self.users)), ""
        if key == ("users", "list_groups"):
            return " ".join(self.users.get(kwargs["username"], [])), ""
        if key == ("users", "get_user_info"):
            return (kwargs["username"] if kwargs["username"] in self.users else ""), ""
        if key == ("samba", "list_shares"):
            return "\n".join(self.smb_shares), ""
        return "", ""

    def get_profile_value(self, category, key):
        try:
            return self.values[(category, key)]
        except KeyError:
            raise CommandTranslationError(f"no such value: {category}.{key}")

    def actions(self):
        observing = {("zfs", "pool_list"), ("network", "hostname_get"), ("system", "timezone_get"),
                     ("users", "list"), ("users", "list_groups"), ("samba", "list_shares")}
        return [(c, a) for c, a, _ in self.calls if (c, a) not in observing]


class FakeConfig:
    def __init__(self, state=None):
        self.state = state or {}

    def to_dict(self):
        return self.state


def reconciler(engine=None, config=None):
    return ReconciliationEngine(engine or FakeEngine(), config or FakeConfig())


def paths(changes):
    return [(c.path, c.change_type) for c in changes]


def action_names(results):
    return [r.action for r in results]


# ---------------------------------------------------------------------------
# observe — what the system says, never config.yaml
# ---------------------------------------------------------------------------

def test_observe_reads_the_running_system(tmp_path):
    exports = tmp_path / "exports"
    exports.write_text("# comment\n/tank/foto 192.168.1.0/24(rw,sync)\n")
    engine = FakeEngine(imported_pools=["rpool", "tank"], users={"sarah": ["sarah", "sudo"]},
                        smb_shares=["media"], exports_file=exports, hostname="mynas")
    state = reconciler(engine).observe()
    assert state["system"] == {"hostname": "mynas", "timezone": "UTC"}
    assert state["storage"]["pools"] == [{"name": "rpool"}, {"name": "tank"}]
    assert state["users"] == [{"username": "sarah", "groups": ["sarah", "sudo"]}]
    assert state["smb_shares"] == [{"name": "media"}]
    assert state["nfs_shares"] == [{"path": "/tank/foto", "allowed_hosts": "192.168.1.0/24",
                                    "options": "rw,sync"}]


def test_what_cannot_be_read_is_unknown_not_empty():
    engine = FakeEngine(failing={("users", "list"), ("samba", "list_shares")})
    state = reconciler(engine).observe()
    assert state["users"] is None
    assert state["smb_shares"] is None
    assert state["nfs_shares"] is None


def test_root_is_never_among_the_observed_users():
    engine = FakeEngine(users={"root": ["root"], "mario": ["mario"]})
    assert [u["username"] for u in reconciler(engine).observe()["users"]] == ["mario"]


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------

def diff(desired, current=None, mode=ReconcileMode.DECLARATIVE, engine=None):
    base = {"system": {}, "storage": {"pools": []}, "users": [], "smb_shares": [], "nfs_shares": []}
    return reconciler(engine).compute_diff(desired, {**base, **(current or {})}, mode=mode)


def test_hostname_and_timezone_change_only_when_they_differ():
    desired = {"system": {"hostname": "nas", "timezone": "Europe/Rome"}}
    assert paths(diff(desired, {"system": {"hostname": "nas", "timezone": "UTC"}})) == [
        ("system.timezone", "modify")]
    assert diff(desired, {"system": {"hostname": "nas", "timezone": "Europe/Rome"}}) == []


def test_a_pool_is_imported_when_missing_and_never_removed():
    desired = {"storage": {"pools": [{"name": "tank"}]}}
    assert paths(diff(desired)) == [("storage.pools.tank", "add")]
    assert diff(desired, {"storage": {"pools": [{"name": "tank"}, {"name": "old"}]}}) == []


def test_a_missing_user_is_added():
    changes = diff({"users": [ADMIN]})
    assert paths(changes) == [("users.sarah", "add")]


def test_only_the_groups_freedomnas_assigns_are_compared():
    current = {"users": [{"username": "sarah", "groups": ["sarah", "users", "sudo"]}]}
    assert diff({"users": [ADMIN]}, current) == []
    current = {"users": [{"username": "sarah", "groups": ["sarah", "users"]}]}
    [change] = diff({"users": [ADMIN]}, current)
    assert (change.path, change.change_type) == ("users.sarah", "modify")
    assert change.old_value == {"username": "sarah", "groups": []}


def test_users_are_ensured_when_the_system_cannot_list_them():
    assert paths(diff({"users": [ADMIN]}, {"users": None})) == [("users.sarah", "ensure")]


def test_declarative_removes_an_account_the_configuration_no_longer_names():
    current = {"users": [{"username": "sarah", "groups": ["sudo"]},
                         {"username": "mario", "groups": ["mario"]}]}
    assert paths(diff({"users": [ADMIN]}, current)) == [("users.mario", "remove")]


def test_an_administrator_is_never_removed():
    current = {"users": [{"username": "sarah", "groups": ["sudo"]},
                         {"username": "admin", "groups": ["admin", "sudo"]}]}
    assert diff({"users": [ADMIN]}, current) == []


def test_additive_never_removes():
    current = {"users": [{"username": "sarah", "groups": ["sudo"]},
                         {"username": "mario", "groups": []}],
               "smb_shares": [{"name": "old"}]}
    changes = diff({"users": [ADMIN]}, current, mode=ReconcileMode.ADDITIVE)
    assert all(c.change_type != "remove" for c in changes)


def test_a_configuration_without_an_administrator_removes_nothing():
    current = {"users": [{"username": "mario", "groups": []}], "smb_shares": [{"name": "old"}]}
    assert [c for c in diff({"users": []}, current) if c.change_type == "remove"] == []


def test_smb_shares_not_in_the_configuration_are_removed_and_every_recorded_one_rewritten():
    desired = {"users": [ADMIN], "shares": {"smb": [{"name": "foto", "path": "/tank/foto"}]}}
    current = {"users": [{"username": "sarah", "groups": ["sudo"]}],
               "smb_shares": [{"name": "foto"}, {"name": "old"}]}
    assert paths(diff(desired, current)) == [("smb_shares.old", "remove"), ("smb_shares.foto", "ensure")]


def test_an_nfs_export_that_matches_is_left_alone():
    share = {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"}
    current = {"users": [{"username": "sarah", "groups": ["sudo"]}],
               "nfs_shares": [{"path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"}]}
    assert diff({"users": [ADMIN], "nfs_shares": [share]}, current) == []


def test_an_nfs_export_that_differs_is_replaced():
    share = {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"}
    current = {"users": [{"username": "sarah", "groups": ["sudo"]}],
               "nfs_shares": [{"path": "/tank/foto", "allowed_hosts": "*", "options": "rw"}]}
    assert paths(diff({"users": [ADMIN], "nfs_shares": [share]}, current)) == [
        ("nfs_shares./tank/foto", "remove"), ("nfs_shares.foto", "add")]


def test_an_export_in_another_syntax_is_known_by_its_path(tmp_path):
    exports = tmp_path / "exports"
    exports.write_text("/tank/foto -ro -network 10.0.0.0/8\n/tank/old -ro\n")
    engine = FakeEngine(users={"sarah": ["sudo"]}, exports_file=exports)
    share = {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"}
    config = FakeConfig({"users": [ADMIN], "nfs_shares": [share]})
    assert paths(reconciler(engine, config).plan()) == [("nfs_shares./tank/old", "remove")]


def test_additive_leaves_a_differing_nfs_export_alone():
    share = {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"}
    current = {"nfs_shares": [{"path": "/tank/foto", "allowed_hosts": "*", "options": "rw"}]}
    assert diff({"nfs_shares": [share]}, current, mode=ReconcileMode.ADDITIVE) == []


def test_the_plan_follows_the_fixed_order():
    desired = {
        "system": {"hostname": "nas", "timezone": "Europe/Rome", "ssh_enabled": False},
        "storage": {"pools": [{"name": "tank"}]},
        "users": [ADMIN],
        "installed_features": [{"id": "samba-cifs", "enabled": True}, {"id": "nfs", "enabled": False}],
        "smb_shares": [{"name": "media", "path": "/tank/media"}],
        "nfs_shares": [{"name": "home", "path": "/tank/home", "allowed_hosts": "*"}],
    }
    current = {"system": {"hostname": "old", "timezone": "UTC"},
               "users": [{"username": "mario", "groups": []}]}
    assert [p for p, _ in paths(diff(desired, current))] == [
        "system.hostname",
        "system.timezone",
        "storage.pools.tank",
        "users.sarah",
        "features.samba-cifs",
        "system.ssh",
        "smb_shares.media",
        "nfs_shares.home",
        "users.mario",
    ]


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------

def test_disaster_recovery_is_the_old_name_of_declarative():
    assert parse_mode("disaster_recovery") is ReconcileMode.DECLARATIVE
    with pytest.raises(ValueError):
        parse_mode("hybrid")


def test_reconcile_reads_config_and_applies_what_differs():
    engine = FakeEngine(users={"sarah": ["sudo"]}, hostname="old")
    config = FakeConfig({"system": {"hostname": "nas"}, "users": [ADMIN]})
    results = reconciler(engine, config).reconcile()
    assert action_names(results) == ["system.hostname_set"]


def test_a_dry_run_plans_without_applying():
    engine = FakeEngine(users={"sarah": ["sudo"], "mario": []}, hostname="old")
    config = FakeConfig({"system": {"hostname": "nas"}, "users": [ADMIN]})
    results = reconciler(engine, config).reconcile(dry_run=True)
    assert [(r.action, r.target_state) for r in results] == [
        ("system.hostname", "planned"), ("users.mario", "planned")]
    assert engine.actions() == []


def test_a_desired_file_is_used_instead_of_config(tmp_path):
    desired = tmp_path / "desired.yaml"
    desired.write_text("system:\n  hostname: from-file\n")
    engine = FakeEngine()
    reconciler(engine, FakeConfig({"system": {"hostname": "from-config"}})).reconcile(
        desired_path=str(desired), mode="additive")
    [(_, _, kwargs)] = [c for c in engine.calls if c[:2] == ("system", "hostname_set")]
    assert kwargs["hostname"] == "from-file"


# ---------------------------------------------------------------------------
# _do_* — the engine calls
# ---------------------------------------------------------------------------

def test_a_removed_account_keeps_its_home_and_leaves_samba_first():
    engine = FakeEngine()
    result = reconciler(engine)._do_user_delete("mario")
    assert result.target_state == "applied"
    assert engine.actions() == [("samba", "check_installed"), ("samba", "delete_user"),
                                ("users", "delete_keep_home")]


def test_without_samba_an_account_is_removed_all_the_same():
    engine = FakeEngine(failing={("samba", "check_installed")})
    assert reconciler(engine)._do_user_delete("mario").target_state == "applied"
    assert ("samba", "delete_user") not in engine.actions()


def test_existing_user_is_skipped_not_recreated():
    engine = FakeEngine(users={"alice": []})
    result = reconciler(engine)._do_user_create({"username": "alice", "groups": []})
    assert result.target_state == "skipped"
    assert ("users", "create") not in engine.actions()


def test_user_modify_adds_and_removes_groups():
    engine = FakeEngine()
    results = reconciler(engine)._do_user_modify(
        {"username": "alice", "groups": ["sudo", "sambashare"]},
        {"username": "alice", "groups": ["sudo", "docker"]},
    )
    assert [(r.action, r.params["group"]) for r in results] == [
        ("users.add_to_group", "sambashare"), ("users.remove_from_group", "docker")]


@pytest.mark.parametrize("share, acl", [
    ({"name": "foto", "path": "/tank/foto", "acl": "Everyone:R,sarah:F,mario:R"},
     "Everyone:R,sarah:F,mario:R"),
    ({"name": "foto", "path": "/tank/foto", "valid_users": ["sarah"]}, "Everyone:R,sarah:F"),
    ({"name": "foto", "path": "/tank/foto"}, "Everyone:R"),
    ({"name": "foto", "path": "/tank/foto", "valid_users": ["sarah"], "read_only": True},
     "Everyone:R"),
])
def test_a_restored_share_never_gets_more_access_than_it_had(share, acl):
    engine = FakeEngine()
    reconciler(engine)._do_smb_share_create(share)
    [created] = [kw for cat, act, kw in engine.calls if (cat, act) == ("samba", "create_share")]
    assert created["acl"] == acl


def test_an_nfs_export_goes_to_its_recorded_hosts():
    engine = FakeEngine()
    reconciler(engine)._do_nfs_share_create(
        {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/8", "options": "ro"})
    [kwargs] = [kw for cat, act, kw in engine.calls if (cat, act) == ("nfs", "export_add")]
    assert kwargs == {"path": "/tank/foto", "clients": "10.0.0.0/8", "options": "ro"}


def test_an_nfs_export_without_hosts_is_not_opened_to_everyone():
    engine = FakeEngine()
    result = reconciler(engine)._do_nfs_share_create({"name": "foto", "path": "/tank/foto"})
    assert result.target_state == "skipped"
    assert ("nfs", "export_add") not in engine.actions()


def test_ssh_is_turned_on_or_off_as_recorded():
    engine = FakeEngine(values={("system", "ssh_service"): "sshd"})
    assert action_names(reconciler(engine)._do_ssh(True)) == ["services.enable", "services.start"]
    assert action_names(reconciler(engine)._do_ssh(False)) == ["services.stop", "services.disable"]


def test_service_enable_for_unmapped_feature_is_skipped():
    [result] = reconciler()._do_service_enable("docker")
    assert result.target_state == "skipped"


def test_service_enable_enables_then_starts():
    engine = FakeEngine(values={("samba", "smb_service"): "smbd"})
    results = reconciler(engine)._do_service_enable("samba-cifs")
    assert [(r.action, r.params["service_name"]) for r in results] == [
        ("services.enable", "smbd"), ("services.start", "smbd")]


def test_a_failed_action_is_reported_not_raised():
    engine = FakeEngine(failing={("system", "hostname_set")})
    result = reconciler(engine)._do_hostname("nas")
    assert result.target_state == "failed"
    assert "hostname_set failed" in result.error


def test_apply_routes_every_change_type():
    engine = FakeEngine(values={("system", "ssh_service"): "sshd", ("samba", "smb_service"): "smbd"})
    changes = [
        Change("system.hostname", None, "nas", "add"),
        Change("system.timezone", None, "UTC", "add"),
        Change("storage.pools.tank", None, {"name": "tank"}, "add"),
        Change("users.alice", None, {"username": "alice", "groups": []}, "add"),
        Change("features.samba-cifs", None, {"id": "samba-cifs", "enabled": True}, "ensure"),
        Change("system.ssh", None, False, "ensure"),
        Change("smb_shares.old", {"name": "old"}, None, "remove"),
        Change("smb_shares.media", None, {"name": "media", "path": "/t"}, "ensure"),
        Change("nfs_shares./o", {"path": "/o"}, None, "remove"),
        Change("nfs_shares.home", None, {"name": "home", "path": "/h", "allowed_hosts": "*"}, "add"),
        Change("users.mario", {"username": "mario"}, None, "remove"),
    ]
    results = reconciler(engine)._apply_changes(changes)
    assert action_names(results) == [
        "system.hostname_set",
        "system.timezone_set",
        "zfs.pool_import",
        "users.create",
        "services.enable",
        "services.start",
        "services.stop",
        "services.disable",
        "samba.delete_share",
        "samba.create_share",
        "nfs.export_remove",
        "nfs.export_add",
        "users.delete_keep_home",
    ]
