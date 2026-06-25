"""
FreedomNAS Configuration Manager

Handles persistent configuration stored in ZFS dataset (.freedomnas).
The configuration is stored as YAML and is designed to survive OS reinstallation
and cross-OS migration.
"""

import copy
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger("FreedomNAS.Config")


class ConfigurationException(Exception):
    """Base exception for configuration errors"""
    pass


class ConfigurationManager:
    """
    Manages FreedomNAS system configuration stored in YAML format.
    
    The configuration is stored in a ZFS dataset (.freedomnas) at:
    /mnt/zpool/.freedomnas/config.yaml
    
    This allows:
    - Configuration persistence across OS reinstallation
    - Cross-OS migration (Debian <-> FreeBSD)
    - Automated recovery of system state
    """
    
    DEFAULT_CONFIG_PATH = Path("/mnt/zpool/.freedomnas/config.yaml")
    BACKUP_CONFIG_PATH = Path("/etc/freedomnas/config.yaml")
    
    # Default configuration template
    DEFAULT_CONFIG = {
        "system": {
            "hostname": "freedomnas",
            "timezone": "UTC",
            "language": "en_US.UTF-8",
            "initialized": False,
            "initialized_at": None,
            "initialized_by": None,  # "installer" or "webui"
            "edition": None,  # "debian" or "freebsd"
            "root_filesystem": None,  # "btrfs" (Linux) or "zfs" (FreeBSD)
        },
        "network": {
            "interfaces": [],
            "interfaces_mapping": {},  # MAC -> logical_name
        },
        "users": [],
        "shares": {
            "smb": [],
            "nfs": [],
        },
        "storage": {
            "pools": [],
            "boot_pool": None,  # For FreeBSD boot pool
        },
        "snapshots": {
            "root": [],  # Root filesystem snapshots
            "boot": [],  # Boot snapshots (FreeBSD)
        },
        "updates": {
            "topgrade": {
                "configured": False,
                "auto_snapshot": True,
                "enabled_steps": [],
            }
        },
        "reporting": {
            "enabled": False,
        }
    }
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the configuration manager.
        
        Args:
            config_path: Path to the configuration file. If not provided,
                        tries default ZFS location, then backup location.
        """
        self.config_path = Path(config_path) if config_path else self._find_config_path()
        self.config: Dict[str, Any] = {}
        self.load()
    
    def _find_config_path(self) -> Path:
        """
        Find the configuration file in standard locations.
        
        Returns:
            Path: The configuration file path
        
        Raises:
            ConfigurationException: If no config file is found
        """
        # Try primary location first (ZFS dataset)
        if self.DEFAULT_CONFIG_PATH.exists():
            logger.info(f"Found configuration at: {self.DEFAULT_CONFIG_PATH}")
            return self.DEFAULT_CONFIG_PATH
        
        # Try backup location
        if self.BACKUP_CONFIG_PATH.exists():
            logger.info(f"Found configuration at: {self.BACKUP_CONFIG_PATH}")
            return self.BACKUP_CONFIG_PATH
        
        # Return primary location (will create if needed)
        logger.warning(f"No existing configuration found. Using default: {self.DEFAULT_CONFIG_PATH}")
        return self.DEFAULT_CONFIG_PATH
    
    def load(self) -> None:
        """
        Load configuration from disk.
        
        If the file doesn't exist, initializes with default configuration.
        Validates configuration structure after loading.
        """
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    loaded_config = yaml.safe_load(f) or {}
                self.config = self._merge_with_defaults(loaded_config)
                self._validate_config(self.config)
                logger.info(f"Loaded configuration from: {self.config_path}")
            except yaml.YAMLError as e:
                raise ConfigurationException(f"Invalid YAML in {self.config_path}: {e}")
            except ConfigurationException:
                raise
            except Exception as e:
                raise ConfigurationException(f"Error loading configuration: {e}")
        else:
            logger.info("No configuration file found. Using defaults.")
            self.config = copy.deepcopy(self.DEFAULT_CONFIG)
            self._validate_config(self.config)
    
    def save(self) -> None:
        """
        Save configuration to disk using atomic writes.
        
        Implements safe atomic write pattern:
        1. Write to temporary file
        2. Backup existing file (if any)
        3. Promote temporary file to permanent (atomic rename)
        
        If any step fails, the backup is restored automatically.
        This prevents data loss if write fails mid-operation.
        
        Raises:
            ConfigurationException: If save operation fails
        """
        try:
            # Validate before saving
            self._validate_config(self.config)
            
            # Ensure parent directory exists
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 1. Write to temporary file first
            temp_path = self.config_path.with_suffix(".yaml.tmp")
            header = self._get_config_header()
            
            with open(temp_path, 'w') as f:
                f.write(header)
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
            
            temp_path.chmod(0o600)
            logger.debug(f"Wrote temporary config to: {temp_path}")
            
            # 2. Backup existing file (if it exists)
            backup_path = self.config_path.with_suffix(".yaml.backup")
            if self.config_path.exists():
                self.config_path.rename(backup_path)
                logger.info(f"Created backup: {backup_path}")
            
            # 3. Promote temporary to permanent (atomic on POSIX)
            temp_path.rename(self.config_path)
            logger.info(f"Saved configuration to: {self.config_path}")
            
        except ConfigurationException:
            raise
        except Exception as e:
            # Automatic recovery: restore backup if new file doesn't exist
            backup_path = self.config_path.with_suffix(".yaml.backup")
            if backup_path.exists() and not self.config_path.exists():
                backup_path.rename(self.config_path)
                logger.warning(f"Save failed, restored backup from: {backup_path}")
            raise ConfigurationException(f"Error saving configuration: {e}")
    
    def _merge_with_defaults(self, loaded_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge loaded configuration with defaults.
        
        Ensures that missing keys from defaults are added.
        
        Args:
            loaded_config: Configuration loaded from file
        
        Returns:
            Dict: Merged configuration
        """
        merged = copy.deepcopy(self.DEFAULT_CONFIG)
        
        def deep_merge(base: Dict, override: Dict) -> Dict:
            for key, value in override.items():
                if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                    base[key] = deep_merge(base[key], value)
                else:
                    base[key] = value
            return base
        
        return deep_merge(merged, loaded_config)
    
    def _validate_config(self, config: Dict[str, Any]) -> bool:
        """
        Validate configuration structure.
        
        Ensures required sections exist and have correct types.
        Helps catch manual editing errors early.
        
        Args:
            config: Configuration dictionary to validate
        
        Returns:
            bool: True if valid
        
        Raises:
            ConfigurationException: If configuration is invalid
        """
        # Check required top-level sections
        required_sections = ["system", "network", "users", "shares", "storage", "updates"]
        for section in required_sections:
            if section not in config:
                raise ConfigurationException(
                    f"Missing required configuration section: '{section}'"
                )
        
        # Validate type constraints
        type_constraints = {
            "users": list,
            "shares": dict,
            "storage": dict,
            "system": dict,
            "network": dict,
            "updates": dict,
        }
        
        for key, expected_type in type_constraints.items():
            if not isinstance(config.get(key), expected_type):
                raise ConfigurationException(
                    f"Configuration section '{key}' must be {expected_type.__name__}, "
                    f"got {type(config.get(key)).__name__}"
                )
        
        # Validate nested structure
        if not isinstance(config["shares"].get("smb"), list):
            raise ConfigurationException("shares.smb must be a list")
        
        if not isinstance(config["shares"].get("nfs"), list):
            raise ConfigurationException("shares.nfs must be a list")
        
        if not isinstance(config["storage"].get("pools"), list):
            raise ConfigurationException("storage.pools must be a list")
        
        if not isinstance(config["network"].get("interfaces"), list):
            raise ConfigurationException("network.interfaces must be a list")
        
        if not isinstance(config["network"].get("interfaces_mapping"), dict):
            raise ConfigurationException("network.interfaces_mapping must be a dict")
        
        # Validate system subsection
        if not isinstance(config["system"].get("initialized"), bool):
            raise ConfigurationException("system.initialized must be a boolean")
        
        logger.debug("Configuration validation passed")
        return True
    
    def _get_config_header(self) -> str:
        """
        Generate YAML header with metadata.
        
        Returns:
            str: Header comment block for YAML file
        """
        edition = self.get_edition() or "not set"
        hostname = self.get_hostname()
        initialized = "yes" if self.is_initialized() else "no"
        
        header = (
            "# FreedomNAS Configuration\n"
            f"# Last modified: {datetime.now().isoformat()}\n"
            f"# Edition: {edition}\n"
            f"# Hostname: {hostname}\n"
            f"# Initialized: {initialized}\n"
            "# DO NOT EDIT MANUALLY unless you know what you're doing!\n"
            "# Use the WebUI or API to make configuration changes.\n"
            "# Manual edits may be overwritten by system updates.\n"
            "---\n"
        )
        return header
    
    # System configuration methods
    
    def get_hostname(self) -> str:
        """Get system hostname"""
        return self.config["system"]["hostname"]
    
    def set_hostname(self, hostname: str) -> None:
        """Set system hostname"""
        self.config["system"]["hostname"] = hostname
        self.save()
    
    def is_initialized(self) -> bool:
        """Check if the system has been initialized"""
        return self.config["system"]["initialized"]
    
    def mark_initialized(self, edition: str, initialized_by: str = "installer") -> None:
        """
        Mark the system as initialized.
        
        Args:
            edition: OS edition ("debian" or "freebsd")
            initialized_by: Who initialized it ("installer" or "webui")
        """
        self.config["system"]["initialized"] = True
        self.config["system"]["initialized_at"] = datetime.now().isoformat()
        self.config["system"]["initialized_by"] = initialized_by
        self.config["system"]["edition"] = edition
        self.save()
    
    def get_edition(self) -> Optional[str]:
        """Get the installed OS edition"""
        return self.config["system"].get("edition")
    
    def get_root_filesystem(self) -> Optional[str]:
        """Get the root filesystem type (btrfs or zfs)"""
        return self.config["system"].get("root_filesystem")
    
    def set_root_filesystem(self, fs_type: str) -> None:
        """
        Set the root filesystem type.
        
        Args:
            fs_type: "btrfs" for Linux, "zfs" for FreeBSD
        """
        if fs_type not in ["btrfs", "zfs"]:
            raise ConfigurationException(
                f"Invalid root filesystem type: {fs_type}. "
                f"Must be 'btrfs' or 'zfs'"
            )
        self.config["system"]["root_filesystem"] = fs_type
        self.save()
    
    # Network configuration methods
    
    def get_interface(self, logical_name: str) -> Optional[Dict[str, Any]]:
        """Get interface configuration by logical name"""
        for interface in self.config["network"]["interfaces"]:
            if interface.get("logical_name") == logical_name:
                return interface
        return None
    
    def add_interface(self, interface_config: Dict[str, Any]) -> None:
        """Add or update an interface configuration"""
        logical_name = interface_config.get("logical_name")
        if not logical_name:
            raise ConfigurationException("Interface must have 'logical_name'")
        
        # Remove existing interface with same name
        self.config["network"]["interfaces"] = [
            i for i in self.config["network"]["interfaces"]
            if i.get("logical_name") != logical_name
        ]
        
        # Add new interface
        self.config["network"]["interfaces"].append(interface_config)
        
        # Update MAC mapping if available
        mac = interface_config.get("mac_address")
        if mac:
            self.config["network"]["interfaces_mapping"][mac] = logical_name
        
        self.save()
    
    def get_interfaces(self) -> List[Dict[str, Any]]:
        """Get all interface configurations"""
        return self.config["network"]["interfaces"]
    
    def get_interface_by_mac(self, mac_address: str) -> Optional[str]:
        """Get logical interface name by MAC address"""
        return self.config["network"]["interfaces_mapping"].get(mac_address)
    
    # User configuration methods
    
    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user configuration by username"""
        for user in self.config["users"]:
            if user.get("username") == username:
                return user
        return None
    
    def add_user(self, user_config: Dict[str, Any]) -> None:
        """Add or update a user"""
        username = user_config.get("username")
        if not username:
            raise ConfigurationException("User must have 'username'")
        
        # Remove existing user
        self.config["users"] = [
            u for u in self.config["users"]
            if u.get("username") != username
        ]
        
        self.config["users"].append(user_config)
        self.save()
    
    def get_users(self) -> List[Dict[str, Any]]:
        """Get all user configurations"""
        return self.config["users"]
    
    def remove_user(self, username: str) -> None:
        """Remove a user"""
        self.config["users"] = [
            u for u in self.config["users"]
            if u.get("username") != username
        ]
        self.save()
    
    # Storage configuration methods
    
    def get_pool(self, pool_name: str) -> Optional[Dict[str, Any]]:
        """Get pool configuration by name"""
        for pool in self.config["storage"]["pools"]:
            if pool.get("name") == pool_name:
                return pool
        return None
    
    def add_pool(self, pool_config: Dict[str, Any]) -> None:
        """Add or update a ZFS pool configuration"""
        pool_name = pool_config.get("name")
        if not pool_name:
            raise ConfigurationException("Pool must have 'name'")
        
        # Remove existing pool
        self.config["storage"]["pools"] = [
            p for p in self.config["storage"]["pools"]
            if p.get("name") != pool_name
        ]
        
        self.config["storage"]["pools"].append(pool_config)
        self.save()
    
    def get_pools(self) -> List[Dict[str, Any]]:
        """Get all pool configurations"""
        return self.config["storage"]["pools"]
    
    def add_root_snapshot(self, snapshot_name: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Record a root filesystem snapshot.
        
        Args:
            snapshot_name: Name of the snapshot
            metadata: Optional metadata (timestamp, size, etc.)
        """
        snapshot_record = {
            "name": snapshot_name,
            "created_at": datetime.now().isoformat(),
        }
        if metadata:
            snapshot_record.update(metadata)
        
        if "root" not in self.config["snapshots"]:
            self.config["snapshots"]["root"] = []
        
        self.config["snapshots"]["root"].append(snapshot_record)
        self.save()
    
    def get_root_snapshots(self) -> List[Dict[str, Any]]:
        """Get all root filesystem snapshots"""
        return self.config.get("snapshots", {}).get("root", [])
    
    def get_boot_pool(self) -> Optional[str]:
        """Get the boot pool name (FreeBSD only)"""
        return self.config["storage"].get("boot_pool")
    
    def set_boot_pool(self, pool_name: str) -> None:
        """
        Set the boot pool name (FreeBSD only).
        
        Args:
            pool_name: Name of the boot pool
        """
        self.config["storage"]["boot_pool"] = pool_name
        self.save()
    
    def get_smb_share(self, share_name: str) -> Optional[Dict[str, Any]]:
        """Get Samba share configuration"""
        for share in self.config["shares"]["smb"]:
            if share.get("name") == share_name:
                return share
        return None
    
    def add_smb_share(self, share_config: Dict[str, Any]) -> None:
        """Add or update a Samba share"""
        share_name = share_config.get("name")
        if not share_name:
            raise ConfigurationException("SMB share must have 'name'")
        
        # Remove existing share
        self.config["shares"]["smb"] = [
            s for s in self.config["shares"]["smb"]
            if s.get("name") != share_name
        ]
        
        self.config["shares"]["smb"].append(share_config)
        self.save()
    
    def get_smb_shares(self) -> List[Dict[str, Any]]:
        """Get all Samba share configurations"""
        return self.config["shares"]["smb"]
    
    def get_nfs_shares(self) -> List[Dict[str, Any]]:
        """Get all NFS share configurations"""
        return self.config["shares"]["nfs"]

    def add_nfs_share(self, share_config: Dict[str, Any]) -> None:
        """Add or update an NFS share"""
        share_name = share_config.get("name")
        if not share_name:
            raise ConfigurationException("NFS share must have 'name'")

        self.config["shares"]["nfs"] = [
            s for s in self.config["shares"]["nfs"]
            if s.get("name") != share_name
        ]

        self.config["shares"]["nfs"].append(share_config)
        self.save()

    def remove_nfs_share(self, name: str) -> None:
        """Remove an NFS share"""
        self.config["shares"]["nfs"] = [
            s for s in self.config["shares"]["nfs"]
            if s.get("name") != name
        ]
        self.save()

    # Updates configuration methods
    
    def is_topgrade_configured(self) -> bool:
        """Check if Topgrade is configured"""
        return self.config["updates"]["topgrade"]["configured"]
    
    def configure_topgrade(self, enabled_steps: List[str]) -> None:
        """Configure Topgrade settings"""
        self.config["updates"]["topgrade"]["configured"] = True
        self.config["updates"]["topgrade"]["configured_at"] = datetime.now().isoformat()
        self.config["updates"]["topgrade"]["enabled_steps"] = enabled_steps
        self.save()
    
    # Generic accessors
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by dot-notation key"""
        keys = key.split(".")
        value = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default
    
    def set(self, key: str, value: Any) -> None:
        """Set a configuration value by dot-notation key"""
        keys = key.split(".")
        config = self.config
        
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        config[keys[-1]] = value
        self.save()
    
    def to_dict(self) -> Dict[str, Any]:
        """Get the entire configuration as a dictionary"""
        return copy.deepcopy(self.config)
    
    def __repr__(self) -> str:
        """String representation"""
        return (
            f"ConfigurationManager(path={self.config_path}, "
            f"hostname={self.get_hostname()}, "
            f"edition={self.get_edition()})"
        )
