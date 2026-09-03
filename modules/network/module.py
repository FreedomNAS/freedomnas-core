"""
NetworkModule — lifecycle and health monitoring for network interfaces.

health_check() verifies that at least one non-loopback interface is up.
Persistent interface mappings are read from the config at start time.
"""

from __future__ import annotations

import logging

try:
    from freedomnas.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Network")


class NetworkModule(FreedomNASModule):
    """Monitors network interface availability."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)

    @property
    def name(self) -> str:
        return "network"

    def start(self) -> None:
        configured = self._config.get_interfaces()
        logger.info(
            "NetworkModule started — %d interface(s) in config",
            len(configured),
        )

    def stop(self) -> None:
        logger.info("NetworkModule stopped")

    def health_check(self) -> bool:
        """
        Return True if at least one non-loopback interface reports as up.
        Uses list_interfaces action, which lists active Ethernet/Wi-Fi interfaces.
        """
        try:
            output = self._engine.execute("network", "list_interfaces")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("NetworkModule health_check: list_interfaces failed: %s", exc)
            return False

        interfaces = output.strip().split() if output else []
        # Filter loopback
        active = [i for i in interfaces if not i.startswith("lo")]
        if not active:
            logger.warning("NetworkModule: no active non-loopback interfaces found")
            return False

        return True
