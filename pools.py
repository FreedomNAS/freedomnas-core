"""
Pool Composition — backend selection.

Everything imports attach_devices/detach_device from here, and this module
decides which implementation answers:

  FREEDOMNAS_POOLS=python   pools_py.py — pure Python (default)
  FREEDOMNAS_POOLS=native   pools_native.py — attach/detach run in Nim,
                            and an ImportError if it is not built
  FREEDOMNAS_POOLS=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins this module back if only that needs reverting. The
native implementation calls the engine's native handle directly, so it
only makes sense paired with the native engine.

Both are held to the same behaviour by tests/test_pools_parity.py.
CommandExecutionError and CommandTranslationError come from engine either
way.
"""

import logging
import os

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

try:
    from freedomnas.pools_py import ROLES
except ImportError:
    from pools_py import ROLES  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Pools")

BACKEND = os.environ.get(
    "FREEDOMNAS_POOLS",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.pools_native import attach_devices, detach_device
    except ImportError:
        from pools_native import attach_devices, detach_device  # type: ignore[no-redef]
    return attach_devices, detach_device


def _load_python():
    try:
        from freedomnas.pools_py import attach_devices, detach_device
    except ImportError:
        from pools_py import attach_devices, detach_device  # type: ignore[no-redef]
    return attach_devices, detach_device


if BACKEND == "native":
    attach_devices, detach_device = _load_native()
elif BACKEND == "auto":
    try:
        attach_devices, detach_device = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native pools backend unavailable (%s); using Python", exc)
        attach_devices, detach_device = _load_python()
        BACKEND = "python"
else:
    attach_devices, detach_device = _load_python()

__all__ = [
    "attach_devices",
    "BACKEND",
    "CommandExecutionError",
    "CommandTranslationError",
    "detach_device",
    "ROLES",
]
