"""
FreedomNAS Package Manager — MANIFEST.json and category reading run in Nim.

Loading MANIFEST.json, resolving a category's JSON file, and every lookup
(packages/services/pkgbuilds for an edition, required/optional categories)
happen in fnas/packages.nim, against a package-manager handle opened once
per instance — no engine handle involved, since this is pure, read-only
JSON navigation. This file is the thin Python side of that boundary.

Selected by FREEDOMNAS_PACKAGES=native; see package_manager.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Packages")

_ERROR_CLASSES = {
    "os_error": FileNotFoundError,
    "value": ValueError,
}


def _unwrap(payload: str) -> Any:
    result = json.loads(payload)
    error = result.get("error")
    if error is None:
        return result["value"]
    kind = error.get("kind", "value")
    raise _ERROR_CLASSES.get(kind, ValueError)(error.get("message", ""))


class PackageManager:
    """Same public API as the pure-Python manager; JSON navigation runs in Nim."""

    def __init__(self, packages_dir: Optional[str] = None) -> None:
        if packages_dir:
            resolved = str(packages_dir)
        else:
            installed = Path("/usr/lib/freedomnas/profiles/packages")
            if installed.exists():
                resolved = str(installed)
            else:
                resolved = str(Path(__file__).parent.parent / "freedomnas-profiles" / "packages")

        self.packages_dir = Path(resolved)
        created = json.loads(_native.package_manager_create(resolved))
        error = created.get("error")
        if error is not None:
            kind = error.get("kind", "os_error")
            raise _ERROR_CLASSES.get(kind, FileNotFoundError)(error.get("message", ""))
        self._handle = created["handle"]

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            try:
                _native.package_manager_release(handle)
            except Exception:
                pass

    # ------------------------------------------------------------------

    def get_categories(self) -> List[Dict[str, Any]]:
        return _unwrap(_native.package_manager_categories(self._handle))

    def get_category_info(self, category_id: str) -> Dict[str, Any]:
        return _unwrap(_native.package_manager_category_info(self._handle, category_id))

    def get_packages_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        return _unwrap(_native.package_manager_packages(
            self._handle, category_id, edition, init_system or ""
        ))

    def get_pkgbuilds_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[Dict[str, str]]:
        return _unwrap(_native.package_manager_pkgbuilds(
            self._handle, category_id, edition, init_system or ""
        ))

    def get_services_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        return _unwrap(_native.package_manager_services(
            self._handle, category_id, edition, init_system or ""
        ))

    def get_early_services_for_os(
        self, category_id: str, edition: str, init_system: Optional[str] = None
    ) -> List[str]:
        return _unwrap(_native.package_manager_early_services(
            self._handle, category_id, edition, init_system or ""
        ))

    def get_config_paths(self, category_id: str, edition: str) -> List[str]:
        return _unwrap(_native.package_manager_config_paths(self._handle, category_id, edition))

    def get_all_packages_for_os(
        self, edition: str, categories: Optional[List[str]] = None
    ) -> Dict[str, List[str]]:
        if categories is None:
            categories = [
                c["id"] for c in self.get_categories() if not c.get("required", False)
            ]
        return {
            category_id: self.get_packages_for_os(category_id, edition)
            for category_id in categories
        }

    def get_required_categories(self) -> List[str]:
        return _unwrap(_native.package_manager_required_categories(self._handle))

    def get_optional_categories(self) -> List[str]:
        return _unwrap(_native.package_manager_optional_categories(self._handle))

    def validate_category(self, category_id: str) -> bool:
        return _unwrap(_native.package_manager_validate_category(self._handle, category_id))

    def validate_edition(self, edition: str) -> bool:
        return _unwrap(_native.package_manager_validate_edition(self._handle, edition))
