"""
The configuration's shadow copy — mount, copy and unmount run in Nim.

Listing pools, checking whether a dataset exists, mounting a shadow dataset
at a throwaway mountpoint, copying config.yaml into it and unmounting again
all happen in fnas/shadow.nim, against the same engine handle the native
engine already opened. This file is the thin Python side of that boundary.

Selected by FREEDOMNAS_SHADOW=native; see shadow.py. Only meaningful paired
with the native engine — it calls `engine._handle` directly.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Shadow")

_ERROR_CLASSES = {
    "translation": CommandTranslationError,
    "execution": CommandExecutionError,
}


def _unwrap(payload: str):
    result = json.loads(payload)
    error = result.get("error")
    if error is None:
        return result["value"]
    kind = error.get("kind", "execution")
    raise _ERROR_CLASSES.get(kind, CommandExecutionError)(error.get("message", ""))


def active_dataset(engine) -> Optional[str]:
    name = _unwrap(_native.shadow_active_dataset(engine._handle))
    return name or None


def ensure_dataset(engine, pool: str) -> bool:
    return _unwrap(_native.shadow_ensure_dataset(engine._handle, pool))


def sync(engine, config_path: str) -> List[Dict]:
    return _unwrap(_native.shadow_sync(engine._handle, config_path))
