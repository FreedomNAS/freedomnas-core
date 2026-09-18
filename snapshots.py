"""
Scheduled ZFS Snapshots — backend selection.

Everything imports SnapshotScheduler from here, and this module decides
which implementation answers:

  FREEDOMNAS_SNAPSHOTS=python   snapshots_py.py — pure Python (default)
  FREEDOMNAS_SNAPSHOTS=native   snapshots_native.py — the scheduling logic
                                runs in Nim, and an ImportError if it is
                                not built
  FREEDOMNAS_SNAPSHOTS=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins the scheduler back if only that needs reverting. The
native scheduler calls the engine's native handle directly, so it only
makes sense paired with the native engine.

Both are held to the same behaviour by tests/test_snapshots_parity.py.
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
    from freedomnas.snapshots_py import PREFIX, snapshot_name
except ImportError:
    from snapshots_py import PREFIX, snapshot_name  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Snapshots")

BACKEND = os.environ.get(
    "FREEDOMNAS_SNAPSHOTS",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.snapshots_native import SnapshotScheduler
    except ImportError:
        from snapshots_native import SnapshotScheduler  # type: ignore[no-redef]
    return SnapshotScheduler


def _load_python():
    try:
        from freedomnas.snapshots_py import SnapshotScheduler
    except ImportError:
        from snapshots_py import SnapshotScheduler  # type: ignore[no-redef]
    return SnapshotScheduler


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
            "FREEDOMNAS_SNAPSHOTS=native needs FREEDOMNAS_ENGINE=native (engine backend is %s); "
            "using the Python snapshot scheduler", _ENGINE_BACKEND,
        )
    BACKEND = "python"

if BACKEND == "native":
    SnapshotScheduler = _load_native()
elif BACKEND == "auto":
    try:
        SnapshotScheduler = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native snapshot scheduler unavailable (%s); using Python", exc)
        SnapshotScheduler = _load_python()
        BACKEND = "python"
else:
    SnapshotScheduler = _load_python()

__all__ = [
    "BACKEND",
    "CommandExecutionError",
    "CommandTranslationError",
    "PREFIX",
    "SnapshotScheduler",
    "snapshot_name",
]
