"""
FreedomNAS Module Loader — manifest-driven discovery and registration.

Scans a modules directory for `<name>/manifest.json` entries, loads each
module's entry-point class straight from its file path (not through Python's
package import machinery), and registers it with the Orchestrator. Loading
by file path means a module works identically whether it ships inside this
repo (dev) or was dropped into /usr/lib/freedomnas/modules/<name>/ by its own
package (system install, or a separately-packaged community module) — none
of that has to be an importable Python package.

manifest.json fields used here:
    id            str  — must match the containing directory name
    entry_point   str  — "<file_stem>:<ClassName>", e.g. "module:StorageModule".
                          Loads {module_dir}/{file_stem}.py and looks up ClassName.
    supported_os  list — module is skipped (not an error) if the current OS
                          isn't in this list.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import List, Optional

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule, Orchestrator
    from freedomnas import paths
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule, Orchestrator  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.ModuleLoader")


class ModuleLoaderException(Exception):
    """Raised when a module manifest is malformed or its entry point can't be loaded."""


class ModuleLoader:
    """Discovers modules/<name>/manifest.json entries and registers them."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager, modules_dir: Optional[str] = None):
        self._engine = engine
        self._config = config
        self.modules_dir = Path(modules_dir or self._get_default_modules_dir())

    @staticmethod
    def _get_default_modules_dir() -> str:
        """
        Mirrors FreedomNASEngine._get_default_profile_dir(): try the system
        install location first, then dev-tree fallbacks.
        """
        possible_paths = paths.modules_candidates()
        for path in possible_paths:
            if path.exists() and path.is_dir():
                logger.debug("Using modules directory: %s", path)
                return str(path)
        raise ModuleLoaderException(
            f"Could not find modules directory. Searched: "
            f"{', '.join(str(p) for p in possible_paths)}"
        )

    def discover(self) -> List[dict]:
        """Return the parsed manifest.json of every module directory found, in sorted order."""
        manifests = []
        for entry in sorted(self.modules_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue  # e.g. modules/_template/ — scaffolding, never a real module
            manifest_path = entry / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
            except json.JSONDecodeError as exc:
                raise ModuleLoaderException(f"Invalid JSON in {manifest_path}: {exc}")
            manifest["_dir"] = entry
            manifests.append(manifest)
        return manifests

    def _load_class(self, module_dir: Path, entry_point: str):
        """Load {module_dir}/{file_stem}.py by file path and return the named class."""
        try:
            file_stem, class_name = entry_point.split(":", 1)
        except ValueError:
            raise ModuleLoaderException(
                f"Malformed entry_point '{entry_point}' — expected '<file>:<ClassName>'"
            )

        file_path = module_dir / f"{file_stem}.py"
        if not file_path.exists():
            raise ModuleLoaderException(f"Entry-point file not found: {file_path}")

        spec = importlib.util.spec_from_file_location(
            f"freedomnas_module_{module_dir.name}_{file_stem}", file_path
        )
        if spec is None or spec.loader is None:
            raise ModuleLoaderException(f"Could not create import spec for {file_path}")
        py_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(py_module)

        try:
            return getattr(py_module, class_name)
        except AttributeError:
            raise ModuleLoaderException(f"{file_path} has no class '{class_name}'")

    def load_all(self, orchestrator: Orchestrator) -> List[str]:
        """
        Discover every module, skip those unsupported on the current OS,
        instantiate and register the rest with the orchestrator.

        Returns the list of module ids that were registered.
        """
        registered = []
        for manifest in self.discover():
            module_id = manifest.get("id", manifest["_dir"].name)
            supported_os = manifest.get("supported_os", [])
            if supported_os and self._engine.os_type not in supported_os:
                logger.info(
                    "Module '%s' skipped — not supported on '%s' (supports: %s)",
                    module_id, self._engine.os_type, supported_os,
                )
                continue

            entry_point = manifest.get("entry_point")
            if not entry_point:
                logger.warning("Module '%s' has no entry_point — skipping", module_id)
                continue

            module_class = self._load_class(manifest["_dir"], entry_point)
            instance: FreedomNASModule = module_class(self._engine, self._config)
            orchestrator.register(instance)
            registered.append(module_id)
            logger.info("Module '%s' loaded and registered", module_id)

        return registered
