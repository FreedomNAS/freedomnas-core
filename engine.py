"""
FreedomNAS Core Engine — backend selection.

Everything in FreedomNAS imports the engine from here, and this module
decides which implementation answers:

  FREEDOMNAS_ENGINE=python   engine_py.py — the pure-Python engine (default)
  FREEDOMNAS_ENGINE=native   engine_native.py — the Nim engine, and an
                             ImportError if the extension is not built
  FREEDOMNAS_ENGINE=auto     native when importable, python otherwise

The two implementations are verified against each other by
tests/test_engine_parity.py. Exception classes always come from engine_py,
so `except CommandExecutionError` catches either backend's failures.
"""

import logging
import os

try:
    from freedomnas.engine_py import (
        CommandExecutionError,
        CommandTranslationError,
        FreedomNASEngineException,
        OSNotSupportedException,
    )
except ImportError:
    from engine_py import (  # type: ignore[no-redef]
        CommandExecutionError,
        CommandTranslationError,
        FreedomNASEngineException,
        OSNotSupportedException,
    )

logger = logging.getLogger("FreedomNAS.Core")

BACKEND = os.environ.get("FREEDOMNAS_ENGINE", "python").strip().lower()


def _load_native():
    try:
        from freedomnas.engine_native import FreedomNASEngine, get_engine
    except ImportError:
        from engine_native import FreedomNASEngine, get_engine  # type: ignore[no-redef]
    return FreedomNASEngine, get_engine


def _load_python():
    try:
        from freedomnas.engine_py import FreedomNASEngine, get_engine
    except ImportError:
        from engine_py import FreedomNASEngine, get_engine  # type: ignore[no-redef]
    return FreedomNASEngine, get_engine


if BACKEND == "native":
    FreedomNASEngine, get_engine = _load_native()
elif BACKEND == "auto":
    try:
        FreedomNASEngine, get_engine = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native engine unavailable (%s); using the Python engine", exc)
        FreedomNASEngine, get_engine = _load_python()
        BACKEND = "python"
else:
    FreedomNASEngine, get_engine = _load_python()

__all__ = [
    "BACKEND",
    "CommandExecutionError",
    "CommandTranslationError",
    "FreedomNASEngine",
    "FreedomNASEngineException",
    "OSNotSupportedException",
    "get_engine",
]
