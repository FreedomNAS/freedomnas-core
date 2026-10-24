"""Tests for ConfigurationManager — YAML-based persistent config in ZFS dataset."""

import copy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import paths
from config import ConfigurationException, ConfigurationManager
from engine import FreedomNASEngine

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")
EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path) -> ConfigurationManager:
    """Return a ConfigurationManager backed by a temp file."""
    return ConfigurationManager(config_path=str(tmp_path / "config.yaml"))


@pytest.fixture
def saved_cfg(tmp_path) -> ConfigurationManager:
    """Return a ConfigurationManager with a saved default config on disk."""
    path = tmp_path / "config.yaml"
    mgr = ConfigurationManager(config_path=str(path))
    mgr.save()
    return mgr


# ---------------------------------------------------------------------------
# Load behaviour
# ---------------------------------------------------------------------------

class TestLoad:
    def test_fresh_load_uses_defaults(self, tmp_path):
        mgr = ConfigurationManager(config_path=str(tmp_path / "new.yaml"))
        assert mgr.get_hostname() == "freedomnas"
        assert mgr.is_initialized() is False
        assert mgr.get_edition() is None

    def test_loads_existing_yaml(self, tmp_path):
        data = {"system": {"hostname": "nas-test", "initialized": False,
                            "timezone": "UTC", "language": "en_US.UTF-8",
                            "initialized_at": None, "initialized_by": None,
                            "edition": None, "root_filesystem": None},
                "network": {"interfaces": [], "interfaces_mapping": {}},
                "users": [],
                "shares": {"smb": [], "nfs": []},
                "storage": {"pools": [], "boot_pool": None},
                "snapshots": {"root": [], "boot": []},
                "updates": {"topgrade": {"configured": False,
                                         "auto_snapshot": True,
                                         "enabled_steps": []}},
                "reporting": {"enabled": False}}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(data))
        mgr = ConfigurationManager(config_path=str(path))
        assert mgr.get_hostname() == "nas-test"

    def test_missing_keys_merged_from_defaults(self, tmp_path):
        minimal = {"system": {"hostname": "sparse", "initialized": False,
                               "timezone": "UTC", "language": "en_US.UTF-8",
                               "initialized_at": None, "initialized_by": None,
                               "edition": None, "root_filesystem": None},
                   "network": {"interfaces": [], "interfaces_mapping": {}},
                   "users": [],
                   "shares": {"smb": [], "nfs": []},
                   "storage": {"pools": []},
                   "updates": {"topgrade": {"configured": False,
                                             "auto_snapshot": True,
                                             "enabled_steps": []}},
                   "reporting": {"enabled": False}}
        path = tmp_path / "sparse.yaml"
        path.write_text(yaml.dump(minimal))
        mgr = ConfigurationManager(config_path=str(path))
        # boot_pool is in DEFAULT_CONFIG but not in sparse — should be filled in
        assert "boot_pool" in mgr.config["storage"]

    def test_invalid_yaml_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(": broken: yaml: {{")
        with pytest.raises(ConfigurationException):
            ConfigurationManager(config_path=str(path))


# ---------------------------------------------------------------------------
# Save — atomic write
# ---------------------------------------------------------------------------

class TestSave:
    def test_save_creates_file(self, cfg, tmp_path):
        cfg.save()
        assert (tmp_path / "config.yaml").exists()

    def test_no_tmp_file_left_after_save(self, cfg, tmp_path):
        cfg.save()
        assert not (tmp_path / "config.yaml.tmp").exists()

    def test_save_is_readable_back(self, cfg, tmp_path):
        cfg.set_hostname("roundtrip-host")
        cfg.save()
        mgr2 = ConfigurationManager(config_path=str(tmp_path / "config.yaml"))
        assert mgr2.get_hostname() == "roundtrip-host"

    def test_backup_restored_on_write_failure(self, saved_cfg, tmp_path):
        config_path = tmp_path / "config.yaml"
        original_content = config_path.read_text()

        # Simulate a failure after backup is created but before rename
        real_rename = Path.rename

        call_count = [0]
        def flaky_rename(self, target):
            call_count[0] += 1
            if call_count[0] == 2:  # second rename is the tmp -> permanent promotion
                raise OSError("disk full")
            return real_rename(self, target)

        with patch.object(Path, "rename", flaky_rename):
            with pytest.raises(ConfigurationException):
                saved_cfg.save()

        # The backup should have been restored
        assert config_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_required_section_raises(self, cfg):
        del cfg.config["users"]
        with pytest.raises(ConfigurationException, match="users"):
            cfg._validate_config(cfg.config)

    def test_wrong_type_raises(self, cfg):
        cfg.config["users"] = "not a list"
        with pytest.raises(ConfigurationException):
            cfg._validate_config(cfg.config)

    def test_smb_not_list_raises(self, cfg):
        cfg.config["shares"]["smb"] = {}
        with pytest.raises(ConfigurationException):
            cfg._validate_config(cfg.config)

    def test_valid_config_returns_true(self, cfg):
        assert cfg._validate_config(cfg.config) is True


# ---------------------------------------------------------------------------
# Deep copy — DEFAULT_CONFIG must not be mutated
# ---------------------------------------------------------------------------

class TestDeepCopy:
    def test_mutating_config_does_not_affect_default(self, cfg):
        original_default = copy.deepcopy(ConfigurationManager.DEFAULT_CONFIG)
        cfg.config["users"].append({"username": "test"})
        assert ConfigurationManager.DEFAULT_CONFIG == original_default

    def test_two_managers_do_not_share_lists(self, tmp_path):
        m1 = ConfigurationManager(config_path=str(tmp_path / "a.yaml"))
        m2 = ConfigurationManager(config_path=str(tmp_path / "b.yaml"))
        m1.config["users"].append({"username": "alice"})
        assert m2.config["users"] == []


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

class TestSystemHelpers:
    def test_get_set_hostname(self, cfg, tmp_path):
        cfg.set_hostname("nas-ufficio")
        mgr2 = ConfigurationManager(config_path=str(tmp_path / "config.yaml"))
        assert mgr2.get_hostname() == "nas-ufficio"

    def test_mark_initialized(self, cfg):
        cfg.mark_initialized("debian", "installer")
        assert cfg.is_initialized() is True
        assert cfg.get_edition() == "debian"

    def test_set_root_filesystem_valid(self, cfg):
        cfg.set_root_filesystem("zfs")
        assert cfg.get_root_filesystem() == "zfs"

    def test_set_root_filesystem_invalid(self, cfg):
        with pytest.raises(ConfigurationException):
            cfg.set_root_filesystem("ext4")


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

class TestUserCRUD:
    def test_add_and_get_user(self, cfg):
        cfg.add_user({"username": "alice", "uid": 1000})
        user = cfg.get_user("alice")
        assert user is not None
        assert user["uid"] == 1000

    def test_get_nonexistent_user_returns_none(self, cfg):
        assert cfg.get_user("ghost") is None

    def test_add_user_without_username_raises(self, cfg):
        with pytest.raises(ConfigurationException):
            cfg.add_user({"uid": 1001})

    def test_add_user_replaces_existing(self, cfg):
        cfg.add_user({"username": "alice", "uid": 1000})
        cfg.add_user({"username": "alice", "uid": 1001})
        assert cfg.get_user("alice")["uid"] == 1001
        assert len([u for u in cfg.get_users() if u["username"] == "alice"]) == 1

    def test_remove_user(self, cfg):
        cfg.add_user({"username": "alice", "uid": 1000})
        cfg.remove_user("alice")
        assert cfg.get_user("alice") is None


# ---------------------------------------------------------------------------
# Storage CRUD
# ---------------------------------------------------------------------------

class TestStorageCRUD:
    def test_add_and_get_pool(self, cfg):
        cfg.add_pool({"name": "datapool", "vdev_type": "raidz2"})
        pool = cfg.get_pool("datapool")
        assert pool is not None
        assert pool["vdev_type"] == "raidz2"

    def test_get_nonexistent_pool_returns_none(self, cfg):
        assert cfg.get_pool("ghost") is None

    def test_add_pool_without_name_raises(self, cfg):
        with pytest.raises(ConfigurationException):
            cfg.add_pool({"vdev_type": "mirror"})

    def test_add_root_snapshot(self, cfg):
        cfg.add_root_snapshot("@pre-update")
        snaps = cfg.get_root_snapshots()
        assert len(snaps) == 1
        assert snaps[0]["name"] == "@pre-update"

    def test_set_boot_pool(self, cfg):
        cfg.set_boot_pool("zroot")
        assert cfg.get_boot_pool() == "zroot"


# ---------------------------------------------------------------------------
# Network CRUD
# ---------------------------------------------------------------------------

class TestNetworkCRUD:
    def test_add_and_get_interface(self, cfg):
        cfg.add_interface({
            "logical_name": "fnas_lan",
            "mac_address": "00:11:22:33:44:55",
            "role": "LAN_MAIN",
        })
        iface = cfg.get_interface("fnas_lan")
        assert iface is not None
        assert iface["role"] == "LAN_MAIN"

    def test_get_interface_by_mac(self, cfg):
        cfg.add_interface({
            "logical_name": "fnas_lan",
            "mac_address": "00:11:22:33:44:55",
        })
        assert cfg.get_interface_by_mac("00:11:22:33:44:55") == "fnas_lan"

    def test_add_interface_without_logical_name_raises(self, cfg):
        with pytest.raises(ConfigurationException):
            cfg.add_interface({"mac_address": "00:11:22:33:44:55"})

    def test_add_interface_replaces_existing(self, cfg):
        cfg.add_interface({"logical_name": "fnas_lan", "role": "LAN_MAIN"})
        cfg.add_interface({"logical_name": "fnas_lan", "role": "LAN_BACKUP"})
        ifaces = [i for i in cfg.get_interfaces() if i["logical_name"] == "fnas_lan"]
        assert len(ifaces) == 1
        assert ifaces[0]["role"] == "LAN_BACKUP"


# ---------------------------------------------------------------------------
# Share CRUD
# ---------------------------------------------------------------------------

class TestShareCRUD:
    def test_add_and_get_smb_share(self, cfg):
        cfg.add_smb_share({"name": "Docs", "path": "/mnt/pool/docs"})
        share = cfg.get_smb_share("Docs")
        assert share is not None
        assert share["path"] == "/mnt/pool/docs"

    def test_add_smb_share_without_name_raises(self, cfg):
        with pytest.raises(ConfigurationException):
            cfg.add_smb_share({"path": "/mnt/pool/docs"})

    def test_get_smb_shares_returns_list(self, cfg):
        cfg.add_smb_share({"name": "A", "path": "/mnt/a"})
        cfg.add_smb_share({"name": "B", "path": "/mnt/b"})
        assert len(cfg.get_smb_shares()) == 2

    def test_get_nfs_shares_returns_list(self, cfg):
        assert isinstance(cfg.get_nfs_shares(), list)


# ---------------------------------------------------------------------------
# Dot-notation accessors
# ---------------------------------------------------------------------------

class TestDotNotationAccessors:
    def test_get_top_level(self, cfg):
        assert cfg.get("system.hostname") == "freedomnas"

    def test_get_missing_returns_default(self, cfg):
        assert cfg.get("system.nonexistent", "fallback") == "fallback"

    def test_set_creates_key(self, cfg):
        cfg.set("system.timezone", "Europe/Rome")
        assert cfg.get("system.timezone") == "Europe/Rome"

    def test_to_dict_is_copy(self, cfg):
        d = cfg.to_dict()
        d["system"]["hostname"] = "mutated"
        assert cfg.get_hostname() == "freedomnas"


class TestConfigPathDiscovery:
    """
    Where the configuration lives. The `.freedomnas` dataset is mounted at
    <config_dir>/state, config_dir coming from the OS profile, so that is
    where the installer, the images and the reconciler all read and write; a
    mismatch here makes the reconciler silently apply defaults over a
    perfectly good configuration.
    """

    @pytest.mark.parametrize("edition, config_dir", [
        ("debian", "/etc/freedomnas"),
        ("freebsd", "/usr/local/etc/freedomnas"),
    ])
    def test_paths_come_from_the_os_profile(self, monkeypatch, edition, config_dir):
        engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
        monkeypatch.setattr(paths, "_engine", lambda: engine)
        assert str(ConfigurationManager.default_config_path()) == f"{config_dir}/state/config.yaml"
        assert str(ConfigurationManager.backup_config_path()) == f"{config_dir}/config.yaml"

    @pytest.mark.parametrize("edition", EDITIONS)
    def test_the_dataset_mounts_where_the_config_is_read(self, edition):
        engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
        command = engine.get_command("zfs", "create_state_dataset", pool_name="tank")
        assert f"mountpoint={paths.state_dir(engine)} " in command

    def test_finds_the_dataset_copy_first(self, tmp_path, monkeypatch):
        primary = tmp_path / "state" / "config.yaml"
        backup = tmp_path / "etc" / "config.yaml"
        for path in (primary, backup):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("system:\n  hostname: fromdisk\n")

        monkeypatch.setattr(ConfigurationManager, "DEFAULT_CONFIG_PATH", primary)
        monkeypatch.setattr(ConfigurationManager, "BACKUP_CONFIG_PATH", backup)
        assert ConfigurationManager()._find_config_path() == primary

    def test_falls_back_to_the_local_copy(self, tmp_path, monkeypatch):
        primary = tmp_path / "state" / "config.yaml"
        backup = tmp_path / "etc" / "config.yaml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text("system:\n  hostname: fromdisk\n")

        monkeypatch.setattr(ConfigurationManager, "DEFAULT_CONFIG_PATH", primary)
        monkeypatch.setattr(ConfigurationManager, "BACKUP_CONFIG_PATH", backup)
        assert ConfigurationManager()._find_config_path() == backup

    def test_a_pending_setup_outlasts_a_restored_configuration(self, tmp_path, monkeypatch):
        marker = tmp_path / "setup-pending"
        monkeypatch.setattr(ConfigurationManager, "SETUP_PENDING_PATH", marker)
        manager = ConfigurationManager(str(tmp_path / "config.yaml"))
        manager.mark_initialized("debian")
        assert not manager.needs_setup()

        marker.touch()
        assert manager.setup_pending() and manager.needs_setup()

        manager.clear_setup_pending()
        assert not marker.exists() and not manager.needs_setup()
        manager.clear_setup_pending()

    def test_the_pending_file_says_who_left_it(self, tmp_path, monkeypatch):
        marker = tmp_path / "setup-pending"
        monkeypatch.setattr(ConfigurationManager, "SETUP_PENDING_PATH", marker)
        manager = ConfigurationManager(str(tmp_path / "config.yaml"))
        assert manager.setup_pending_kind() == ""
        marker.touch()
        assert manager.setup_pending_kind() == ""
        marker.write_text("install\n")
        assert manager.setup_pending_kind() == "install"


class TestServiceBoot:
    def test_a_boot_choice_survives_a_reload(self, tmp_path):
        path = tmp_path / "config.yaml"
        mgr = ConfigurationManager(config_path=str(path))
        mgr.set_service_boot("agetty.tty1", False)
        mgr.set_service_boot("nfs-server", True)
        assert ConfigurationManager(config_path=str(path)).get_service_boot() == {
            "agetty.tty1": False, "nfs-server": True}

    def test_a_configuration_from_before_services_has_no_choices(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"system": {"hostname": "old"}}))
        assert ConfigurationManager(config_path=str(path)).get_service_boot() == {}


class TestModuleSections:
    def test_a_module_section_survives_a_reload(self, tmp_path):
        path = tmp_path / "config.yaml"
        ConfigurationManager(config_path=str(path)).set_module_config("containers", {"pool": "tank"})
        assert ConfigurationManager(config_path=str(path)).get_module_config("containers") == {"pool": "tank"}

    def test_a_module_without_a_section_gets_an_empty_one(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"system": {"hostname": "old"}}))
        assert ConfigurationManager(config_path=str(path)).get_module_config("containers") == {}

    def test_changing_what_was_read_does_not_change_the_configuration(self, tmp_path):
        mgr = ConfigurationManager(config_path=str(tmp_path / "config.yaml"))
        mgr.set_module_config("containers", {"apps": ["jellyfin"]})
        mgr.get_module_config("containers")["apps"].append("immich")
        assert mgr.get_module_config("containers") == {"apps": ["jellyfin"]}


def test_remove_and_rename_root_snapshots(tmp_path):
    from config import ConfigurationManager
    cfg = ConfigurationManager(str(tmp_path / "config.yaml"))
    for name in ("tank/a@s1", "tank/a/b@s1", "tank/a@s2"):
        cfg.add_root_snapshot(name)
    cfg.remove_root_snapshot("tank/a@s1")
    assert [r["name"] for r in cfg.get_root_snapshots()] == ["tank/a/b@s1", "tank/a@s2"]
    cfg.add_root_snapshot("tank/a@s1")
    cfg.remove_root_snapshot("tank/a@s1", recursive=True)
    assert [r["name"] for r in cfg.get_root_snapshots()] == ["tank/a@s2"]
    cfg.rename_root_snapshot("tank/a@s2", "tank/a@kept")
    assert [r["name"] for r in cfg.get_root_snapshots()] == ["tank/a@kept"]


def test_a_bridge_does_not_take_its_ports_mac_in_the_name_mapping(tmp_path):
    from config import ConfigurationManager
    cfg = ConfigurationManager(str(tmp_path / "config.yaml"))
    cfg.add_interface({"logical_name": "fnas_lan1", "mac_address": "52:54:00:00:00:0a"})
    cfg.add_interface({"logical_name": "fnas_bridge1", "type": "bridge", "mac_address": "52:54:00:00:00:0a"})
    assert cfg.get("network.interfaces_mapping") == {"52:54:00:00:00:0a": "fnas_lan1"}


def test_an_administrators_group_is_recorded_as_the_admin_flag(tmp_path):
    from config import known_admin_groups, normalize_user
    assert {"sudo", "wheel"} <= known_admin_groups()
    assert normalize_user({"username": "a", "groups": ["sudo", "docker"]}) == {
        "username": "a", "groups": ["docker"], "admin": True}
    assert normalize_user({"username": "b", "groups": ["wheel"], "admin": False})["admin"] is True
    assert normalize_user({"username": "c", "groups": ["users"]}) == {"username": "c", "groups": ["users"]}


def test_an_old_configuration_is_read_with_admin_flags(tmp_path):
    from config import ConfigurationManager
    path = tmp_path / "config.yaml"
    path.write_text("users:\n- username: sarah\n  groups: [sudo, docker]\n")
    manager = ConfigurationManager(config_path=str(path))
    assert manager.get_user("sarah") == {"username": "sarah", "groups": ["docker"], "admin": True}
    manager.add_user({"username": "luca", "groups": ["wheel"]})
    assert manager.get_user("luca")["admin"] is True
    assert manager.get("system.saved_at")


def test_every_save_runs_the_hooks_with_the_file(tmp_path):
    mgr = ConfigurationManager(config_path=str(tmp_path / "config.yaml"))
    seen = []
    mgr.on_save.append(seen.append)
    mgr.save()
    mgr.save()
    assert seen == [tmp_path / "config.yaml", tmp_path / "config.yaml"]


def test_a_failing_hook_does_not_fail_the_save(tmp_path):
    mgr = ConfigurationManager(config_path=str(tmp_path / "config.yaml"))

    def broken(path):
        raise RuntimeError("no pool")

    mgr.on_save.append(broken)
    mgr.save()
    assert (tmp_path / "config.yaml").exists()
