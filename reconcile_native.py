"""
FreedomNAS Reconciliation Engine — the diff/apply logic runs in Nim.

compute_diff, the fixed disaster-recovery order and every atomic engine
call all happen in fnas/reconcile.nim, against the same engine handle the
native engine already opened. This file is the thin Python side of that
boundary: it JSON-encodes the configuration (timestamps included, the way
config_native does) and unwraps the native module's reply back into the
same ActionResult and Change objects the pure-Python engine returns, so
callers of reconcile(), compute_diff() and load_desired() see no
difference.

Selected by FREEDOMNAS_RECONCILE=native; see reconcile.py. Only meaningful
paired with the native engine — reconcile() drives it through its native
handle, and reconcile.py falls back to Python when the engine is not
native.
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
    from freedomnas.engine_native import native_handle
except ImportError:
    from engine_native import native_handle  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas.reconcile_py import ActionResult, Change, ReconcileMode
    from freedomnas.config_native import _decode, _encode
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from reconcile_py import ActionResult, Change, ReconcileMode  # type: ignore[no-redef]
    from config_native import _decode, _encode  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Reconciler")

_ERROR_CLASSES = {
    "value": ValueError,
    "translation": CommandTranslationError,
    "execution": CommandExecutionError,
    "os_error": OSError,
}


def _yaml_error():
    """What yaml.safe_load raises in the Python reconciler, when PyYAML is there."""
    try:
        import yaml
        return yaml.YAMLError
    except ImportError:
        return ValueError


def _unwrap(payload: str):
    result = json.loads(payload)
    error = result.get("error")
    if error is None:
        return result["value"]
    kind = error.get("kind", "execution")
    if kind == "config":
        raise _yaml_error()(error.get("message", ""))
    raise _ERROR_CLASSES.get(kind, CommandExecutionError)(error.get("message", ""))


def _mode_name(mode) -> str:
    return mode.value if isinstance(mode, ReconcileMode) else str(mode)


class ReconciliationEngine:
    """Same public API as the pure-Python engine; diff/apply run in Nim."""

    def __init__(self, engine, config) -> None:
        self.engine = engine
        self.config = config

    def load_desired(self, path: str) -> dict:
        """Load a desired-state YAML file and return it as a dict."""
        return _decode(_unwrap(_native.reconcile_load_desired(str(path)))) or {}

    def compute_diff(
        self,
        desired: dict,
        current: dict,
        mode: ReconcileMode = ReconcileMode.HYBRID,
    ) -> List[Change]:
        """Compare desired vs current state and return the list of Changes needed."""
        changes = _unwrap(_native.reconcile_compute_diff(
            json.dumps(_encode(desired)), json.dumps(_encode(current)), _mode_name(mode)
        ))
        return [
            Change(
                path=c["path"],
                old_value=_decode(c["old_value"]),
                new_value=_decode(c["new_value"]),
                change_type=c["change_type"],
            )
            for c in changes
        ]

    def reconcile(
        self,
        desired_path: Optional[str] = None,
        mode: str = "hybrid",
    ) -> List[ActionResult]:
        state = json.dumps(_encode(self.config.to_dict()))
        nfs_shares = json.dumps(_encode(self.config.get_nfs_shares()))
        results = _unwrap(_native.reconcile_run(
            native_handle(self.engine), desired_path or "", _mode_name(mode), state, nfs_shares
        ))
        return [
            ActionResult(
                action=r["action"],
                params=_decode(r["params"]),
                target_state=r["target_state"],
                error=r.get("error"),
            )
            for r in results
        ]
