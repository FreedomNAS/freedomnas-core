"""
SharesModule — lifecycle and health monitoring for SMB and NFS services.

health_check() verifies that the enabled share services (smbd, nfsd) are active.
Service names are read from the OS profile via engine.get_profile_value() —
never hardcoded, satisfying the Golden Rule (spec §3.1).
"""

from __future__ import annotations

import logging
from typing import List, Tuple

try:
    from freedomnas.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Shares")

_FEATURES = {
    "samba-cifs": ("samba", "smb_service"),
    "nfs": ("nfs", "nfs_service"),
}


class SharesModule(FreedomNASModule):
    """Monitors SMB and NFS service health based on installed features."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)
        self._active_services: List[str] = []

    @property
    def name(self) -> str:
        return "shares"

    def _services(self) -> List[str]:
        """
        The services of the enabled share features, and of every kind of
        share the configuration holds: a restored or imaged system has shares
        but may record no installed features.
        """
        wanted = []
        for feature in self._config.get_installed_features():
            if feature.get("enabled") and feature.get("id") in _FEATURES:
                wanted.append(_FEATURES[feature["id"]])
        if self._config.get_smb_shares():
            wanted.append(_FEATURES["samba-cifs"])
        if self._config.get_nfs_shares():
            wanted.append(_FEATURES["nfs"])
        services: List[str] = []
        for category, key in wanted:
            try:
                svc = self._engine.get_profile_value(category, key)
            except CommandTranslationError:
                logger.warning("SharesModule: no profile value for %s.%s", category, key)
                continue
            if svc not in services:
                services.append(svc)
        return services

    def start(self) -> None:
        self._active_services = self._services()
        logger.info(
            "SharesModule started — monitoring services: %s",
            self._active_services or "(none)",
        )

    def stop(self) -> None:
        self._active_services.clear()
        logger.info("SharesModule stopped")

    def health(self) -> Tuple[bool, str]:
        """Every service the shares need answers `services.status`."""
        self._active_services = self._services()
        counts = [f"{n} {kind}" for n, kind in ((len(self._config.get_smb_shares()), "SMB"),
                                                 (len(self._config.get_nfs_shares()), "NFS")) if n]
        shares = (", ".join(counts) + " shares") if counts else "no shares"
        if not self._active_services:
            return True, f"{shares}, no share services enabled"

        stopped = []
        for service_name in self._active_services:
            try:
                self._engine.execute("services", "status", service_name=service_name)
            except CommandExecutionError as exc:
                logger.warning(
                    "SharesModule: service %s is not running: %s", service_name, exc
                )
                stopped.append(service_name)
            except CommandTranslationError as exc:
                logger.warning(
                    "SharesModule: no status action for service %s: %s", service_name, exc
                )
        if stopped:
            return False, f"{shares}, " + ", ".join(stopped) + " not running"
        return True, f"{shares}, " + ", ".join(self._active_services) + " running"

    def health_check(self) -> bool:
        return self.health()[0]
