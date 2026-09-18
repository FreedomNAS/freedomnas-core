"""
FreedomNAS ZFS Encryption Key Management — wrapping and unlock run in Nim.

scrypt + AES-256-GCM key wrapping, keyslot JSON persistence, pool/device
discovery, USB zero-touch unlock, the TTY passphrase prompt and pool
provisioning all happen in fnas/keymgmt.nim, against the same engine
handle the native engine already opened. This file is the thin Python
side of that boundary: it hex/JSON-encodes arguments and unwraps the
native module's reply into the same exception classes keymgmt_py raises,
so `except SlotUnavailableError` catches either backend's failures.

Selected by FREEDOMNAS_KEYMGMT=native; see keymgmt.py. Only meaningful
paired with the native engine — it calls `engine._handle` directly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas.keymgmt_py import (
        KEYSLOT_DIR,
        KeyManagementError,
        RUNTIME_KEY_DIR,
        SlotUnavailableError,
        USB_MOUNTPOINT,
    )
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from keymgmt_py import (  # type: ignore[no-redef]
        KEYSLOT_DIR,
        KeyManagementError,
        RUNTIME_KEY_DIR,
        SlotUnavailableError,
        USB_MOUNTPOINT,
    )

logger = logging.getLogger("FreedomNAS.KeyMgmt")

_ERROR_CLASSES = {
    "keymgmt": SlotUnavailableError,
    "translation": CommandTranslationError,
    "execution": CommandExecutionError,
}


def _unwrap(payload: str) -> Any:
    result = json.loads(payload)
    error = result.get("error")
    if error is None:
        return result["value"]
    kind = error.get("kind", "execution")
    raise _ERROR_CLASSES.get(kind, KeyManagementError)(error.get("message", ""))


def wrap_key(raw_key: bytes, secret: bytes) -> dict:
    """Wrap raw_key so it can only be recovered with the given secret."""
    return _unwrap(_native.keymgmt_wrap_key(raw_key.hex(), secret.hex()))


def unwrap_key(slot: dict, secret: bytes) -> bytes:
    """Recover the raw key from a slot. Raises SlotUnavailableError on any mismatch."""
    return bytes.fromhex(_unwrap(_native.keymgmt_unwrap_key(json.dumps(slot), secret.hex())))


class KeyslotStore:
    """Persists wrapped-key slots for one pool — the JSON end runs in Nim."""

    def __init__(self, pool_name: str, base_dir: Optional[Path] = None) -> None:
        self.pool_name = pool_name
        self._base_dir = str(base_dir or KEYSLOT_DIR)

    def exists(self) -> bool:
        return _unwrap(_native.keymgmt_store_exists(self.pool_name, self._base_dir))

    def get_slot(self, name: str) -> Optional[dict]:
        return _unwrap(_native.keymgmt_store_get_slot(self.pool_name, self._base_dir, name))

    def set_slot(self, name: str, slot: dict) -> None:
        _unwrap(_native.keymgmt_store_set_slot(
            self.pool_name, self._base_dir, name, json.dumps(slot)
        ))


class PoolUnlocker:
    """Unlocks ZFS-native-encrypted pools using independent keyslots — in Nim."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def list_locked_pools(self) -> List[str]:
        return _unwrap(_native.keymgmt_list_locked_pools(self.engine._handle))

    def try_usb_unlock(self, pool_name: str) -> bool:
        return _unwrap(_native.keymgmt_try_usb_unlock(
            self.engine._handle, pool_name,
            str(KEYSLOT_DIR), str(USB_MOUNTPOINT), str(RUNTIME_KEY_DIR),
        ))

    def try_tty_unlock(self, pool_name: str, tty_path: str, timeout: int = 300) -> bool:
        return _unwrap(_native.keymgmt_try_tty_unlock(
            self.engine._handle, pool_name, tty_path, timeout,
            str(KEYSLOT_DIR), str(RUNTIME_KEY_DIR),
        ))


class PoolProvisioner:
    """Creates a new ZFS-native-encrypted pool and its two independent keyslots — in Nim."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def create_encrypted_pool(
        self,
        pool_name: str,
        vdev_type: str,
        devices: str,
        master_passphrase: str,
        usb_devices: Optional[List[str]] = None,
        ashift: int = 12,
        compression: str = "lz4",
    ) -> None:
        _unwrap(_native.keymgmt_create_encrypted_pool(
            self.engine._handle, pool_name, vdev_type, devices, master_passphrase,
            json.dumps(list(usb_devices or [])), ashift, compression,
            str(KEYSLOT_DIR), str(RUNTIME_KEY_DIR), str(USB_MOUNTPOINT),
        ))
