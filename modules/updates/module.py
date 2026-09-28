"""
UpdatesModule — tracks update configuration state.

This module has no continuous running process to monitor; its health_check()
simply confirms that topgrade is configured when it is expected to be.
Updates are run on-demand via the WebUI terminal WebSocket.
"""

from __future__ import annotations

import logging
from typing import Tuple

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Updates")


class UpdatesModule(FreedomNASModule):
    """Monitors update configuration consistency."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)

    @property
    def name(self) -> str:
        return "updates"

    def start(self) -> None:
        configured = self._config.is_topgrade_configured()
        logger.info(
            "UpdatesModule started — topgrade configured: %s", configured
        )

    def stop(self) -> None:
        logger.info("UpdatesModule stopped")

    def health(self) -> Tuple[bool, str]:
        """
        Updates run on demand and keep no service: the module is healthy as
        long as its configuration can be read.
        """
        try:
            configured = self._config.is_topgrade_configured()
        except Exception as exc:
            logger.warning("UpdatesModule health_check: config read failed: %s", exc)
            return False, f"configuration unreadable: {exc}"
        return True, "topgrade configured" if configured else "topgrade not configured yet"

    def health_check(self) -> bool:
        return self.health()[0]
