"""
FreedomNAS Backup/Restore Manager — Handle configuration backups and disaster recovery.

Provides utilities to:
  • Save packages.yaml (tracks installed categories)
  • Backup configuration files by category
  • Restore packages and configs across editions
"""

import json
import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


class BackupManager:
    """Manage backups and restore of package configurations."""

    def __init__(self, backup_dir: Optional[str] = None, system_root: str = "/"):
        """
        Initialize backup manager.

        Args:
            backup_dir: Directory to store backups. Defaults to /var/backups/freedomnas/
            system_root: Root directory for system operations (for testing)
        """
        if backup_dir:
            self.backup_dir = Path(backup_dir)
        else:
            self.backup_dir = Path(system_root) / "var/backups/freedomnas"

        self.system_root = Path(system_root)
        self.packages_config_path = self.system_root / "etc/freedomnas/packages.yaml"

        # Create backup directory
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def save_packages_config(self, packages_config: Dict[str, Any]) -> Path:
        """
        Save packages.yaml configuration.

        Args:
            packages_config: Dict with installed categories and metadata

        Returns:
            Path to saved config file
        """
        config_path = self.system_root / "etc/freedomnas/packages.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to YAML-like format (simple key: value)
        with open(config_path, "w") as f:
            f.write("# FreedomNAS Installed Packages Configuration\n")
            f.write(f"# Generated: {datetime.now().isoformat()}\n\n")
            f.write(json.dumps(packages_config, indent=2))

        return config_path

    def load_packages_config(self) -> Optional[Dict[str, Any]]:
        """Load packages.yaml configuration."""
        config_path = self.system_root / "etc/freedomnas/packages.yaml"
        if not config_path.exists():
            return None

        with open(config_path) as f:
            # Skip comments
            lines = [l for l in f if not l.strip().startswith("#")]
            return json.loads("".join(lines))

    def backup_category_config(
        self,
        category_id: str,
        config_paths: List[str],
        timestamp: Optional[str] = None
    ) -> Path:
        """
        Backup configuration files for a category.

        Args:
            category_id: Category ID (e.g., 'samba-cifs')
            config_paths: List of absolute paths to backup
            timestamp: Custom timestamp (defaults to now)

        Returns:
            Path to the backup tarball
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        backup_file = self.backup_dir / f"{category_id}-{timestamp}.tar.gz"

        with tarfile.open(backup_file, "w:gz") as tar:
            for config_path in config_paths:
                full_path = self.system_root / config_path.lstrip("/")
                if full_path.exists():
                    # Add to tar with relative path (without system_root)
                    arcname = config_path.lstrip("/")
                    tar.add(full_path, arcname=arcname)

        return backup_file

    def restore_category_config(self, backup_file: Path) -> List[str]:
        """
        Restore configuration files from a backup tarball.

        Args:
            backup_file: Path to the .tar.gz backup file

        Returns:
            List of restored file paths
        """
        restored = []

        with tarfile.open(backup_file, "r:gz") as tar:
            for member in tar.getmembers():
                # Restore to system_root + original path
                extract_path = self.system_root / member.name
                extract_path.parent.mkdir(parents=True, exist_ok=True)

                # Extract file
                tar.extract(member, path=self.system_root)
                restored.append(f"/{member.name}")

        return restored

    def get_backups_for_category(self, category_id: str) -> List[Path]:
        """Get all backup files for a category, sorted by date (newest first)."""
        pattern = f"{category_id}-*.tar.gz"
        backups = sorted(
            self.backup_dir.glob(pattern),
            reverse=True
        )
        return backups

    def create_full_backup(
        self,
        packages_config: Dict[str, Any],
        category_configs: Dict[str, List[str]],
        backup_name: Optional[str] = None
    ) -> Path:
        """
        Create a complete system backup including packages.yaml and all configs.

        Args:
            packages_config: The packages.yaml content
            category_configs: Dict mapping category_id -> list of config paths
            backup_name: Optional name for backup (defaults to timestamp)

        Returns:
            Path to the complete backup directory
        """
        if backup_name is None:
            backup_name = datetime.now().strftime("freedomnas-backup-%Y-%m-%d_%H-%M-%S")

        backup_subdir = self.backup_dir / backup_name
        backup_subdir.mkdir(parents=True, exist_ok=True)

        # Save packages.yaml to backup
        with open(backup_subdir / "packages.yaml", "w") as f:
            f.write(json.dumps(packages_config, indent=2))

        # Backup each category's configs
        for category_id, config_paths in category_configs.items():
            if config_paths:
                backup_file = self.backup_category_config(
                    category_id,
                    config_paths,
                    timestamp=backup_name
                )
                # Move to backup subdir
                shutil.move(str(backup_file), str(backup_subdir / backup_file.name))

        return backup_subdir

    def restore_from_backup(
        self,
        backup_dir: Path,
        selected_categories: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Restore system from a complete backup.

        Args:
            backup_dir: Path to backup directory
            selected_categories: List of categories to restore. If None, restore all.

        Returns:
            Dict with restore results
        """
        results = {
            "restored_categories": [],
            "restored_files": [],
            "errors": []
        }

        # Load packages.yaml from backup
        packages_yaml_path = backup_dir / "packages.yaml"
        if packages_yaml_path.exists():
            with open(packages_yaml_path) as f:
                backup_config = json.load(f)

            # Determine which categories to restore
            if selected_categories is None:
                selected_categories = [
                    cat["id"] for cat in backup_config.get("installed_categories", [])
                ]

            # Restore each category's configs
            for category in backup_config.get("installed_categories", []):
                cat_id = category["id"]
                if cat_id not in selected_categories:
                    continue

                # Find backup file for this category
                backup_pattern = f"{cat_id}-{backup_dir.name}.tar.gz"
                backup_file = backup_dir / backup_pattern

                if backup_file.exists():
                    try:
                        restored = self.restore_category_config(backup_file)
                        results["restored_categories"].append(cat_id)
                        results["restored_files"].extend(restored)
                    except Exception as e:
                        results["errors"].append(f"Failed to restore {cat_id}: {e}")
                else:
                    results["errors"].append(f"No backup found for category {cat_id}")

        return results

    def cleanup_old_backups(self, category_id: str, keep_count: int = 5) -> List[Path]:
        """
        Remove old backups for a category, keeping only the N most recent.

        Args:
            category_id: Category ID
            keep_count: Number of backups to keep

        Returns:
            List of deleted backup file paths
        """
        backups = self.get_backups_for_category(category_id)
        deleted = []

        for backup_file in backups[keep_count:]:
            backup_file.unlink()
            deleted.append(backup_file)

        return deleted


# Example usage
if __name__ == "__main__":
    bm = BackupManager()

    # Example: Save packages config
    config = {
        "version": "4.0.0",
        "edition": "debian",
        "installed_categories": [
            {"id": "core", "status": "installed"},
            {"id": "samba-cifs", "status": "installed"}
        ]
    }

    try:
        config_path = bm.save_packages_config(config)
        print(f"✓ Saved packages config to {config_path}")

        loaded = bm.load_packages_config()
        print(f"✓ Loaded config: {loaded['edition']} with {len(loaded['installed_categories'])} categories")

    except Exception as e:
        print(f"Error: {e}")
