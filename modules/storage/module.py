"""
StorageModule — lifecycle and health monitoring for ZFS pools.

health_check() parses `zpool list` output to detect DEGRADED or FAULTED pools.
A single unhealthy pool is enough to fail the check; the Orchestrator's circuit
breaker will disable the module after MAX_FAILURES consecutive failures.
"""

from __future__ import annotations

import logging
from typing import List

try:
    from freedomnas.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Storage")

_UNHEALTHY_STATES = {"DEGRADED", "FAULTED", "REMOVED", "UNAVAIL"}


class StorageModule(FreedomNASModule):
    """Monitors ZFS pool health and surfaces degraded/faulted pools."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)
        self._known_pools: List[str] = []

    @property
    def name(self) -> str:
        return "storage"

    def start(self) -> None:
        """Populate known pool list from config at startup."""
        self._known_pools = [p["name"] for p in self._config.get_pools()]
        logger.info("StorageModule started — tracking pools: %s", self._known_pools)

    def stop(self) -> None:
        self._known_pools.clear()
        logger.info("StorageModule stopped")

    def health_check(self) -> bool:
        """
        Run zpool list and check that no tracked pool is in an unhealthy state.
        Returns True only when all known pools report ONLINE.
        """
        try:
            stdout, _ = self._engine.execute("zfs", "pool_list")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("StorageModule health_check: pool_list failed: %s", exc)
            return False

        # `zpool list -Hp -o name,size,alloc,free,capacity,health`: the
        # profile names its columns, so health is the sixth and last one.
        # Left to zpool's default order it moved between OpenZFS releases.
        for line in stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            pool_name = parts[0]
            health = parts[5].upper()
            if pool_name in self._known_pools and health in _UNHEALTHY_STATES:
                logger.error("Pool %s is in state: %s", pool_name, health)
                return False

        return True
