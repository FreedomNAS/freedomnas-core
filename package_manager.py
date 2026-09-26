"""
FreedomNAS Package Manager — Load and manage OS-specific package definitions.

Provides utilities to:
  • Load package definitions from JSON category files
  • Get packages for a specific OS edition
  • Track installed categories via packages.yaml
  • Support disaster recovery across editions
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    from freedomnas import paths
except ImportError:  # running from the repository, not from the package
    import paths  # type: ignore[no-redef]


class PackageManager:
    """Manage OS-specific package definitions organized by category."""

    def __init__(self, packages_dir: Optional[str] = None):
        """
        Initialize package manager.

        Args:
            packages_dir: Path to packages directory. If None, uses installed location.
        """
        if packages_dir:
            self.packages_dir = Path(packages_dir)
        else:
            profiles = paths.profiles_dir() or paths.profiles_candidates()[0]
            self.packages_dir = profiles / "packages"

        if not self.packages_dir.exists():
            raise FileNotFoundError(f"Packages directory not found: {self.packages_dir}")

        self.manifest: Dict[str, Any] = {}
        self.categories: Dict[str, Dict[str, Any]] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        """Load MANIFEST.json to get list of categories."""
        manifest_path = self.packages_dir / "MANIFEST.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"MANIFEST.json not found: {manifest_path}")

        with open(manifest_path) as f:
            self.manifest = json.load(f)

    def _load_category(self, category_id: str) -> Dict[str, Any]:
        """Load a category JSON file."""
        if category_id in self.categories:
            return self.categories[category_id]

        # Find category in manifest
        cat_info = next(
            (c for c in self.manifest.get("categories", []) if c["id"] == category_id),
            None
        )
        if not cat_info:
            raise ValueError(f"Category not found in manifest: {category_id}")

        # Load category JSON
        cat_path = self.packages_dir / cat_info["file"]
        if not cat_path.exists():
            raise FileNotFoundError(f"Category file not found: {cat_path}")

        with open(cat_path) as f:
            category_data = json.load(f)

        self.categories[category_id] = category_data
        return category_data

    def get_categories(self) -> List[Dict[str, Any]]:
        """Get all available categories with metadata."""
        return self.manifest.get("categories", [])

    def get_category_info(self, category_id: str) -> Dict[str, Any]:
        """Get metadata for a specific category."""
        cats = self.get_categories()
        for cat in cats:
            if cat["id"] == category_id:
                return cat
        raise ValueError(f"Category not found: {category_id}")

    def get_packages_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        """
        Get package list for a category and OS edition.

        Args:
            category_id: Category ID (e.g., 'core', 'samba-cifs')
            edition: OS edition ('debian', 'freebsd', 'arch')
            init_system: Adds the edition's `init.<init_system>` packages, for
                         editions that ship their service scripts separately
                         per init (Artix's openssh-openrc, openssh-dinit, ...)

        Returns:
            List of package names for that OS
        """
        if edition not in self.manifest.get("supported_editions", []):
            raise ValueError(f"Unsupported edition: {edition}")
        return self._edition_values(category_id, edition, "packages", init_system)

    def get_pkgbuilds_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """Get the {name, git} PKGBUILD repositories a category builds after its packages."""
        return self._edition_values(category_id, edition, "pkgbuilds", init_system)

    def get_admin_groups_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        """
        Get the groups the administrator has to belong to for a category's
        own tools to work — Docker's socket is owned by `docker`, and the
        update run reports a failed step without it.
        """
        return self._edition_values(category_id, edition, "admin_groups", init_system)

    def get_services_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        """Get services to enable for a category and OS edition."""
        return self._edition_values(category_id, edition, "services", init_system)

    def get_early_services_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        """
        Get services that must come up before everything else — importing and
        mounting pools — enabled through the init profile's `enable_early`
        where it has one (OpenRC's sysinit runlevel).
        """
        return self._edition_values(category_id, edition, "early_services", init_system)

    def _edition_values(
        self, category_id: str, edition: str, key: str, init_system: Optional[str]
    ) -> List[Any]:
        category = self._load_category(category_id)
        if edition not in category:
            raise ValueError(f"Edition '{edition}' not supported in category '{category_id}'")
        entry = category[edition]
        values = list(entry.get(key, []))
        if init_system:
            values += entry.get("init", {}).get(init_system, {}).get(key, [])
        return values

    def get_config_paths(self, category_id: str, edition: str) -> List[str]:
        """Get configuration file paths for backup/restore."""
        category = self._load_category(category_id)
        if edition not in category:
            raise ValueError(f"Edition '{edition}' not supported in category '{category_id}'")
        return category[edition].get("config_paths", [])

    def get_all_packages_for_os(self, edition: str, categories: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """
        Get packages for multiple categories.

        Args:
            edition: OS edition
            categories: List of category IDs. If None, returns all non-required categories.

        Returns:
            Dict mapping category_id -> list of packages
        """
        if categories is None:
            # Get all optional categories by default
            categories = [
                c["id"] for c in self.get_categories()
                if not c.get("required", False)
            ]

        result = {}
        for cat_id in categories:
            result[cat_id] = self.get_packages_for_os(cat_id, edition)
        return result

    def get_required_categories(self) -> List[str]:
        """Get list of required category IDs."""
        return [c["id"] for c in self.get_categories() if c.get("required", False)]

    def get_optional_categories(self) -> List[str]:
        """Get list of optional category IDs."""
        return [c["id"] for c in self.get_categories() if not c.get("required", False)]

    def validate_category(self, category_id: str) -> bool:
        """Check if a category exists and is valid."""
        try:
            self._load_category(category_id)
            return True
        except (ValueError, FileNotFoundError):
            return False

    def validate_edition(self, edition: str) -> bool:
        """Check if an edition is supported."""
        return edition in self.manifest.get("supported_editions", [])


# Example usage in installer/WebUI
if __name__ == "__main__":
    try:
        pm = PackageManager()

        print("Available Categories:")
        for cat in pm.get_categories():
            print(f"  {cat['id']}: {cat['name']} ({'required' if cat.get('required') else 'optional'})")

        print("\nCore packages for Debian:")
        core_packages = pm.get_packages_for_os("core", "debian")
        for pkg in core_packages[:5]:
            print(f"  - {pkg}")
        print(f"  ... and {len(core_packages) - 5} more")

        print("\nRequired categories:", pm.get_required_categories())
        print("Optional categories:", pm.get_optional_categories())

    except Exception as e:
        print(f"Error: {e}")
