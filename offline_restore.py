"""
The part of a restore a system can take before it ever runs: the installer
calls it inside the chroot of the system it installs, with a data pool's
copy of the configuration (config.yaml and identity/, as `.freedomnas`
keeps them) in `source`.

It puts the NAS's identity back (certificate authority, SSH host keys),
installs the packages of the modules the configuration has sections for,
and lets each module install what its section needs (`prepare`). Nothing
is started and no pool is touched, and config.yaml is not written: on the
first boot `state-boot` restores the configuration from the data pool as
for any reinstall, and the reconcile finds the packages already there.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List

import yaml

try:
    from freedomnas.config import ConfigurationManager
    from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
    from freedomnas.reconcile import ActionResult, install_missing_modules
    from freedomnas import shadow
except ImportError:  # running from the repository, not from the package
    from config import ConfigurationManager  # type: ignore[no-redef]
    from module_loader import ModuleLoader, ModuleLoaderException  # type: ignore[no-redef]
    from reconcile import ActionResult, install_missing_modules  # type: ignore[no-redef]
    import shadow  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.OfflineRestore")

CONFIG_NAME = "config.yaml"


class OfflineRestoreError(Exception):
    """The source holds no configuration to restore."""


def restore(engine, source: Path) -> Dict[str, List]:
    """
    Identity, module packages and what the modules prepare, from the copy in
    `source`. Returns {"identity": [...], "results": [ActionResult, ...]}.
    """
    config_file = source / CONFIG_NAME
    if not config_file.is_file():
        raise OfflineRestoreError(f"no {CONFIG_NAME} in {source}")
    data = yaml.safe_load(config_file.read_text()) or {}
    sections = {module_id: section for module_id, section in (data.get("modules") or {}).items()
                if isinstance(section, dict) and section}

    identity = shadow.restore_identity(engine, source, restart_ssh=False)

    with tempfile.TemporaryDirectory(prefix="freedomnas-offline-") as scratch:
        copy = Path(scratch) / CONFIG_NAME
        shutil.copyfile(config_file, copy)
        config = ConfigurationManager(config_path=str(copy))
        results = install_missing_modules(engine, config, sections)
        if any(r.action == "modules.install" and r.target_state == "applied" for r in results):
            try:
                engine.reload_profile()
            except Exception as exc:
                logger.error("Profiles not loaded again after installing modules: %s", exc)
        results.extend(_prepare_modules(engine, config, sections))
    return {"identity": identity, "results": results}


def _prepare_modules(engine, config, sections: Dict[str, dict]) -> List[ActionResult]:
    try:
        pairs = ModuleLoader(engine, config).instances()
    except ModuleLoaderException as exc:
        return [ActionResult(action="modules.prepare", params={}, target_state="failed", error=str(exc))]
    results: List[ActionResult] = []
    for manifest, module in pairs:
        module_id = manifest.get("id", module.name)
        section = sections.get(module_id)
        if not section:
            continue
        try:
            results.extend(module.prepare(section) or [])
        except Exception as exc:
            logger.error("Module %s could not prepare: %s", module_id, exc)
            results.append(ActionResult(action=f"modules.{module_id}.prepare", params={},
                                        target_state="failed", error=str(exc)))
    return results
