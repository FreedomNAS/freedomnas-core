"""
FreedomNAS Reconciliation Engine — the diff/apply logic runs in Nim.

compute_diff, the fixed disaster-recovery order and every atomic engine
call all happen in fnas/reconcile.nim, against the same engine handle the
native engine already opened. This file is the thin Python side of that
boundary: it JSON-encodes the configuration and unwraps the native module's
reply back into the same ActionResult objects the pure-Python engine
returns, so callers (`r.target_state`, `r.error`) see no difference.

Selected by FREEDOMNAS_RECONCILE=native; see reconcile.py. Only meaningful
paired with the native engine — it calls `engine._handle` directly.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas.reconcile_py import ActionResult
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from reconcile_py import ActionResult  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Reconciler")

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


class ReconciliationEngine:
    """Same public API as the pure-Python engine; diff/apply run in Nim."""

    def __init__(self, engine, config) -> None:
        self.engine = engine
        self.config = config

    def reconcile(
        self,
        desired_path: Optional[str] = None,
        mode: str = "hybrid",
    ) -> List[ActionResult]:
        state = json.dumps(self.config.to_dict())
        nfs_shares = json.dumps(self.config.get_nfs_shares())
        results = _unwrap(_native.reconcile_run(
            self.engine._handle, desired_path or "", mode, state, nfs_shares
        ))
        return [
            ActionResult(
                action=r["action"],
                params=r["params"],
                target_state=r["target_state"],
                error=r.get("error"),
            )
            for r in results
        ]
