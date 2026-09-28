"""
Growing the root pool to the disk it was flashed onto.

A FreedomNAS image is a few GB, and whatever disk it is written to is
usually larger: until the root pool's partition is extended to the end of
the disk and ZFS is told the device grew, the rest of the disk sits unused.

Only the simple case is touched — a root pool on a single partition, which
is what every image ships. A mirror, a raidz or a pool with more than one
device was laid out by someone, and is left as it is. Nothing here may stop
the boot: every failure is reported and the caller carries on.
"""

import logging
from typing import Dict, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

logger = logging.getLogger("FreedomNAS.Grow")

# Below this there is nothing worth growing into: partition alignment and
# the backup GPT already leave about a MiB free at the end of every disk.
MIN_GROWTH_BYTES = 64 * 1024 * 1024


def _single_device(listing: str) -> Optional[str]:
    """The pool's only device from `zpool list -vHPL`, or None if it has more."""
    children = [line.split("\t") for line in listing.splitlines()[1:] if line.strip()]
    if len(children) != 1:
        return None
    name = next((field for field in children[0] if field), "")
    return name if name.startswith("/dev/") else None


def grow_root_pool(engine) -> Dict[str, object]:
    """
    Extend the root pool's partition into the free space that follows it,
    then the pool.

    Returns {"pool", "device", "grown": bool, "reason": str}. Never raises.
    """
    result: Dict[str, object] = {"pool": None, "device": None, "grown": False, "reason": ""}
    try:
        root_dataset, _ = engine.execute("zfs", "dataset_at_path", path="/")
        pool = root_dataset.split("/", 1)[0]
        if not pool:
            result["reason"] = "the root filesystem is not on ZFS"
            return result
        result["pool"] = pool

        listing, _ = engine.execute("zfs", "pool_leaf_devices", pool_name=pool)
        device = _single_device(listing)
        if device is None:
            result["reason"] = "the root pool has more than one device"
            return result
        result["device"] = device

        info, _ = engine.execute("zfs", "partition_of", device=device)
        disk, part_num, start, size = info.split()
        disk_size, _ = engine.execute("zfs", "disk_size", disk=disk)
        offsets, _ = engine.execute("zfs", "partition_offsets", disk=disk)
        end = int(start) + int(size)
        following = [int(o) for o in offsets.split() if int(o) > int(start)]
        free = min(following, default=int(disk_size)) - end
        if free < MIN_GROWTH_BYTES:
            result["reason"] = ("another partition follows it" if following
                                else "the partition already reaches the end of the disk")
            return result

        logger.info("Growing %s (partition %s of %s) by %d bytes", device, part_num, disk, free)
        engine.execute("zfs", "partition_grow", disk=disk, part_num=part_num, timeout=120)
        engine.execute("zfs", "pool_expand", pool_name=pool, device=device, timeout=120)
    except (CommandExecutionError, CommandTranslationError, ValueError) as exc:
        logger.error("Could not grow the root pool: %s", exc)
        result["reason"] = str(exc)
        return result

    result["grown"] = True
    return result
