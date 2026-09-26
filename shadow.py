"""
The configuration's shadow copy on every pool that carries one.

`config.yaml` is a backup of the system state, not the source of truth — and
a backup that lives only on the OS disk is no backup at all. When the disk
that holds the root pool dies, what survives is the data pool, and that is
where the appliance's identity has to be waiting: hostname, users, shares,
snapshot policies.

So every pool gets a `<pool>/.freedomnas` dataset, and the configuration is
mirrored into each of them. `firstboot.sh` looks for exactly that dataset
when it comes up, which is what makes a reinstall onto a blank disk find its
old self on the data pool.

All the copies mount at the same place (<config_dir>/state), only one at a
time — which is why mirroring mounts them somewhere else for a moment, the
same way the initramfs mounts the root dataset at /sysroot.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas import paths
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore
    import paths  # type: ignore

logger = logging.getLogger("FreedomNAS.Shadow")

DATASET_NAME = ".freedomnas"
CONFIG_NAME = "config.yaml"


def _pools(engine) -> List[str]:
    try:
        stdout, _ = engine.execute("zfs", "pool_list", check=False)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.error("Cannot list pools: %s", exc)
        return []
    return [line.split()[0] for line in stdout.splitlines() if line.split()]


def _exists(engine, dataset: str) -> bool:
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=dataset, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return False
    return stdout.strip() == dataset


def active_dataset(engine) -> Optional[str]:
    """The `.freedomnas` currently mounted at <config_dir>/state, if any."""
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=str(paths.state_dir(engine)), check=False)
    except (CommandExecutionError, CommandTranslationError):
        return None
    name = stdout.strip()
    return name if name.endswith(f"/{DATASET_NAME}") else None


def ensure_dataset(engine, pool: str) -> bool:
    """
    Give a pool its shadow dataset. Returns True if it had to be created.

    Called when a pool is created, so that a data pool is ready to hold the
    configuration from the moment it exists.
    """
    dataset = f"{pool}/{DATASET_NAME}"
    if _exists(engine, dataset):
        return False
    engine.execute("zfs", "create_state_dataset", pool_name=pool)
    logger.info("Created shadow dataset %s", dataset)
    return True


def sync(engine, config_path: str) -> List[Dict]:
    """
    Copy the live configuration into every other pool's shadow dataset.

    The pool the configuration is already served from is skipped: it is the
    source. Never raises — a pool that cannot be mirrored is reported and the
    others still get their copy.
    """
    source = Path(config_path)
    if not source.is_file():
        logger.warning("No configuration at %s — nothing to mirror", source)
        return []

    active = active_dataset(engine)
    results = []
    for pool in _pools(engine):
        dataset = f"{pool}/{DATASET_NAME}"
        if dataset == active or not _exists(engine, dataset):
            continue
        results.append(_copy_into(engine, dataset, source))
    return results


def _copy_into(engine, dataset: str, source: Path) -> Dict:
    result = {"dataset": dataset, "copied": False, "error": None}
    mountpoint = tempfile.mkdtemp(prefix="freedomnas-shadow.")
    try:
        engine.execute("zfs", "mount_dataset_at", dataset=dataset, mountpoint=mountpoint)
    except (CommandExecutionError, CommandTranslationError) as exc:
        result["error"] = f"could not mount: {exc}"
        Path(mountpoint).rmdir()
        return result

    try:
        shutil.copy2(source, Path(mountpoint) / CONFIG_NAME)
        result["copied"] = True
        logger.info("Mirrored the configuration to %s", dataset)
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        try:
            engine.execute("zfs", "unmount_at", mountpoint=mountpoint, check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Left %s mounted at %s: %s", dataset, mountpoint, exc)
        try:
            Path(mountpoint).rmdir()
        except OSError:
            pass
    return result
