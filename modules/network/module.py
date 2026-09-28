"""
NetworkModule — lifecycle and health monitoring for network interfaces.

health_check() verifies that at least one non-loopback interface is up.
Persistent interface mappings are read from the config at start time.
"""

from __future__ import annotations

import json
import logging
from typing import Tuple

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

    def health(self) -> Tuple[bool, str]:
        """
        At least one interface other than loopback is up. Linux profiles
        list interfaces as `ip -j addr show` JSON, with their state; FreeBSD's
        `ifconfig -l -ether` gives bare names, and a name there means an
        Ethernet interface exists.
        """
        try:
            stdout, _ = self._engine.execute("network", "list_interfaces")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("NetworkModule health_check: list_interfaces failed: %s", exc)
            return False, f"cannot list interfaces: {exc}"

        try:
            listed = json.loads(stdout)
        except ValueError:
            listed = None
        if isinstance(listed, list):
            up = [
                i.get("ifname", "") for i in listed
                if isinstance(i, dict)
                and not i.get("ifname", "lo").startswith("lo")
                and (i.get("operstate") == "UP" or "UP" in i.get("flags", []) and "LOWER_UP" in i.get("flags", []))
            ]
        else:
            up = [name for name in stdout.split() if not name.startswith("lo")]

        if not up:
            logger.warning("NetworkModule: no active non-loopback interfaces found")
            return False, "no interface is up"
        return True, ", ".join(up) + " up"

    def health_check(self) -> bool:
        return self.health()[0]
