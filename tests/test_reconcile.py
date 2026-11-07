"""
Behaviour of the ReconciliationEngine. White-box: it reaches into
`observe()`, `compute_diff()` and `_do_*` as well as `reconcile()`.
"""

from pathlib import Path

import pytest
import yaml

import reconcile
from reconcile import (
    ActionResult,
    Change,
    CommandExecutionError,
    CommandTranslationError,
    ReconcileMode,
    ReconciliationEngine,
    ModuleLoaderException,
    parse_mode,
)


ADMIN = {"username": "sarah", "groups": ["sudo"], "admin": True}


class FakeEngine:
    """
    A system with pools, accounts, usershares and exports. `outputs` holds
    what a command prints; anything listed in `failing` raises.
    """

    def __init__(self, imported_pools=(), users=None, smb_shares=(), exports_file=None,
                 hostname="nas", timezone="UTC", values=None, failing=(),
                 taken_uids=(), owners=None, groups=None, acltypes="", services=()):
        self.imported_pools = list(imported_pools)
        self.taken_uids = {str(u) for u in taken_uids}
        self.owners = dict(owners or {})
        self.groups = dict(groups or {})
        self.acltypes = acltypes
        self.services = list(services)
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
            name = kwargs["username"]
            if name in self.taken_uids:
                return f"other{name}:x:{name}:{name}::/home/other:/bin/sh\n", ""
            return (name if name in self.users else ""), ""
        if key == ("users", "uid_set"):
            self.taken_uids = {u for u in self.taken_uids if u != kwargs["username"].removeprefix("other")}
            self.taken_uids.add(kwargs["uid"])
            return "", ""
        if key == ("directories", "owner_uid_get"):
            return self.owners.get(kwargs["path"], ""), ""
        if key == ("zfs", "acltype_list"):
            return self.acltypes, ""
        if key == ("directories", "group_get"):
            return self.groups.get(kwargs["path"], ""), ""
        if key == ("samba", "list_shares"):
            return "\n".join(self.smb_shares), ""
        if key == ("services", "list_all"):
            return "\n".join(self.services), ""
        return "", ""

    def get_profile_value(self, category, key):
        try:
            return self.values[(category, key)]
        except KeyError:
            raise CommandTranslationError(f"no such value: {category}.{key}")

    def actions(self):
        observing = {("zfs", "pool_list"), ("network", "hostname_get"), ("system", "timezone_get"),
                     ("users", "list"), ("users", "list_groups"), ("samba", "list_shares"),
                     ("network", "list_interfaces")}
        return [(c, a) for c, a, _ in self.calls if (c, a) not in observing]


class FakeConfig:
    def __init__(self, state=None, pending=False):
        self.state = state or {}
        self.pending = pending

    def setup_pending(self):
        return self.pending

    def get_service_boot(self):
        return dict((self.state.get("services") or {}).get("boot") or {})

    def to_dict(self):
        return self.state

    def get(self, key, default=None):
        value = self.state
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return default if value is None else value

    def set(self, key, value):
        *parents, last = key.split(".")
        node = self.state
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value

    def get_interface(self, name):
        return next((i for i in self.get("network.interfaces", []) if i.get("logical_name") == name), None)

    def add_interface(self, entry):
        self.remove_interface(entry["logical_name"])
        self.state.setdefault("network", {}).setdefault("interfaces", []).append(entry)

    def remove_interface(self, name):
        network = self.state.setdefault("network", {})
        network["interfaces"] = [i for i in network.get("interfaces", []) if i.get("logical_name") != name]


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

def diff(desired, current=None, mode=ReconcileMode.DECLARATIVE, engine=None, config=None):
    base = {"system": {}, "storage": {"pools": []}, "users": [], "smb_shares": [], "nfs_shares": []}
    return reconciler(engine, config).compute_diff(desired, {**base, **(current or {})}, mode=mode)


def test_hostname_and_timezone_change_only_when_they_differ():
    desired = {"system": {"hostname": "nas", "timezone": "Europe/Rome"}}
    assert paths(diff(desired, {"system": {"hostname": "nas", "timezone": "UTC"}})) == [
        ("system.timezone", "modify")]
    assert diff(desired, {"system": {"hostname": "nas", "timezone": "Europe/Rome"}}) == []


def test_a_pool_is_imported_when_missing_and_never_removed():
    desired = {"storage": {"pools": [{"name": "tank"}]}}
    assert paths(diff(desired)) == [("storage.pools.tank", "add")]
    assert paths(diff(desired, {"storage": {"pools": [{"name": "tank"}, {"name": "old"}]}})) == [
        ("storage.pools.tank", "ensure")]


def test_an_imported_pool_is_cached_and_mounted():
    """firstboot leaves a restored data pool imported with -N and no cachefile."""
    engine = FakeEngine()
    results = reconciler(engine)._apply_changes(
        [Change("storage.pools.tank", None, {"name": "tank"}, "ensure")])
    assert action_names(results) == ["zfs.pool_cachefile_default", "zfs.mount_pool_auto"]
    assert all(r.params == {"pool_name": "tank"} for r in results)


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


def test_an_administrator_written_on_debian_keeps_the_running_editions_group():
    """2026-10-14: a config.yaml written by Debian named `sudo`; on Gentoo the
    reconciler took `wheel` away from its own administrator."""
    engine = FakeEngine(values={("users", "sudo_group"): "wheel"})
    desired = {"users": [{"username": "sarah", "groups": ["sudo", "docker"], "admin": True}]}
    current = {"users": [{"username": "sarah", "groups": ["sarah", "wheel", "docker"]}]}
    assert diff(desired, current, engine=engine) == []


def test_an_administrator_by_flag_alone_joins_the_running_editions_group():
    engine = FakeEngine(values={("users", "sudo_group"): "wheel"})
    desired = {"users": [{"username": "sarah", "groups": [], "admin": True}]}
    current = {"users": [{"username": "sarah", "groups": ["sarah"]}]}
    [change] = diff(desired, current, engine=engine)
    assert (change.path, change.change_type) == ("users.sarah", "modify")
    assert "wheel" in change.new_value["groups"]


def test_the_administrators_group_is_never_taken_away():
    current = {"users": [{"username": "sarah", "groups": ["sudo"]},
                         {"username": "mario", "groups": ["mario", "sudo"]}]}
    desired = {"users": [ADMIN, {"username": "mario", "groups": []}]}
    assert diff(desired, current) == []


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


class PasswdEngine(FakeEngine):
    """Accounts answer get_user_info with their passwd line, as getent does."""

    def execute(self, category, action, **kwargs):
        if (category, action) == ("users", "get_user_info") and kwargs["username"] in self.users:
            self.calls.append((category, action, kwargs))
            return f"{kwargs['username']}:x:1001:1001::/home/{kwargs['username']}:/bin/sh", ""
        return super().execute(category, action, **kwargs)


OWNER_OPTIONS = {("nfs", "owner_options"): ",all_squash,anonuid={uid},anongid={gid}"}
ANNA_SHARE = {"name": "anna", "path": "/tank/anna", "allowed_hosts": "10.0.0.5",
              "options": "rw", "owner": "anna", "map_to_owner": True}


def test_an_export_mapped_onto_its_owner_matches_its_line():
    engine = PasswdEngine(users={"sarah": ["sudo"], "anna": []}, values=OWNER_OPTIONS)
    current = {"users": [{"username": "sarah", "groups": ["sudo"]}],
               "nfs_shares": [{"path": "/tank/anna", "allowed_hosts": "10.0.0.5",
                               "options": "rw,all_squash,anonuid=1001,anongid=1001"}]}
    assert diff({"users": [ADMIN], "nfs_shares": [ANNA_SHARE]}, current, engine=engine) == []


def test_an_export_that_lost_its_mapping_is_written_again():
    engine = PasswdEngine(users={"sarah": ["sudo"], "anna": []}, values=OWNER_OPTIONS)
    current = {"users": [{"username": "sarah", "groups": ["sudo"]}],
               "nfs_shares": [{"path": "/tank/anna", "allowed_hosts": "10.0.0.5", "options": "rw"}]}
    assert paths(diff({"users": [ADMIN], "nfs_shares": [ANNA_SHARE]}, current, engine=engine)) == [
        ("nfs_shares./tank/anna", "remove"), ("nfs_shares.anna", "add")]


def test_the_nfsv4_root_line_is_not_an_export(tmp_path):
    exports = tmp_path / "exports"
    exports.write_text("# comment\nV4: /\n/tank/foto -mapall=anna -network 10.0.0.0/24\n")
    engine = FakeEngine(users={"sarah": ["sudo"]}, exports_file=exports)
    share = {"name": "foto", "path": "/tank/foto", "allowed_hosts": "10.0.0.0/24"}
    config = FakeConfig({"users": [ADMIN], "nfs_shares": [share]})
    assert paths(reconciler(engine, config).plan()) == []


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


@pytest.mark.parametrize("answer, state", [("", "warning"), ("wheel:*:0:root", "failed")])
def test_only_a_group_this_system_does_not_have_is_a_warning(answer, state):
    engine = FakeEngine(failing={("users", "add_to_group")})
    execute = engine.execute
    engine.os_type = "freebsd"
    engine.execute = lambda category, action, **kw: (
        (answer, "") if action == "group_exists" else execute(category, action, **kw))
    [result] = reconciler(engine)._do_user_modify(
        {"username": "alice", "groups": ["wheel"]}, {"username": "alice", "groups": []})
    assert (result.action, result.target_state) == ("users.add_to_group", state)
    assert ("this freebsd system has no such group" in result.error) == (state == "warning")


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


def test_ssh_stays_off_until_a_restored_image_is_set_up():
    desired = {"system": {"ssh_enabled": True}}
    [ssh] = diff(desired)
    assert ssh.new_value is True
    [ssh] = diff(desired, config=FakeConfig(pending=True))
    assert (ssh.path, ssh.new_value) == ("system.ssh", False)


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


def test_a_service_disabled_at_boot_is_not_turned_back_on_for_its_feature():
    engine = FakeEngine(values={("samba", "smb_service"): "smbd"})
    config = FakeConfig({"services": {"boot": {"smbd": False}}})
    [result] = reconciler(engine, config)._do_service_enable("samba-cifs")
    assert result.target_state == "skipped"
    assert engine.actions() == []


def test_the_boot_choices_of_the_webui_are_planned():
    changes = diff({"services": {"boot": {"nfs-server": False, "agetty.tty1": True}}})
    boot = [c for c in changes if c.path.startswith("services.boot.")]
    assert [(c.new_value["name"], c.new_value["enabled"]) for c in boot] == [
        ("nfs-server", False), ("agetty.tty1", True)]


def test_a_service_disabled_at_boot_is_stopped_and_disabled():
    engine = FakeEngine(services=["sshd", "nfs-server"])
    results = reconciler(engine)._do_service_boot("nfs-server", False)
    assert [(r.action, r.target_state) for r in results] == [
        ("services.stop", "applied"), ("services.disable", "applied")]


def test_a_service_enabled_at_boot_is_enabled_and_started():
    engine = FakeEngine(services=["sshd", "nfs-server"])
    results = reconciler(engine)._do_service_boot("nfs-server", True)
    assert [r.action for r in results] == ["services.enable", "services.start"]


def test_a_boot_choice_for_a_service_this_system_lacks_is_skipped():
    engine = FakeEngine(services=["sshd"])
    [result] = reconciler(engine)._do_service_boot("cronie", True)
    assert result.target_state == "skipped"
    assert ("services", "enable") not in engine.actions()


def test_a_failed_action_is_reported_not_raised():
    engine = FakeEngine(failing={("system", "hostname_set")})
    result = reconciler(engine)._do_hostname("nas")
    assert result.target_state == "failed"
    assert "hostname_set failed" in result.error


def test_apply_routes_every_change_type(tmp_path):
    engine = FakeEngine(values={("system", "ssh_service"): "sshd", ("samba", "smb_service"): "smbd",
                                ("samba", "config_file"): str(tmp_path / "smb.conf"),
                                ("samba", "usershare_path"): "/var/lib/samba/usershares"})
    changes = [
        Change("system.hostname", None, "nas", "add"),
        Change("system.timezone", None, "UTC", "add"),
        Change("storage.pools.tank", None, {"name": "tank"}, "add"),
        Change("storage.pools.data", None, {"name": "data"}, "ensure"),
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
        "zfs.pool_cachefile_default",
        "zfs.mount_pool_auto",
        "users.create",
        "services.enable",
        "services.start",
        "services.stop",
        "services.disable",
        "samba.delete_share",
        "samba.usershare_prepare",
        "samba.create_share",
        "nfs.export_remove",
        "nfs.export_add",
        "users.delete_keep_home",
    ]


def test_usershares_are_prepared_once_before_the_first_share(tmp_path):
    """A restored configuration's shares reach a system no wizard has prepared."""
    smb_conf = tmp_path / "smb.conf"
    smb_conf.write_text("[global]\n   workgroup = HOME\n")
    engine = FakeEngine(values={("samba", "config_file"): str(smb_conf),
                                ("samba", "usershare_path"): "/var/lib/samba/usershares",
                                ("samba", "smb_service"): "smb"})
    results = reconciler(engine)._apply_changes([
        Change("smb_shares.a", None, {"name": "a", "path": "/tank/a"}, "ensure"),
        Change("smb_shares.b", None, {"name": "b", "path": "/tank/b"}, "ensure"),
    ])
    assert action_names(results) == ["samba.usershare_prepare", "samba.create_share", "samba.create_share"]
    assert "usershare path = /var/lib/samba/usershares" in smb_conf.read_text()


def _created(engine):
    return [(a, kw.get("username"), kw.get("uid")) for c, a, kw in engine.calls
            if c == "users" and a.startswith("create")]


def test_a_recorded_uid_is_kept():
    engine = FakeEngine()
    reconciler(engine)._apply_changes([Change("users.luca", None, {"username": "luca", "uid": 1003}, "add")])
    assert _created(engine) == [("create_with_uid", "luca", "1003")]


def test_without_a_recorded_uid_the_owned_share_tells_it():
    """A configuration from before uids were recorded: the restored files carry the old ID."""
    desired = {"users": [{"username": "luca"}, {"username": "anna"}, {"username": "mario"}],
               "smb_shares": [{"name": "documenti", "path": "/tank/documenti", "owner": "luca"}],
               "nfs_shares": [{"name": "foto", "path": "/tank/foto", "owner": "anna", "allowed_hosts": "*"}]}
    engine = FakeEngine(owners={"/tank/documenti": "1003\n", "/tank/foto": "1004\n"},
                        values={("nfs", "default_options"): "rw"})
    rec = reconciler(engine)
    changes = [c for c in rec.compute_diff(desired, {"system": {}, "storage": {"pools": []}, "users": [],
                                                    "smb_shares": [], "nfs_shares": []})
               if c.path.startswith("users.")]
    rec._apply_changes(changes)
    assert _created(engine) == [("create_with_uid", "luca", "1003"), ("create_with_uid", "anna", "1004"),
                                ("create", "mario", "")]


def test_a_root_owned_share_says_nothing_about_its_owner():
    engine = FakeEngine(owners={"/tank/a": "0\n"})
    rec = reconciler(engine)
    rec._owned_paths = {"luca": ["/tank/a"]}
    rec._apply_changes([Change("users.luca", None, {"username": "luca"}, "add")])
    assert _created(engine) == [("create", "luca", "")]


def test_the_account_holding_a_restored_users_uid_is_moved_to_a_free_one():
    engine = FakeEngine(taken_uids=[1003])
    results = reconciler(engine)._apply_changes(
        [Change("users.luca", None, {"username": "luca", "uid": 1003}, "add")])
    assert _created(engine) == [("create_with_uid", "luca", "1003")]
    assert [(a, kw) for c, a, kw in engine.calls if a == "uid_set"] == [
        ("uid_set", {"username": "other1003", "uid": "1000"})]
    assert results[0].target_state == "applied"


def test_a_uid_the_system_holds_is_not_reused():
    engine = FakeEngine(taken_uids=[998])
    reconciler(engine)._apply_changes([Change("users.luca", None, {"username": "luca", "uid": 998}, "add")])
    assert _created(engine) == [("create", "luca", "")]


LINUX_GROUP = {("directories", "owner_group"): "{owner}", ("samba", "config_file"): "/dev/null",
               ("samba", "usershare_path"): "/var/lib/samba/usershares", ("samba", "smb_service"): "smb",
               ("nfs", "default_options"): "rw"}


def _regrouped(engine):
    return [(kw["path"], kw["group"]) for c, a, kw in engine.calls if a == "group_set_recursive"]


def test_a_restored_share_takes_the_editions_group_once(tmp_path):
    """FreeBSD left its files in wheel (gid 0); on Linux they go to the owner's group."""
    values = {**LINUX_GROUP, ("samba", "config_file"): str(tmp_path / "smb.conf")}
    engine = FakeEngine(values=values, groups={"/tank/documenti": "root\n", "/tank/foto": "anna\n"})
    reconciler(engine)._apply_changes([
        Change("smb_shares.documenti", None, {"name": "documenti", "path": "/tank/documenti", "owner": "luca"}, "ensure"),
        Change("nfs_shares.documenti", None, {"name": "documenti", "path": "/tank/documenti", "owner": "luca",
                                              "allowed_hosts": "*"}, "add"),
        Change("smb_shares.foto", None, {"name": "foto", "path": "/tank/foto", "owner": "anna"}, "ensure"),
        Change("smb_shares.media", None, {"name": "media", "path": "/tank/media"}, "ensure"),
    ])
    assert _regrouped(engine) == [("/tank/documenti", "luca")]


def test_on_freebsd_the_group_is_wheel(tmp_path):
    values = {**LINUX_GROUP, ("directories", "owner_group"): "wheel",
              ("samba", "config_file"): str(tmp_path / "smb.conf")}
    engine = FakeEngine(values=values, groups={"/tank/documenti": "luca\n"})
    reconciler(engine)._apply_changes([
        Change("smb_shares.documenti", None, {"name": "documenti", "path": "/tank/documenti", "owner": "luca"}, "ensure")])
    assert _regrouped(engine) == [("/tank/documenti", "wheel")]


def _acl_sets(engine):
    return [(kw["dataset"], kw["value"]) for c, a, kw in engine.calls
            if a == "dataset_set" and kw.get("property") == "acltype"]


def test_a_pool_from_freebsd_gets_posix_acls_on_linux():
    """Only the root and what is set locally change; inherited datasets follow."""
    engine = FakeEngine(values={("zfs", "pool_acltype"): "posix"}, acltypes=(
        "tank\tnfsv4\tlocal\n"
        "tank/documenti\tnfsv4\tinherited from tank\n"
        "tank/foto\tnfsv4\tlocal\n"
        "tank/media\tposix\tlocal\n"))
    reconciler(engine)._apply_changes([Change("storage.pools.tank", None, {"name": "tank"}, "ensure")])
    assert _acl_sets(engine) == [("tank", "posix"), ("tank/foto", "posix")]


def test_a_pool_already_right_is_left_alone():
    engine = FakeEngine(values={("zfs", "pool_acltype"): "nfsv4"},
                        acltypes="tank\tnfsv4\tlocal\ntank/a\tnfsv4\tinherited from tank\n")
    reconciler(engine)._apply_changes([Change("storage.pools.tank", None, {"name": "tank"}, "add")])
    assert _acl_sets(engine) == []


def test_a_pool_that_failed_to_import_is_not_touched():
    engine = FakeEngine(values={("zfs", "pool_acltype"): "posix"}, failing={("zfs", "pool_import")},
                        acltypes="tank\tnfsv4\tlocal\n")
    reconciler(engine)._apply_changes([Change("storage.pools.tank", None, {"name": "tank"}, "add")])
    assert _acl_sets(engine) == []


# ---------------------------------------------------------------------------
# network interfaces
# ---------------------------------------------------------------------------

LAN = {"logical_name": "eth0", "config": {"method": "static", "ip": "192.168.1.5", "cidr": 24,
                                          "gateway": "192.168.1.1", "dns": ["1.1.1.1"]}}


def test_observe_lists_interface_names():
    engine = FakeEngine(values={})
    engine.execute = lambda c, a, **k: (
        ('[{"ifname": "lo"}, {"ifname": "eth0"}]', "") if (c, a) == ("network", "list_interfaces")
        else ("", ""))
    assert reconciler(engine).observe()["network"]["links"] == ["lo", "eth0"]


def test_configured_interfaces_are_ensured_together():
    desired = {"network": {"interfaces": [LAN, {"logical_name": "eth1"}]}}
    [change] = diff(desired, {"network": {"links": ["lo", "eth0"]}})
    assert (change.path, change.change_type, change.new_value) == ("network.interfaces", "ensure", [LAN])
    assert diff(desired, {"network": {"links": None}}) == []


def test_a_configured_interface_is_left_alone():
    engine = FakeEngine()
    engine.execute = lambda c, a, **k: (engine.calls.append((c, a, k)) or
                                        {"connection_exists": "yes\n",
                                         "list_interfaces": '[{"ifname": "eth0"}]'}.get(a, ""), "")
    [result] = reconciler(engine)._do_network([LAN])
    assert result.target_state == "skipped"
    assert [a for _, a, _ in engine.calls] == ["list_physical", "list_physical", "list_interfaces", "connection_exists"]


def _with_links(engine, links):
    plain = engine.execute
    engine.execute = lambda c, a, **k: (
        (engine.calls.append((c, a, k)) or (links, "")) if (c, a) == ("network", "list_interfaces")
        else plain(c, a, **k))
    return engine


def test_an_unconfigured_interface_gets_its_configuration_back():
    engine = _with_links(FakeEngine(), '[{"ifname": "eth0"}]')
    [result] = reconciler(engine)._do_network([LAN])
    assert result.target_state == "applied"
    assert [a for c, a, _ in engine.calls
            if c == "network" and a not in ("list_physical", "list_interfaces")] == [
        "connection_exists", "connection_ensure", "ipv4_static", "ipv6_auto", "dns_set",
        "mtu_auto", "routes_set", "connection_activate"]


def test_a_failing_interface_is_reported():
    engine = _with_links(FakeEngine(failing={("network", "connection_activate")}), '[{"ifname": "eth0"}]')
    [result] = reconciler(engine)._do_network([LAN])
    assert result.target_state == "failed" and "connection_activate" in result.error


def test_an_interface_not_on_this_system_is_skipped():
    engine = _with_links(FakeEngine(), '[{"ifname": "eth1"}]')
    [result] = reconciler(engine)._do_network([LAN])
    assert result.target_state == "skipped" and result.error == "not on this system"


def test_an_entry_is_found_by_its_mac_under_its_new_name():
    engine = _with_links(FakeEngine(values={}), '[{"ifname": "fnas_lan1"}]')
    plain = engine.execute
    engine.execute = lambda c, a, **k: (("fnas_lan1 52:54:00:12:34:56 fnas_lan1\n", "")
                                        if a == "list_physical" else plain(c, a, **k))
    [result] = reconciler(engine)._do_network([dict(LAN, mac_address="52:54:00:12:34:56")])
    assert result.params == {"interface": "fnas_lan1"} and result.target_state == "applied"


def test_a_renamed_configured_interface_is_activated_again():
    engine = FakeEngine()
    engine.execute = lambda c, a, **k: (engine.calls.append((c, a, k)) or
                                        {"connection_exists": "yes\n"}.get(a, ""), "")
    result = reconciler(engine)._do_interface("fnas_lan2", LAN["config"], renamed=True)
    assert result.target_state == "applied"
    assert [a for _, a, _ in engine.calls] == ["connection_exists", "connection_activate"]


BRIDGE = {"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan1"], "stp": False,
          "mac_address": "52:54:00:12:34:56", "config": {"method": "dhcp"}}
PORT = {"logical_name": "fnas_lan1", "master": "fnas_bridge1", "mac_address": "52:54:00:12:34:56"}


def test_a_missing_bridge_is_created_with_its_ports():
    engine = _with_links(FakeEngine(), '[{"ifname": "fnas_lan1"}]')
    results = reconciler(engine)._do_network([BRIDGE, PORT])
    assert [(r.action, r.target_state) for r in results] == [("network.virtual", "applied")]
    actions = [a for c, a, _ in engine.calls if c == "network" and a not in ("list_physical", "list_interfaces")]
    assert actions[:2] == ["connection_exists", "bridge_create"]
    assert "bridge_port_add" in actions and actions[-1] == "virtual_activate"


def test_bonds_and_vlans_come_before_bridges():
    bond = {"logical_name": "fnas_bond1", "type": "bond", "ports": ["fnas_lan2", "fnas_lan3"], "mode": "lacp",
            "config": {}}
    vlan = {"logical_name": "fnas_vlan10", "type": "vlan", "parent": "fnas_bond1", "vlan_id": 10, "config": {}}
    engine = _with_links(FakeEngine(), '[{"ifname": "fnas_lan1"}, {"ifname": "fnas_lan2"}, {"ifname": "fnas_lan3"}]')
    results = reconciler(engine)._do_network([BRIDGE, vlan, bond])
    assert [r.params["interface"] for r in results] == ["fnas_bond1", "fnas_vlan10", "fnas_bridge1"]
    assert all(r.target_state == "applied" for r in results)


def test_a_bridge_whose_port_is_missing_is_skipped():
    engine = _with_links(FakeEngine(), '[{"ifname": "fnas_lan2"}]')
    [result] = reconciler(engine)._do_network([BRIDGE])
    assert result.target_state == "skipped" and "fnas_lan1" in result.error


def _configured(engine):
    plain = engine.execute
    engine.execute = lambda c, a, **k: (
        (engine.calls.append((c, a, k)) or ("yes\n", "")) if a == "connection_exists" else plain(c, a, **k))
    return engine


def _owned(owner_uid, user_line):
    engine = FakeEngine()
    plain = engine.execute
    answers = {"group_get": "1001\n", "owner_uid_get": owner_uid + "\n", "get_user_info": user_line}
    engine.execute = lambda c, a, **k: (
        (engine.calls.append((c, a, k)) or (answers[a], "")) if a in answers else plain(c, a, **k))
    engine.get_profile_value = lambda c, k: "{owner}"
    return engine


def test_a_share_owned_by_another_number_keeps_its_group():
    engine = _owned("1003", "luca:x:1001:1001::/home/luca:/bin/bash\n")
    [result] = reconciler(engine)._do_owner_group({"path": "/tank/documenti", "owner": "luca"})
    assert result.target_state == "skipped" and "uid 1003" in result.error
    assert "group_set_recursive" not in [a for _, a, _ in engine.calls]


def test_a_share_owned_by_the_same_number_gets_its_group():
    engine = _owned("1003", "luca:x:1003:1003::/home/luca:/bin/bash\n")
    [result] = reconciler(engine)._do_owner_group({"path": "/tank/documenti", "owner": "luca"})
    assert result.target_state == "applied"


def _marked(tmp_path, forget=()):
    config = FakeConfig()
    config.config_path = tmp_path / "config.yaml"
    (tmp_path / "network-reapply").write_text(yaml.safe_dump({"source": "tank", "forget": list(forget)}))
    return config


def test_a_network_taken_from_the_data_pool_is_applied_over_the_old_one(tmp_path):
    engine = _configured(_with_links(FakeEngine(), '[{"ifname": "eth0"}]'))
    config = _marked(tmp_path, forget=["fnas_lan3"])
    [forget, result] = reconciler(engine, config)._do_network([LAN])
    assert (forget.action, forget.params) == ("network.forget", {"interface": "fnas_lan3"})
    assert result.target_state == "applied"
    actions = [a for c, a, _ in engine.calls if c == "network" and a not in ("list_physical", "list_interfaces")]
    assert actions[0] == "connection_forget" and "ipv4_static" in actions
    assert not (tmp_path / "network-reapply").exists()


def test_an_existing_bridge_gets_the_taken_network_again(tmp_path):
    engine = _configured(_with_links(FakeEngine(), '[{"ifname": "fnas_lan1"}]'))
    [result] = reconciler(engine, _marked(tmp_path))._do_network([BRIDGE, PORT])
    assert result.target_state == "applied"
    actions = [a for c, a, _ in engine.calls if c == "network"]
    assert "bridge_create" not in actions and "bridge_port_add" in actions


def test_a_missing_wireguard_is_set_up_again():
    import wireguard
    entry = wireguard.new_server({"endpoint": "nas.example.org"}, [])
    engine = FakeEngine()
    engine.execute_with_stdin = lambda c, a, data, **k: (engine.calls.append((c, a, k)) or ("", ""))
    [result] = reconciler(engine)._do_network([entry])
    assert (result.action, result.target_state) == ("network.wireguard", "applied")
    assert [a for _, a, _ in engine.calls if a.startswith("wg_")] == ["wg_exists", "wg_write", "wg_apply"]


def test_smb_shares_wait_for_smbd_and_start_it(monkeypatch):
    import reconcile
    monkeypatch.setattr(reconcile.time, "sleep", lambda s: None)
    engine = FakeEngine(values={("samba", "smb_service"): "samba_server"})
    tries = {"n": 0}
    plain = engine.execute

    def execute(category, action, **kwargs):
        if (category, action) == ("samba", "create_share"):
            engine.calls.append((category, action, kwargs))
            tries["n"] += 1
            if tries["n"] < 3:
                raise CommandExecutionError("net usershare add: ... refused. Maybe smbd is not running.")
            return "", ""
        return plain(category, action, **kwargs)

    engine.execute = execute
    monkeypatch.setattr(reconcile.usershares, "prepare", lambda e: None)
    r = reconciler(engine)
    assert r._do_usershares_prepare().target_state == "applied"
    assert ("services", "start", {"timeout": 60, "service_name": "samba_server"}) in engine.calls
    assert r._do_smb_share_create({"name": "docs", "path": "/tank/docs", "valid_users": ["luca"]}).target_state == "applied"
    assert tries["n"] == 3


def test_other_smb_errors_are_not_retried(monkeypatch):
    import reconcile
    monkeypatch.setattr(reconcile.time, "sleep", lambda s: None)
    engine = FakeEngine(failing={("samba", "create_share")})
    result = reconciler(engine)._do_smb_share_create({"name": "docs", "path": "/tank/docs"})
    assert result.target_state == "failed"
    assert [a for _, a, _ in engine.calls].count("create_share") == 1


def test_an_administrator_joins_the_admin_groups_this_system_has(monkeypatch):
    """A configuration restored from another edition names no `docker` group."""
    import reconcile as reconcile_module
    monkeypatch.setattr(reconcile_module, "admin_groups_present", lambda engine: ["docker"])
    current = {"users": [{"username": "sarah", "groups": ["sudo"]},
                         {"username": "luca", "groups": []}]}
    desired = {"users": [ADMIN, {"username": "luca", "groups": []}]}
    [change] = diff(desired, current)
    assert (change.path, change.change_type) == ("users.sarah", "modify")
    assert change.new_value["groups"] == ["sudo", "docker"]
    current["users"][0]["groups"] = ["sudo", "docker"]
    assert diff(desired, current) == []


def test_without_an_administrator_no_admin_group_is_looked_up(monkeypatch):
    import reconcile as reconcile_module
    looked = []
    monkeypatch.setattr(reconcile_module, "admin_groups_present", lambda engine: looked.append(1) or ["docker"])
    diff({"users": [{"username": "luca", "groups": []}]}, {"users": [{"username": "luca", "groups": []}]})
    assert looked == []


# ---------------------------------------------------------------------------
# modules — each module reconciles its own section
# ---------------------------------------------------------------------------

class RecordingModule:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.seen = None

    def reconcile(self, desired, mode):
        if self.fail:
            raise RuntimeError("broken")
        self.seen = (desired, mode)
        return [ActionResult(action=f"{self.name}.done", params={}, target_state="applied")]


def with_modules(monkeypatch, *modules, error=None):
    class Loader:
        failed = {}

        def __init__(self, engine, config):
            if error:
                raise ModuleLoaderException(error)

        def instances(self):
            return [({"id": m.name, **({"after": m.after} if getattr(m, "after", None) else {})}, m)
                    for m in modules]

        def discover(self):
            return [m for m, _ in self.instances()]

    monkeypatch.setattr(reconcile, "ModuleLoader", Loader)


def test_modules_get_their_own_section_after_everything_else(monkeypatch):
    apps, quiet = RecordingModule("containers"), RecordingModule("other")
    with_modules(monkeypatch, apps, quiet)
    engine = FakeEngine(users={"sarah": ["sudo"]}, hostname="old")
    config = FakeConfig({"system": {"hostname": "nas"}, "users": [ADMIN],
                         "modules": {"containers": {"apps": ["jellyfin"]}}})
    results = reconciler(engine, config).reconcile(mode="additive")
    assert action_names(results) == ["system.hostname_set", "containers.done", "other.done"]
    assert apps.seen == ({"apps": ["jellyfin"]}, "additive")
    assert quiet.seen == ({}, "additive")


def test_a_module_runs_after_the_modules_its_manifest_names(monkeypatch):
    apps, proxy, other = RecordingModule("containers"), RecordingModule("proxy"), RecordingModule("other")
    apps.after = ["proxy", "missing"]
    with_modules(monkeypatch, apps, other, proxy)
    results = reconciler(FakeEngine(users={"sarah": ["sudo"]}), FakeConfig({"users": [ADMIN]})).reconcile()
    assert action_names(results) == ["other.done", "proxy.done", "containers.done"]


def test_modules_that_wait_for_each_other_keep_their_order(monkeypatch):
    one, two = RecordingModule("one"), RecordingModule("two")
    one.after, two.after = ["two"], ["one"]
    with_modules(monkeypatch, one, two)
    results = reconciler(FakeEngine(users={"sarah": ["sudo"]}), FakeConfig({"users": [ADMIN]})).reconcile()
    assert action_names(results) == ["one.done", "two.done"]


def test_a_failing_module_is_reported_and_the_others_still_run(monkeypatch):
    with_modules(monkeypatch, RecordingModule("broken", fail=True), RecordingModule("fine"))
    results = reconciler(FakeEngine(users={"sarah": ["sudo"]}), FakeConfig({"users": [ADMIN]})).reconcile()
    assert [(r.action, r.target_state) for r in results] == [
        ("modules.broken", "failed"), ("fine.done", "applied")]


def test_modules_that_cannot_be_loaded_are_one_failure(monkeypatch):
    with_modules(monkeypatch, error="no modules")
    results = reconciler(FakeEngine(users={"sarah": ["sudo"]}), FakeConfig({"users": [ADMIN]})).reconcile()
    assert [(r.action, r.target_state) for r in results] == [("modules", "failed")]


def test_a_dry_run_leaves_the_modules_alone(monkeypatch):
    module = RecordingModule("containers")
    with_modules(monkeypatch, module)
    reconciler(FakeEngine(users={"sarah": ["sudo"]}), FakeConfig({"users": [ADMIN]})).reconcile(dry_run=True)
    assert module.seen is None


class PowerEngine(FakeEngine):
    """An Artix/OpenRC system whose acpid rules live under `tmp`."""

    def __init__(self, tmp, running=""):
        super().__init__()
        self.profile_dir = str(Path(__file__).resolve().parents[2] / "freedomnas-profiles")
        self.os_type, self.init_system = "artix", "openrc"
        self.running = running
        self.section = {"category": "power-button", "rule_dir": str(tmp / "events"),
                        "rule_file": str(tmp / "events" / "freedomnas-power"),
                        "rule": "event=button/power.*\naction=/bin/sh -c poweroff\n", "service": "acpid"}

    def get_profile_section(self, category):
        return dict(self.section) if category == "power_button" else {}

    def execute(self, category, action, **kwargs):
        out = super().execute(category, action, **kwargs)
        if (category, action) == ("packages", "install"):
            Path(self.section["rule_dir"]).mkdir()
        if (category, action) == ("services", "is_running"):
            return self.running, ""
        return out


def test_the_power_button_step_is_planned_only_where_the_profile_asks_for_it(tmp_path):
    assert ("system.power_button", "ensure") not in paths(diff({}))
    assert ("system.power_button", "ensure") in paths(diff({}, engine=PowerEngine(tmp_path)))


def test_a_system_without_acpid_gets_it_its_rule_and_the_service(tmp_path):
    engine = PowerEngine(tmp_path)
    results = reconciler(engine)._do_power_button()
    [install] = [kw for c, a, kw in engine.calls if a == "install"]
    assert install["package_name"] == "acpid acpid-openrc"
    assert (tmp_path / "events" / "freedomnas-power").read_text() == engine.section["rule"]
    assert [a for c, a in engine.actions() if c == "services"] == ["enable", "is_running", "start"]
    assert action_names(results) == ["power_button.install", "power_button.service"]


def test_a_system_already_set_up_is_left_running(tmp_path):
    engine = PowerEngine(tmp_path, running="yes")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "freedomnas-power").write_text(engine.section["rule"])
    assert reconciler(engine)._do_power_button() == []
    assert [a for c, a in engine.actions()] == ["enable", "is_running"]


def test_a_changed_rule_restarts_acpid(tmp_path):
    engine = PowerEngine(tmp_path, running="yes")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "freedomnas-power").write_text("old\n")
    reconciler(engine)._do_power_button()
    assert ("services", "restart") in engine.actions()


def test_acpid_disabled_at_boot_stays_off(tmp_path):
    engine = PowerEngine(tmp_path)
    (tmp_path / "events").mkdir()
    config = FakeConfig({"services": {"boot": {"acpid": False}}})
    reconciler(engine, config)._do_power_button()
    assert [a for c, a in engine.actions() if c == "services"] == []


class ModulesEngine(FakeEngine):
    def __init__(self, offered):
        super().__init__()
        self.offered = offered

    def execute(self, category, action, **kwargs):
        out = super().execute(category, action, **kwargs)
        if (category, action) == ("packages", "module_search"):
            return "".join(f"{p}\t1.0\tdesc\n" for p in self.offered), ""
        return out


def test_a_module_the_configuration_names_but_the_system_lacks_is_installed(monkeypatch):
    class Loader:
        failed = {}

        def __init__(self, engine, config):
            pass

        def discover(self):
            return [{"id": "containers"}]

    monkeypatch.setattr(reconcile, "ModuleLoader", Loader)
    engine = ModulesEngine(["freedomnas-module-containers", "freedomnas-module-proxy"])
    results = reconciler(engine)._install_modules(
        {"containers": {"enabled": True}, "proxy": {"enabled": True}, "gone": {"enabled": True}, "empty": {}})
    installs = [kw for c, a, kw in engine.calls if a == "install"]
    assert installs == [{"package_name": "freedomnas-module-proxy", "timeout": 7200}]
    assert [(r.params["module"], r.target_state) for r in results] == [("gone", "skipped"), ("proxy", "applied")]


def test_nothing_is_installed_when_every_module_is_there(monkeypatch):
    class Loader:
        failed = {}

        def __init__(self, engine, config):
            pass

        def discover(self):
            return [{"id": "proxy"}]

    monkeypatch.setattr(reconcile, "ModuleLoader", Loader)
    engine = ModulesEngine(["freedomnas-module-proxy"])
    assert reconciler(engine)._install_modules({"proxy": {"enabled": True}}) == []
    assert engine.calls == []


def test_group_shares_are_given_back_again_once_numbers_moved(monkeypatch):
    engine = FakeEngine()
    rec = reconciler(engine)
    monkeypatch.setattr(reconcile.shadow, "take_ids",
                        lambda *a, **kw: ["media gid 1002 -> 1000", "sarah gid 1003 -> 1002"])
    restored = []
    monkeypatch.setattr(rec, "_do_share_group", lambda share: restored.append(share["name"]) or [])
    share = {"name": "media", "group": "media", "path": "/tank/media"}
    rec._apply_changes([Change("share_groups.media", None, share, "ensure"),
                        Change("users.sarah", None, {"username": "sarah", "uid": 1002, "gid": 1002}, "add")])
    assert restored == ["media", "media"]


def test_group_shares_are_given_back_once_when_no_number_moved(monkeypatch):
    engine = FakeEngine()
    rec = reconciler(engine)
    monkeypatch.setattr(reconcile.shadow, "take_ids", lambda *a, **kw: [])
    restored = []
    monkeypatch.setattr(rec, "_do_share_group", lambda share: restored.append(share["name"]) or [])
    share = {"name": "media", "group": "media", "path": "/tank/media"}
    rec._apply_changes([Change("share_groups.media", None, share, "ensure"),
                        Change("users.sarah", None, {"username": "sarah", "uid": 1002, "gid": 1002}, "add")])
    assert restored == ["media"]


class RecordingProgress:
    def __init__(self):
        self.calls = []

    def plan(self, labels):
        self.calls.append(("plan", labels))

    def start(self, index):
        self.calls.append(("start", index))

    def done(self, index, errors):
        self.calls.append(("done", index, errors))


def test_every_step_reaches_the_restore_lock(monkeypatch):
    apps = RecordingModule("containers")
    apps.after = ["broken"]
    with_modules(monkeypatch, apps, RecordingModule("broken", fail=True))
    engine = FakeEngine(users={"sarah": ["sudo"]}, hostname="old")
    config = FakeConfig({"system": {"hostname": "nas"}, "users": [ADMIN]})
    rec = reconciler(engine, config)
    rec.progress = RecordingProgress()
    rec.reconcile()
    plan = rec.progress.calls[0]
    assert plan == ("plan", ["Hostname", "Modules to install", "Module broken", "Module containers"])
    assert ("done", 0, []) in rec.progress.calls and ("done", 3, []) in rec.progress.calls
    assert ("done", 2, ["modules.broken: broken"]) in rec.progress.calls


def test_an_account_is_created_without_the_groups_this_system_lacks_yet(monkeypatch):
    engine = FakeEngine()
    rec = reconciler(engine)
    monkeypatch.setattr(rec, "_group_present", lambda group: group != "docker")
    result = rec._do_user_create({"username": "sarah", "groups": ["docker", "wheel"], "uid": 1005})
    assert result.target_state == "applied"
    [create] = [kw for c, a, kw in engine.calls if a in ("create", "create_with_uid")]
    assert create["groups"] == "wheel"


def test_modules_installed_by_the_reconcile_bring_their_profiles_and_steps(monkeypatch):
    present = [{"id": "users"}]

    class Loader:

        failed = {}

        def __init__(self, engine, config):
            pass

        def discover(self):
            return list(present)

        def instances(self):
            return [(m, RecordingModule(m["id"])) for m in present]

    class Reloading(ModulesEngine):
        reloads = 0

        def reload_profile(self):
            Reloading.reloads += 1

        def execute(self, category, action, **kwargs):
            if (category, action) == ("packages", "install"):
                present.append({"id": "proxy"})
            return super().execute(category, action, **kwargs)

    monkeypatch.setattr(reconcile, "ModuleLoader", Loader)
    engine = Reloading(["freedomnas-module-proxy"])
    config = FakeConfig({"users": [ADMIN], "modules": {"proxy": {"enabled": True}}})
    rec = reconciler(engine, config)
    rec.progress = RecordingProgress()
    added = []
    rec.progress.add = lambda label: added.append(label) or 10
    results = rec.reconcile()
    assert Reloading.reloads == 1
    assert added == ["Module proxy"]
    assert ("done", 10, []) in rec.progress.calls
    assert "proxy.done" in action_names(results)


def test_a_community_profile_left_out_by_the_engine_is_reported(monkeypatch):
    with_modules(monkeypatch, RecordingModule("fine"))
    engine = FakeEngine(users={"sarah": ["sudo"]})
    engine.profile_failures = {"broken": "Invalid JSON in profile fragment broken-debian.json, line 2 column 3: x"}
    results = reconciler(engine, FakeConfig({"users": [ADMIN]})).reconcile()
    assert [(r.action, r.target_state) for r in results] == [
        ("modules.broken.profile", "failed"), ("fine.done", "applied")]
    assert "line 2 column 3" in results[0].error


def test_broken_modules_are_reported_and_the_working_one_is_reconciled(tmp_path):
    import json
    base = tmp_path / "modules"
    for name, manifest, source in [
        ("badmanifest", '{"id": "badmanifest",', ""),
        ("badimport", json.dumps({"id": "badimport", "entry_point": "module:M"}), "raise RuntimeError('boom')\n"),
        ("good", json.dumps({"id": "good", "entry_point": "module:M"}),
         "from orchestrator import FreedomNASModule\n"
         "from reconcile import ActionResult\n"
         "class M(FreedomNASModule):\n"
         "    name = 'good'\n"
         "    def start(self): pass\n"
         "    def stop(self): pass\n"
         "    def health_check(self): return True\n"
         "    def reconcile(self, desired, mode):\n"
         "        return [ActionResult(action='good.done', params=desired, target_state='applied')]\n"),
    ]:
        (base / name).mkdir(parents=True)
        (base / name / "manifest.json").write_text(manifest)
        (base / name / "module.py").write_text(source)

    class Loader(reconcile.ModuleLoader):
        def __init__(self, engine, config):
            super().__init__(engine, config, modules_dir=str(base))

    engine = FakeEngine(users={"sarah": ["sudo"]})
    engine.os_type = "debian"
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(reconcile, "ModuleLoader", Loader)
        results = reconciler(engine, FakeConfig({"users": [ADMIN], "modules": {"good": {"x": 1}}})).reconcile()
    states = {r.action: (r.target_state, r.error) for r in results}
    assert states["good.done"][0] == "applied"
    assert states["modules.badmanifest"][0] == "failed" and "line 1 column" in states["modules.badmanifest"][1]
    assert states["modules.badimport"][0] == "failed" and "RuntimeError: boom" in states["modules.badimport"][1]
