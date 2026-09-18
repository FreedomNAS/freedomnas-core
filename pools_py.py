"""
Pool composition (pure Python): the devices that are not where the data lives.

A cache device (L2ARC) and a log device (SLOG) are attachments, not part of
the pool's redundancy. That has two consequences this module is built around:

  * they can be added to a pool that already exists, so the same code serves
    "choose an SSD while creating the pool" and "add one later";
  * losing one loses no data, so attaching them must never be able to take
    the pool down with it — a cache that fails to attach is reported, and the
    pool it was meant for is left exactly as it was.

The reference implementation the native one is verified against, and the
fallback when FREEDOMNAS_POOLS=python. See pools.py.
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
