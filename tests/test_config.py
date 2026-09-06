"""Tests for ConfigurationManager — YAML-based persistent config in ZFS dataset."""

import copy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from config import ConfigurationException, ConfigurationManager


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
        cfg.set_root_filesystem("btrfs")
        assert cfg.get_root_filesystem() == "btrfs"

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
    /etc/freedomnas/state, so that is where the installer, the images and the
    reconciler all read and write; a mismatch here makes the reconciler
    silently apply defaults over a perfectly good configuration.
    """

    def test_default_path_matches_the_mounted_dataset(self):
        assert str(ConfigurationManager.DEFAULT_CONFIG_PATH) == (
            "/etc/freedomnas/state/config.yaml"
        )

    def test_backup_path_is_the_local_fallback(self):
        assert str(ConfigurationManager.BACKUP_CONFIG_PATH) == (
            "/etc/freedomnas/config.yaml"
        )

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
