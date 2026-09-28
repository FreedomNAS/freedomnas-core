"""
Pool composition: the devices that are not where the data lives.

A cache device (L2ARC) and a log device (SLOG) are attachments, not part of
the pool's redundancy. That has two consequences this module is built around:

  * they can be added to a pool that already exists, so the same code serves
    "choose an SSD while creating the pool" and "add one later";
  * losing one loses no data, so attaching them must never be able to take
    the pool down with it — a cache that fails to attach is reported, and the
    pool it was meant for is left exactly as it was.
"""

import logging
from typing import Dict, List, Optional, Sequence

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

logger = logging.getLogger("FreedomNAS.Pools")

ROLES = {"cache": "pool_add_cache", "log": "pool_add_log"}


def attach_devices(
    engine,
    pool_name: str,
    cache: Optional[Sequence[str]] = None,
    log: Optional[Sequence[str]] = None,
) -> Dict[str, List]:
    """
    Attach cache and/or log devices to an existing pool.

    Returns what went in and what did not. Never raises: the pool is already
    there, and a cache device that will not attach is a smaller problem than
    an exception unwinding the caller.
    """
    result: Dict[str, List] = {"cache": [], "log": [], "errors": []}
    for role, devices in (("cache", cache), ("log", log)):
        devices = [d for d in (devices or []) if d]
        if not devices:
            continue
        try:
            engine.execute(
                "zfs", ROLES[role], pool_name=pool_name, devices=" ".join(devices)
            )
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Could not attach %s to %s: %s", role, pool_name, exc)
            result["errors"].append({"role": role, "devices": devices, "error": str(exc)})
            continue
        logger.info("Attached %s device(s) to %s: %s", role, pool_name, " ".join(devices))
        result[role] = list(devices)
    return result


def detach_device(engine, pool_name: str, device: str) -> None:
    """
    Remove one attached device. Raises if ZFS refuses.

    `zpool remove` takes hot spares, cache, log and even some data vdevs —
    the refusal to unmake a pool's redundancy comes from ZFS itself, which is
    the right place for it to come from.
    """
    engine.execute("zfs", "pool_remove_device", pool_name=pool_name, device=device)
    logger.info("Removed %s from %s", device, pool_name)


SECTIONS = {"cache": "cache", "logs": "log"}


def parse_attached(listing: str) -> Dict[str, List[str]]:
    """
    The cache and log vdevs in `zpool list -vHP`. Their section headings
    ("cache", "logs") are the only lines that do not start with a tab; the
    vdevs one tab in are what `zpool remove` takes — a device, or the
    mirror-N of a mirrored log.
    """
    found: Dict[str, List[str]] = {"cache": [], "log": []}
    section = None
    for line in listing.splitlines()[1:]:
        if not line.strip():
            continue
        if not line.startswith("\t"):
            section = SECTIONS.get(line.split()[0])
            continue
        if section and not line.startswith("\t\t"):
            name = line.strip().split("\t")[0]
            if name:
                found[section].append(name)
    return found


def attached(engine, pool_name: str) -> Optional[Dict[str, List[str]]]:
    """What is attached to the pool as cache or log right now, or None if ZFS cannot say."""
    try:
        stdout, _ = engine.execute("zfs", "pool_leaf_devices", pool_name=pool_name, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return None
    if not stdout.strip():
        return None
    return parse_attached(stdout)
