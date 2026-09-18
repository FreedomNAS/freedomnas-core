"""
FreedomNAS ZFS Encryption Key Management — backend selection.

Everything imports wrap_key/unwrap_key/KeyslotStore/PoolUnlocker/
PoolProvisioner from here, and this module decides which implementation
answers:

  FREEDOMNAS_KEYMGMT=python   keymgmt_py.py — pure Python (default)
  FREEDOMNAS_KEYMGMT=native   keymgmt_native.py — scrypt + AES-256-GCM
                              wrapping, keyslot persistence, discovery and
                              unlock all run in Nim, and an ImportError if
                              it is not built
  FREEDOMNAS_KEYMGMT=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins this module back if only that needs reverting. The
native implementation calls the engine's native handle directly, so it
only makes sense paired with the native engine.

A keyslot written by one backend must open with the other: both wrap with
the exact same scrypt (RFC 7914) and AES-256-GCM (NIST SP 800-38D)
parameters, cross-checked byte-for-byte in tests/test_keymgmt_parity.py —
alongside PoolUnlocker/PoolProvisioner end to end, including the TTY
passphrase prompt against a real pty.

KeyManagementError, SlotUnavailableError and the path constants are pure
data with no backend of their own — they always come from keymgmt_py.
"""

import logging
import os

try:
    from freedomnas.keymgmt_py import (
        KEYSLOT_DIR,
        KeyManagementError,
        RUNTIME_KEY_DIR,
        SlotUnavailableError,
        USB_KEY_FILENAME,
        USB_MOUNTPOINT,
    )
except ImportError:
    from keymgmt_py import (  # type: ignore[no-redef]
        KEYSLOT_DIR,
        KeyManagementError,
        RUNTIME_KEY_DIR,
        SlotUnavailableError,
        USB_KEY_FILENAME,
        USB_MOUNTPOINT,
    )

logger = logging.getLogger("FreedomNAS.KeyMgmt")

BACKEND = os.environ.get(
    "FREEDOMNAS_KEYMGMT",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.keymgmt_native import (
            KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key,
        )
    except ImportError:
        from keymgmt_native import (  # type: ignore[no-redef]
            KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key,
        )
    return KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key


def _load_python():
    try:
        from freedomnas.keymgmt_py import (
            KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key,
        )
    except ImportError:
        from keymgmt_py import (  # type: ignore[no-redef]
            KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key,
        )
    return KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key


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
            "FREEDOMNAS_KEYMGMT=native needs FREEDOMNAS_ENGINE=native (engine backend is %s); "
            "using the Python key management", _ENGINE_BACKEND,
        )
    BACKEND = "python"

if BACKEND == "native":
    KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key = _load_native()
elif BACKEND == "auto":
    try:
        KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native keymgmt backend unavailable (%s); using Python", exc)
        KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key = _load_python()
        BACKEND = "python"
else:
    KeyslotStore, PoolProvisioner, PoolUnlocker, unwrap_key, wrap_key = _load_python()

__all__ = [
    "BACKEND",
    "KEYSLOT_DIR",
    "KeyManagementError",
    "KeyslotStore",
    "PoolProvisioner",
    "PoolUnlocker",
    "RUNTIME_KEY_DIR",
    "SlotUnavailableError",
    "USB_KEY_FILENAME",
    "USB_MOUNTPOINT",
    "unwrap_key",
    "wrap_key",
]
