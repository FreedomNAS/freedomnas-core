"""Read-only views of what ZFS is doing: the event log, per-vdev I/O and the ARC."""

import re
from typing import Dict, List, Optional


def parse_events(text: str, limit: int = 1000) -> List[Dict]:
    """`zpool events -H -v`, newest first."""
    events: List[Dict] = []
    current: Optional[Dict] = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            parts = line.split("\t")
            if len(parts) >= 2:
                current = {"time": parts[0].strip(), "class": parts[1].strip(), "fields": {}}
                events.append(current)
            continue
        if current is not None and " = " in line:
            key, _, value = line.strip().partition(" = ")
            current["fields"][key.strip()] = value.strip().strip('"')
    for event in events:
        cls = event["class"]
        event["error"] = cls.startswith("ereport.")
        event["short"] = cls.replace("sysevent.fs.zfs.", "").replace("ereport.fs.zfs.", "")
        event["pool"] = event["fields"].get("pool", "")
    events.reverse()
    return events[:limit]


def _num(value: str):
    return int(value) if value.isdigit() else None


def parse_iostat(text: str, pool: str) -> List[Dict]:
    """`zpool iostat -Hpvy <pool> 1 1`: the pool line, then one line per vdev."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        if not rows and parts[0] != pool:
            continue
        rows.append({"name": parts[0], "alloc": _num(parts[1]), "free": _num(parts[2]),
                     "read_ops": _num(parts[3]) or 0, "write_ops": _num(parts[4]) or 0,
                     "read_bytes": _num(parts[5]) or 0, "write_bytes": _num(parts[6]) or 0})
    return rows


_FREEBSD = re.compile(r"^kstat\.zfs\.misc\.arcstats\.([a-z0-9_]+):\s*(\d+)$")


def parse_arcstats(text: str) -> Dict[str, int]:
    """Linux /proc/spl/kstat/zfs/arcstats (`name type value`) or FreeBSD's sysctl form."""
    stats: Dict[str, int] = {}
    for line in (text or "").splitlines():
        m = _FREEBSD.match(line.strip())
        if m:
            stats[m.group(1)] = int(m.group(2))
            continue
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit() and parts[1].isdigit():
            stats[parts[0]] = int(parts[2])
    return stats


def arc_summary(stats: Dict[str, int]) -> Optional[Dict]:
    if "size" not in stats:
        return None
    hits, misses = stats.get("hits", 0), stats.get("misses", 0)
    l2_hits, l2_misses = stats.get("l2_hits", 0), stats.get("l2_misses", 0)
    return {
        "size": stats["size"], "target": stats.get("c"), "max": stats.get("c_max"),
        "min": stats.get("c_min"), "hits": hits, "misses": misses,
        "hit_ratio": round(100.0 * hits / (hits + misses), 1) if hits + misses else None,
        "l2_size": stats.get("l2_size", 0), "l2_hits": l2_hits, "l2_misses": l2_misses,
        "l2_hit_ratio": round(100.0 * l2_hits / (l2_hits + l2_misses), 1) if l2_hits + l2_misses else None,
    }
