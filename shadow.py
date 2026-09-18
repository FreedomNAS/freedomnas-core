"""
The Configuration's Shadow Copy — backend selection.

Everything imports active_dataset/ensure_dataset/sync from here, and this
module decides which implementation answers:

  FREEDOMNAS_SHADOW=python   shadow_py.py — pure Python (default)
  FREEDOMNAS_SHADOW=native   shadow_native.py — mount/copy/unmount run in
                             Nim, and an ImportError if it is not built
  FREEDOMNAS_SHADOW=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins this module back if only that needs reverting. The
native implementation calls the engine's native handle directly, so it
only makes sense paired with the native engine.

Both are held to the same behaviour by tests/test_shadow_parity.py.
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
    from freedomnas.shadow_py import CONFIG_NAME, DATASET_NAME, STATE_DIR
except ImportError:
    from shadow_py import CONFIG_NAME, DATASET_NAME, STATE_DIR  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Shadow")

BACKEND = os.environ.get(
    "FREEDOMNAS_SHADOW",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.shadow_native import active_dataset, ensure_dataset, sync
    except ImportError:
        from shadow_native import active_dataset, ensure_dataset, sync  # type: ignore[no-redef]
    return active_dataset, ensure_dataset, sync


def _load_python():
    try:
        from freedomnas.shadow_py import active_dataset, ensure_dataset, sync
    except ImportError:
        from shadow_py import active_dataset, ensure_dataset, sync  # type: ignore[no-redef]
    return active_dataset, ensure_dataset, sync


if BACKEND == "native":
    active_dataset, ensure_dataset, sync = _load_native()
elif BACKEND == "auto":
    try:
        active_dataset, ensure_dataset, sync = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native shadow backend unavailable (%s); using Python", exc)
        active_dataset, ensure_dataset, sync = _load_python()
        BACKEND = "python"
else:
    active_dataset, ensure_dataset, sync = _load_python()

__all__ = [
    "active_dataset",
    "BACKEND",
    "CommandExecutionError",
    "CommandTranslationError",
    "CONFIG_NAME",
    "DATASET_NAME",
    "ensure_dataset",
    "STATE_DIR",
    "sync",
]
