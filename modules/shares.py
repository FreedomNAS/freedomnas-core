"""
SharesModule — lifecycle and health monitoring for SMB and NFS services.

health_check() verifies that the enabled share services (smbd, nfsd) are active.
Service names are read from the OS profile via engine.get_profile_value() —
never hardcoded, satisfying the Golden Rule (spec §3.1).
"""

from __future__ import annotations

import logging
from typing import List

from freedomnas.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
from freedomnas.config import ConfigurationManager
from freedomnas.orchestrator import FreedomNASModule

logger = logging.getLogger("FreedomNAS.Module.Shares")


class SharesModule(FreedomNASModule):
    """Monitors SMB and NFS service health based on installed features."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)
        self._active_services: List[str] = []

    @property
    def name(self) -> str:
        return "shares"

    def start(self) -> None:
        """Resolve service names from profile for each enabled feature."""
        self._active_services = []
        feature_map = {
            "samba-cifs": ("samba", "smb_service"),
            "nfs":        ("nfs",   "nfs_service"),
        }
        for feature in self._config.get_installed_features():
            if not feature.get("enabled"):
                continue
            fid = feature.get("id", "")
            if fid in feature_map:
                category, key = feature_map[fid]
                try:
                    svc = self._engine.get_profile_value(category, key)
                    self._active_services.append(svc)
                except CommandTranslationError:
                    logger.warning("SharesModule: no profile value for %s.%s", category, key)

        logger.info(
            "SharesModule started — monitoring services: %s",
            self._active_services or "(none)",
        )

    def stop(self) -> None:
        self._active_services.clear()
        logger.info("SharesModule stopped")

    def health_check(self) -> bool:
        """
        Return True if every tracked service reports as active.
        Uses the `services.status` profile action.
        """
        if not self._active_services:
            return True  # nothing to check

        for service_name in self._active_services:
            try:
                self._engine.execute("services", "status", service_name=service_name)
            except CommandExecutionError as exc:
                logger.warning(
                    "SharesModule: service %s is not running: %s", service_name, exc
                )
                return False
            except CommandTranslationError as exc:
                logger.warning(
                    "SharesModule: no status action for service %s: %s", service_name, exc
                )

        return True
