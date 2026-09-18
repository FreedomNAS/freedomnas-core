"""
Pool composition — attach/detach run in Nim.

Filtering out empty device entries, building the `zpool add` device list and
the per-role error reporting all happen in fnas/pools.nim, against the same
engine handle the native engine already opened. This file is the thin
Python side of that boundary: it JSON-encodes the device lists and unwraps
the native module's reply.

Selected by FREEDOMNAS_POOLS=native; see pools.py. Only meaningful paired
with the native engine — it calls `engine._handle` directly.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Sequence

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Pools")

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


def attach_devices(
    engine,
    pool_name: str,
    cache: Optional[Sequence[str]] = None,
    log: Optional[Sequence[str]] = None,
) -> Dict[str, List]:
    return _unwrap(_native.pool_attach_devices(
        engine._handle, pool_name, json.dumps(list(cache or [])), json.dumps(list(log or []))
    ))


def detach_device(engine, pool_name: str, device: str) -> None:
    _unwrap(_native.pool_detach_device(engine._handle, pool_name, device))
