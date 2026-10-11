"""
FreedomNAS Module Loader — manifest-driven discovery and registration.

Scans the modules directories for `<name>/manifest.json` entries, loads each
module's entry-point class straight from its file path (not through Python's
package import machinery), and registers it with the Orchestrator. Loading
by file path means a module works identically whether it ships inside this
repo (dev) or was dropped into /usr/lib/freedomnas/modules/<name>/ by its own
package (system install, or a separately-packaged community module) — none
of that has to be an importable Python package.

The core modules live inside the core (<root>/core/freedomnas/modules) and
community modules beside it (<root>/modules): both are scanned, core first,
and a community module cannot take the id of a core one.

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
        self.modules_dirs = [Path(modules_dir)] if modules_dir else self._get_default_modules_dirs()

    @staticmethod
    def _get_default_modules_dirs() -> List[Path]:
        found = [p for p in paths.modules_candidates() if p.is_dir()]
        if not found:
            raise ModuleLoaderException(
                f"Could not find modules directory. Searched: "
                f"{', '.join(str(p) for p in paths.modules_candidates())}"
            )
        logger.debug("Using modules directories: %s", ", ".join(map(str, found)))
        return found

    def discover(self) -> List[dict]:
        """Return the parsed manifest.json of every module directory found, core modules first."""
        manifests = []
        seen = set()
        for base in self.modules_dirs:
            for entry in sorted(base.iterdir()):
                if not entry.is_dir() or entry.name.startswith(("_", ".")):
                    continue
                manifest_path = entry / "manifest.json"
                if not manifest_path.exists():
                    continue
                try:
                    with open(manifest_path, "r") as f:
                        manifest = json.load(f)
                except json.JSONDecodeError as exc:
                    raise ModuleLoaderException(f"Invalid JSON in {manifest_path}: {exc}")
                module_id = manifest.get("id", entry.name)
                if module_id in seen:
                    logger.warning("Module '%s' in %s ignored: the id is already taken", module_id, base)
                    continue
                seen.add(module_id)
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

    def instances(self) -> List[tuple]:
        """
        Discover every module, skip those unsupported on the current OS, and
        instantiate the rest. Returns (manifest, instance) pairs, unstarted.
        """
        loaded = []
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
            loaded.append((manifest, instance))
        return loaded

    def load_all(self, orchestrator: Orchestrator) -> List[str]:
        """
        Instantiate every module this OS supports and register it with the
        orchestrator. Returns the list of module ids that were registered.
        """
        registered = []
        for manifest, instance in self.instances():
            module_id = manifest.get("id", manifest["_dir"].name)
            orchestrator.register(instance)
            registered.append(module_id)
            logger.info("Module '%s' loaded and registered", module_id)

        return registered
