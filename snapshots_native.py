"""
Scheduled ZFS snapshots — the scheduling logic runs in Nim.

Policy iteration, snapshot naming, listing "ours" and pruning down to the
kept count all happen in fnas/snapshots.nim, against the same engine handle
the native engine already opened. This file is the thin Python side of that
boundary: it collects the policies from `config` and unwraps the native
module's JSON protocol.

Selected by FREEDOMNAS_SNAPSHOTS=native; see snapshots.py. Only meaningful
paired with the native engine — it calls `engine._handle` directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas.snapshots_py import PREFIX, snapshot_name
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from snapshots_py import PREFIX, snapshot_name  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Snapshots")

_ERROR_CLASSES = {
    "value": ValueError,
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


class SnapshotScheduler:
    """Takes and prunes the snapshots described by `snapshots.policies` — in Nim."""

    def __init__(self, engine, config) -> None:
        self.engine = engine
        self.config = config

    def run(self, interval: str, now: Optional[datetime] = None) -> List[Dict]:
        policies = json.dumps(self.config.get_snapshot_policies())
        now_iso = now.isoformat() if now is not None else ""
        return _unwrap(_native.snapshot_run(self.engine._handle, policies, interval, now_iso))
