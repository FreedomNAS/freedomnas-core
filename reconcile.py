"""
FreedomNAS Reconciliation Engine — backend selection.

Everything imports ReconciliationEngine from here, and this module decides
which implementation answers:

  FREEDOMNAS_RECONCILE=python   reconcile_py.py — pure Python (default)
  FREEDOMNAS_RECONCILE=native   reconcile_native.py — the diff/apply logic
                                runs in Nim, and an ImportError if it is not
                                built
  FREEDOMNAS_RECONCILE=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins this module back if only that needs reverting. The
native implementation calls the engine's native handle directly, so it
only makes sense paired with the native engine.

Both are held to the same behaviour by tests/test_reconcile_parity.py.
Change, ReconcileMode, ActionResult, CommandExecutionError and
CommandTranslationError come from reconcile_py either way — they are pure
data, with no backend of their own.
"""

import logging
import os

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

try:
    from freedomnas.reconcile_py import ActionResult, Change, ReconcileMode
except ImportError:
    from reconcile_py import ActionResult, Change, ReconcileMode  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Reconciler")

BACKEND = os.environ.get(
    "FREEDOMNAS_RECONCILE",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.reconcile_native import ReconciliationEngine
    except ImportError:
        from reconcile_native import ReconciliationEngine  # type: ignore[no-redef]
    return ReconciliationEngine


def _load_python():
    try:
        from freedomnas.reconcile_py import ReconciliationEngine
    except ImportError:
        from reconcile_py import ReconciliationEngine  # type: ignore[no-redef]
    return ReconciliationEngine


try:
    from freedomnas.engine import BACKEND as _ENGINE_BACKEND
except ImportError:
    from engine import BACKEND as _ENGINE_BACKEND  # type: ignore[no-redef]

# The native module drives the engine from inside the extension, so it
# cannot run on top of the Python engine. Rolling the engine back rolls
# this back with it, rather than failing on the first call.
if BACKEND in ("native", "auto") and _ENGINE_BACKEND != "native":
    if BACKEND == "native":
        logger.warning(
            "FREEDOMNAS_RECONCILE=native needs FREEDOMNAS_ENGINE=native (engine backend is %s); "
            "using the Python reconciler", _ENGINE_BACKEND,
        )
    BACKEND = "python"

if BACKEND == "native":
    ReconciliationEngine = _load_native()
elif BACKEND == "auto":
    try:
        ReconciliationEngine = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native reconcile backend unavailable (%s); using Python", exc)
        ReconciliationEngine = _load_python()
        BACKEND = "python"
else:
    ReconciliationEngine = _load_python()

__all__ = [
    "ActionResult",
    "BACKEND",
    "Change",
    "CommandExecutionError",
    "CommandTranslationError",
    "ReconcileMode",
    "ReconciliationEngine",
]
