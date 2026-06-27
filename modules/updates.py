"""
UpdatesModule — tracks update configuration state.

This module has no continuous running process to monitor; its health_check()
simply confirms that topgrade is configured when it is expected to be.
Updates are run on-demand via the WebUI terminal WebSocket.
"""

from __future__ import annotations

import logging

from freedomnas.engine import FreedomNASEngine
from freedomnas.config import ConfigurationManager
from freedomnas.orchestrator import FreedomNASModule

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

    def health_check(self) -> bool:
        """
        Always healthy — updates are on-demand and have no persistent service.
        Returns False only if the config itself is unreadable.
        """
        try:
            self._config.is_topgrade_configured()
            return True
        except Exception as exc:
            logger.warning("UpdatesModule health_check: config read failed: %s", exc)
            return False
