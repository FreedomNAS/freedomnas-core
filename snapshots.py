"""
Scheduled ZFS snapshots with retention.

Cron decides *when* — one entry per interval, from the profile — and this
decides *what*: for every dataset with a policy, take a snapshot and then
prune that interval down to the number of copies the policy keeps.

The one rule that matters: **only snapshots this scheduler created are ever
destroyed**. They carry a name of their own, so a snapshot taken by hand, by
the update terminal (`pre-update-*`) or by anything else can never be pruned
by a retention count someone lowered.
"""

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

logger = logging.getLogger("FreedomNAS.Snapshots")

PREFIX = "freedomnas"
INTERVALS = ("hourly", "daily", "weekly", "monthly")


def snapshot_name(interval: str, when: Optional[datetime] = None) -> str:
    """
    `freedomnas-hourly-20260910-030000`.

    The timestamp is last and fixed-width, so sorting these names
    alphabetically sorts them by age — which is how retention finds the
    oldest without asking ZFS for creation times.
    """
    when = when or datetime.now()
    return f"{PREFIX}-{interval}-{when:%Y%m%d-%H%M%S}"


class SnapshotScheduler:
    """Takes and prunes the snapshots described by `snapshots.policies`."""

    def __init__(self, engine, config) -> None:
        self.engine = engine
        self.config = config

    # ------------------------------------------------------------------

    def run(self, interval: str, now: Optional[datetime] = None) -> List[Dict]:
        """Apply every policy that asks for this interval. Never raises."""
        if interval not in INTERVALS:
            raise ValueError(f"Unknown interval: {interval}")

        results = []
        for policy in self.config.get_snapshot_policies():
            dataset = (policy.get("dataset") or "").strip()
            try:
                keep = int(policy.get(interval, 0) or 0)
            except (TypeError, ValueError):
                keep = 0
            if not dataset or keep <= 0:
                continue
            results.append(self._run_one(dataset, interval, keep, now, policy.get("recursive") is True))
        return results

    # ------------------------------------------------------------------

    def _run_one(self, dataset: str, interval: str, keep: int, now, recursive: bool = False) -> Dict:
        """
        A recursive policy snapshots the dataset and every dataset inside it
        at the same moment, under the same name, and prunes them together.
        """
        name = snapshot_name(interval, now)
        suffix = "_recursive" if recursive else ""
        result = {"dataset": dataset, "interval": interval, "created": None,
                  "destroyed": [], "error": None}
        try:
            self.engine.execute("zfs", "snapshot_create" + suffix, dataset=dataset, snap_name=name)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Snapshot of %s failed: %s", dataset, exc)
            result["error"] = str(exc)
            return result

        result["created"] = f"{dataset}@{name}"
        logger.info("Created %s", result["created"])
        result["destroyed"] = self._prune(dataset, interval, keep, suffix)
        return result

    def _ours(self, dataset: str, interval: str) -> List[str]:
        """Our snapshots of this dataset and interval, oldest first."""
        try:
            stdout, _ = self.engine.execute("zfs", "snapshot_list", check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Cannot list snapshots: %s", exc)
            return []

        wanted = f"{dataset}@{PREFIX}-{interval}-"
        names = [
            line.split("\t")[0]
            for line in stdout.splitlines()
            if line.split("\t")[0].startswith(wanted)
        ]
        return sorted(names)

    def _prune(self, dataset: str, interval: str, keep: int, suffix: str = "") -> List[str]:
        existing = self._ours(dataset, interval)
        destroyed = []
        for full_name in existing[:max(0, len(existing) - keep)]:
            snap = full_name.split("@", 1)[1]
            try:
                self.engine.execute("zfs", "snapshot_destroy" + suffix, dataset=dataset, snap_name=snap)
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Could not destroy %s: %s", full_name, exc)
                continue
            logger.info("Pruned %s", full_name)
            destroyed.append(full_name)
        return destroyed


# ---------------------------------------------------------------------------
# Snapshots by hand: names, listings, holds, changes
# ---------------------------------------------------------------------------

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")


def split_name(full: str) -> tuple:
    """`pool/ds@snap` -> (`pool/ds`, `snap`). Raises ValueError for anything else."""
    if not isinstance(full, str) or full.count("@") != 1 or len(full) > 255:
        raise ValueError(f"{full!r} is not a snapshot name (dataset@name)")
    dataset, snap = full.split("@")
    if not dataset or not all(_PART.match(p) for p in dataset.split("/")) or not _PART.match(snap):
        raise ValueError(f"{full!r} is not a snapshot name (dataset@name)")
    return dataset, snap


def check_part(name: str, what: str = "snapshot") -> str:
    if not isinstance(name, str) or not _PART.match(name) or len(name) > 200:
        raise ValueError(f"A {what} name starts with a letter or digit and holds letters, digits, _ . : -")
    return name


def check_tag(tag: str) -> str:
    if not isinstance(tag, str) or not _TAG.match(tag) or len(tag) > 200:
        raise ValueError("A hold tag starts with a letter or digit and holds letters, digits, _ . : + -")
    return tag


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_list(text: str) -> List[Dict]:
    """`zfs list -t snapshot -Hp -o name,creation,used,referenced,userrefs,clones`."""
    found = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or "@" not in parts[0]:
            continue
        dataset, snap = parts[0].split("@", 1)
        entry = {"name": parts[0], "dataset": dataset, "snapshot": snap,
                 "creation": _int(parts[1]) if _int(parts[1]) is not None else parts[1],
                 "used": _int(parts[2]) if _int(parts[2]) is not None else parts[2]}
        if len(parts) >= 6:
            entry["referenced"] = _int(parts[3])
            entry["holds"] = _int(parts[4]) or 0
            entry["clones"] = [c for c in parts[5].split(",") if c and c != "-"]
        found.append(entry)
    return found


def parse_bookmarks(text: str) -> List[Dict]:
    found = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "#" in parts[0]:
            dataset, mark = parts[0].split("#", 1)
            found.append({"name": parts[0], "dataset": dataset, "bookmark": mark,
                          "creation": _int(parts[1])})
    return found


def parse_holds(text: str) -> List[Dict]:
    found = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            found.append({"tag": parts[1], "time": _int(parts[2])})
    return found


DIFF_CHANGES = {"-": "removed", "+": "created", "M": "modified", "R": "renamed"}
DIFF_TYPES = {"F": "file", "/": "directory", "@": "symlink", "|": "pipe", "=": "socket",
              "B": "block device", "C": "character device", ">": "door", "P": "event port"}


def _unescape(path: str) -> str:
    raw = re.sub(r"\\0*([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)
    try:
        return raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return raw


def parse_diff(text: str, limit: int = 5000) -> Dict:
    """`zfs diff -FHt`: what changed between a snapshot and a later one or the live dataset."""
    changes, total = [], 0
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        total += 1
        if len(changes) >= limit:
            continue
        entry = {"change": DIFF_CHANGES.get(parts[1], parts[1]),
                 "type": DIFF_TYPES.get(parts[2], parts[2]), "path": _unescape(parts[3])}
        if len(parts) >= 5:
            entry["new_path"] = _unescape(parts[4])
        changes.append(entry)
    return {"changes": changes, "total": total, "truncated": total > len(changes)}
