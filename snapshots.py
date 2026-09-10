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
            results.append(self._run_one(dataset, interval, keep, now))
        return results

    # ------------------------------------------------------------------

    def _run_one(self, dataset: str, interval: str, keep: int, now) -> Dict:
        name = snapshot_name(interval, now)
        result = {"dataset": dataset, "interval": interval, "created": None,
                  "destroyed": [], "error": None}
        try:
            self.engine.execute("zfs", "snapshot_create", dataset=dataset, snap_name=name)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Snapshot of %s failed: %s", dataset, exc)
            result["error"] = str(exc)
            return result

        result["created"] = f"{dataset}@{name}"
        logger.info("Created %s", result["created"])
        result["destroyed"] = self._prune(dataset, interval, keep)
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

    def _prune(self, dataset: str, interval: str, keep: int) -> List[str]:
        existing = self._ours(dataset, interval)
        destroyed = []
        for full_name in existing[:max(0, len(existing) - keep)]:
            snap = full_name.split("@", 1)[1]
            try:
                self.engine.execute("zfs", "snapshot_destroy", dataset=dataset, snap_name=snap)
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Could not destroy %s: %s", full_name, exc)
                continue
            logger.info("Pruned %s", full_name)
            destroyed.append(full_name)
        return destroyed
