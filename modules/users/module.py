"""
UsersModule — lifecycle check that users declared in config exist on the OS.

health_check() runs `users.get_user_info` for each user in config and returns
False if any user is missing (non-zero exit code from the engine).
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

logger = logging.getLogger("FreedomNAS.Module.Users")


class UsersModule(FreedomNASModule):
    """Verifies that all configured users exist on the OS at runtime."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)
        self._expected_users: List[str] = []

    @property
    def name(self) -> str:
        return "users"

    def start(self) -> None:
        self._expected_users = [
            u["username"] for u in self._config.get_users()
            if u.get("username")
        ]
        logger.info(
            "UsersModule started — tracking %d user(s): %s",
            len(self._expected_users),
            self._expected_users,
        )

    def stop(self) -> None:
        self._expected_users.clear()
        logger.info("UsersModule stopped")

    def health(self) -> Tuple[bool, str]:
        """
        Every configured account still exists on the OS; a missing one is
        drift between the configuration and the system.
        """
        missing = []
        for username in self._expected_users:
            try:
                out, _ = self._engine.execute("users", "get_user_info", username=username, check=False)
            except CommandTranslationError as exc:
                logger.debug("UsersModule: get_user_info not available: %s", exc)
                return True, "accounts cannot be checked on this edition"
            except CommandExecutionError:
                out = ""
            if not out.strip():
                logger.warning("UsersModule: user '%s' not found on OS", username)
                missing.append(username)
        if missing:
            return False, ", ".join(missing) + " missing from the system"
        return True, f"{len(self._expected_users)} account(s) present"

    def health_check(self) -> bool:
        return self.health()[0]
