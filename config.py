"""
FreedomNAS Configuration Manager — backend selection.

Everything imports ConfigurationManager from here, and this module decides
which implementation answers:

  FREEDOMNAS_CONFIG=python   config_py.py — PyYAML (default)
  FREEDOMNAS_CONFIG=native   config_native.py — the YAML end runs in Nim,
                             and an ImportError if it is not built
  FREEDOMNAS_CONFIG=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins the configuration back if only that needs reverting.

Both write the same bytes; tests/test_config_parity.py is what holds them
to it. ConfigurationException comes from config_py either way.
"""

import logging
import os

try:
    from freedomnas.config_py import ConfigurationException
except ImportError:
    from config_py import ConfigurationException  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Config")

BACKEND = os.environ.get(
    "FREEDOMNAS_CONFIG",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.config_native import ConfigurationManager
    except ImportError:
        from config_native import ConfigurationManager  # type: ignore[no-redef]
    return ConfigurationManager


def _load_python():
    try:
        from freedomnas.config_py import ConfigurationManager
    except ImportError:
        from config_py import ConfigurationManager  # type: ignore[no-redef]
    return ConfigurationManager


if BACKEND == "native":
    ConfigurationManager = _load_native()
elif BACKEND == "auto":
    try:
        ConfigurationManager = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native config unavailable (%s); using PyYAML", exc)
        ConfigurationManager = _load_python()
        BACKEND = "python"
else:
    ConfigurationManager = _load_python()

__all__ = ["BACKEND", "ConfigurationException", "ConfigurationManager"]
