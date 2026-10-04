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

import json
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


VDEV_CLASSES = ("data", "special", "dedup", "spare")
VDEV_LAYOUTS = ("", "mirror", "raidz1", "raidz2", "raidz3")
MIN_DEVICES = {"": 1, "mirror": 2, "raidz1": 2, "raidz2": 3, "raidz3": 4}


def vdev_spec(groups: Sequence[Dict]) -> List[str]:
    """
    The words zpool create/add take for a list of vdev groups, each
    {"class": data|special|dedup|spare, "type": ""|mirror|raidz1-3, "devices": [...]}.

    Raises ValueError for a layout zpool would refuse or that makes no sense:
    special and dedup vdevs are single disks or mirrors, spares are never grouped.
    """
    if not groups:
        raise ValueError("No vdevs given")
    words: List[str] = []
    seen = set()
    for group in groups:
        cls = group.get("class") or "data"
        layout = group.get("type") or ""
        devices = [str(d) for d in group.get("devices") or [] if str(d)]
        if cls not in VDEV_CLASSES:
            raise ValueError(f"Unknown vdev class: {cls!r}")
        if layout not in VDEV_LAYOUTS:
            raise ValueError(f"Unknown vdev layout: {layout!r}")
        if not devices:
            raise ValueError(f"A {cls} vdev needs at least one device")
        for device in devices:
            if device in seen:
                raise ValueError(f"{device} is listed twice")
            seen.add(device)
        if cls == "spare":
            words += ["spare", *devices]
            continue
        if cls in ("special", "dedup") and layout not in ("", "mirror"):
            raise ValueError(f"A {cls} vdev is a single disk or a mirror")
        if len(devices) < MIN_DEVICES[layout]:
            raise ValueError(f"{layout} needs at least {MIN_DEVICES[layout]} devices")
        if cls != "data":
            words.append(cls)
        if layout:
            words.append(layout)
        words += devices
    return words


STATUS_GROUPS = (("vdevs", "data"), ("special", "special"), ("dedup", "dedup"),
                 ("logs", "log"), ("l2cache", "cache"), ("spares", "spare"))


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _vdev(raw: Dict, depth: int, parent_type: str) -> Dict:
    node = {
        "name": raw.get("name", ""),
        "path": raw.get("path", ""),
        "type": raw.get("vdev_type", ""),
        "state": raw.get("state", ""),
        "read_errors": int(raw.get("read_errors") or 0),
        "write_errors": int(raw.get("write_errors") or 0),
        "checksum_errors": int(raw.get("checksum_errors") or 0),
        "size": raw.get("total_space") or raw.get("phys_space"),
        "depth": depth,
        "parent_type": parent_type,
        "children": [],
    }
    for child in (raw.get("vdevs") or {}).values():
        node["children"].append(_vdev(child, depth + 1, node["type"]))
    return node


def parse_status(text: str, pool_name: str) -> Optional[Dict]:
    """
    `zpool status -j --json-int -P` for one pool, reduced to what the WebUI
    shows: state, ZFS's own problem/action text, scrub or resilver progress
    and the vdev tree grouped by role.
    """
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return None
    raw = (data.get("pools") or {}).get(pool_name)
    if raw is None:
        return None
    scan = raw.get("scan_stats") or {}
    total = int(scan.get("to_examine") or 0)
    done = int(scan.get("issued") or scan.get("examined") or 0)
    result = {
        "name": pool_name,
        "state": raw.get("state", ""),
        "status": _one_line(raw.get("status")),
        "action": _one_line(raw.get("action")),
        "error_count": int(raw.get("error_count") or 0),
        "scan": {
            "function": scan.get("function", ""),
            "state": scan.get("state", ""),
            "start_time": scan.get("start_time"),
            "end_time": scan.get("end_time"),
            "errors": int(scan.get("errors") or 0),
            "total": total,
            "done": done,
            "percent": round(100.0 * done / total, 1) if total else None,
            "paused": bool(scan.get("scrub_pause")),
        } if scan else None,
        "removing": (raw.get("removal_stats") or {}).get("state") == "SCANNING",
        "checkpoint": (raw.get("checkpoint_stats") or {}).get("state") == "EXISTS",
        "groups": [],
    }
    for key, role in STATUS_GROUPS:
        vdevs = raw.get(key)
        if not vdevs:
            continue
        if key == "vdevs":
            vdevs = next(iter(vdevs.values())).get("vdevs") or {}
        result["groups"].append({
            "role": role,
            "vdevs": [_vdev(v, 0, "") for v in vdevs.values()],
        })
    return result


def status(engine, pool_name: str) -> Optional[Dict]:
    stdout, _ = engine.execute("zfs", "pool_status_json", pool_name=pool_name)
    return parse_status(stdout, pool_name)


def parse_properties(text: str) -> Dict[str, Dict[str, str]]:
    """`zpool get -Hp -o property,value,source all`: name -> value and source."""
    found = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            found[parts[0]] = {"value": parts[1], "source": parts[2]}
    return found
