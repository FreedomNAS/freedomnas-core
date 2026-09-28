"""
FreedomNAS ZFS Encryption Key Management

Manages independent unlock factors ("keyslots") for ZFS-native-encrypted
pools, analogous to LUKS2/systemd-cryptenroll keyslots:

  - At pool-creation time a random raw ZFS key is generated once and never
    stored in the clear anywhere.
  - Two independent wrapped copies of that raw key are kept in a keyslot
    file outside the encrypted pool (on the boot device, so it is readable
    before the pool is unlocked): one unwrapped by the byte content of a
    "freedomnas.key" file distributed to one or more USB sticks (zero-touch
    boot), one unwrapped by an operator-chosen Master Passphrase (TTY
    fallback). Each slot uses its own salt/nonce and scrypt KDF, so
    compromising one factor does not reveal the other or the pool key.
  - Both unlock paths converge on the same engine action (zfs.load_key_file):
    the raw key is written to a 0600 tmpfs file just long enough for
    `zfs load-key -L file://...` to read it, then the file is shredded.

All ZFS/OS operations delegate to FreedomNASEngine; no subprocess calls here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import select
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from freedomnas.engine import (
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    from freedomnas import paths
except ImportError:
    from engine import (  # type: ignore[no-redef]
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    import paths  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.KeyMgmt")

# None means <config_dir>/keyslots from the OS profile; tests point it elsewhere.
KEYSLOT_DIR: Optional[Path] = None
RUNTIME_KEY_DIR = Path("/run/freedomnas")
USB_KEY_FILENAME = "freedomnas.key"
USB_MOUNTPOINT = Path("/run/freedomnas/usbkey")
KEYSLOT_PROPERTY = "org.freedomnas:keyslots"

# scrypt cost parameters: ~32 MiB / attempt, appropriate for a boot-time
# unlock (a handful of attempts, not an offline password database).
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024  # headroom above the 128*N*r requirement below
_KEY_LEN = 32   # AES-256 raw ZFS key length
_SALT_LEN = 16
_NONCE_LEN = 12


_TIMED_OUT = object()


class KeyManagementError(Exception):
    """Base exception for keyslot/unlock errors."""


class SlotUnavailableError(KeyManagementError):
    """Raised when a keyslot cannot be unwrapped: missing, wrong secret, or corrupted."""


# ----------------------------------------------------------------------
# Key wrapping primitives
# ----------------------------------------------------------------------

def _derive_wrapping_key(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        secret, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=_KEY_LEN,
    )


def wrap_key(raw_key: bytes, secret: bytes) -> dict:
    """Wrap raw_key so it can only be recovered with the given secret."""
    salt = secrets.token_bytes(_SALT_LEN)
    nonce = secrets.token_bytes(_NONCE_LEN)
    wrapping_key = _derive_wrapping_key(secret, salt)
    ciphertext = AESGCM(wrapping_key).encrypt(nonce, raw_key, None)
    return {"salt": salt.hex(), "nonce": nonce.hex(), "ciphertext": ciphertext.hex()}


def unwrap_key(slot: dict, secret: bytes) -> bytes:
    """Recover the raw key from a slot. Raises SlotUnavailableError on any mismatch."""
    try:
        salt = bytes.fromhex(slot["salt"])
        nonce = bytes.fromhex(slot["nonce"])
        ciphertext = bytes.fromhex(slot["ciphertext"])
    except (KeyError, ValueError) as exc:
        raise SlotUnavailableError(f"Malformed keyslot data: {exc}")

    wrapping_key = _derive_wrapping_key(secret, salt)
    try:
        return AESGCM(wrapping_key).decrypt(nonce, ciphertext, None)
    except Exception:
        raise SlotUnavailableError("Wrong secret or corrupted keyslot")


# ----------------------------------------------------------------------
# Keyslot persistence
# ----------------------------------------------------------------------

class KeyslotStore:
    """
    Persists wrapped-key slots for one pool at <config_dir>/keyslots/<pool>.json.

    Must live outside the encrypted pool -- it has to be readable before the
    pool is unlocked -- mirroring config.py's use of <config_dir> as the
    boot-device shadow location.
    """

    def __init__(self, pool_name: str, base_dir: Optional[Path] = None):
        # Resolved at call time, not bound as a default argument, so that
        # overriding the module-level KEYSLOT_DIR (e.g. in tests) takes effect.
        self.pool_name = pool_name
        self.path = (base_dir or KEYSLOT_DIR or paths.keyslot_dir()) / f"{pool_name}.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.chmod(0o600)
        tmp.rename(self.path)

    def get_slot(self, name: str) -> Optional[dict]:
        if not self.exists():
            return None
        return self.load().get("slots", {}).get(name)

    def set_slot(self, name: str, slot: dict) -> None:
        data = self.load() if self.exists() else {
            "pool": self.pool_name,
            "created_at": datetime.now().isoformat(),
            "slots": {},
        }
        data["slots"][name] = slot
        data["updated_at"] = datetime.now().isoformat()
        self.save(data)


# ----------------------------------------------------------------------
# The keyslots' copy on the pool itself
#
# The file on the boot device is lost with the boot device, and a pool moved
# to another system leaves it behind. So the same slots are also kept in a
# ZFS user property of the pool's own dataset: properties are not encrypted,
# so they can be read before the pool is unlocked — the model LUKS uses for
# its header. The slots hold only wrapped copies of the key.
# ----------------------------------------------------------------------

def encode_keyslots(data: dict) -> str:
    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_keyslots(value: str) -> Optional[dict]:
    try:
        data = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("slots"), dict) else None


def pool_keyslots(engine: FreedomNASEngine, dataset: str) -> Optional[dict]:
    """The keyslots recorded on a pool's dataset, or None."""
    try:
        stdout, _ = engine.execute(
            "zfs", "dataset_get_property", property=KEYSLOT_PROPERTY, dataset=dataset, check=False
        )
    except (CommandExecutionError, CommandTranslationError):
        return None
    value = stdout.split("\t")[0].strip()
    if not value or value == "-":
        return None
    return decode_keyslots(value)


def publish_keyslots(engine: FreedomNASEngine, pool_name: str) -> bool:
    """
    Copy this system's keyslots for pool_name onto the pool. Returns True if
    the pool holds them afterwards; never raises.
    """
    store = KeyslotStore(pool_name)
    if not store.exists():
        return False
    try:
        data = store.load()
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read the keyslots of %s: %s", pool_name, exc)
        return False
    if pool_keyslots(engine, pool_name) == data:
        return True
    try:
        engine.execute(
            "zfs", "dataset_set",
            property=KEYSLOT_PROPERTY, value=encode_keyslots(data), dataset=pool_name,
        )
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Could not record the keyslots on %s: %s", pool_name, exc)
        return False
    logger.info("Recorded the keyslots of %s on the pool", pool_name)
    return True


def adopt_keyslots(pool_name: str, data: dict) -> bool:
    """
    Give this system the keyslots of a pool it imported, so the boot-time
    unlock (USB key, console passphrase) works for it. A pool that already
    has a keyslot file here keeps it.
    """
    store = KeyslotStore(pool_name)
    if store.exists():
        return False
    store.save({**data, "pool": pool_name, "adopted_at": datetime.now().isoformat()})
    return True


def removable_partitions(engine: FreedomNASEngine) -> List[str]:
    return PoolUnlocker(engine).removable_partitions()


def load_raw_key(engine: FreedomNASEngine, dataset: str, raw_key: bytes) -> None:
    """Load an unwrapped key into an encryption root. Raises on failure."""
    keyfile = _write_transient_keyfile(dataset.replace("/", "_"), raw_key)
    try:
        engine.execute("zfs", "load_key_file", pool_name=dataset, keyfile_path=str(keyfile))
    finally:
        _shred(keyfile)


# ----------------------------------------------------------------------
# Unlock at boot: USB zero-touch, TTY passphrase fallback
# ----------------------------------------------------------------------

class PoolUnlocker:
    """
    Unlocks ZFS-native-encrypted pools using independent keyslots.

    Secret material for the passphrase path never touches argv or a
    subprocess pipe -- only the unwrapped raw pool key ever reaches disk,
    and only transiently, in a 0600 tmpfs file that is shredded immediately
    after `zfs load-key` reads it.
    """

    def __init__(self, engine: FreedomNASEngine):
        self.engine = engine

    # ---- discovery ----------------------------------------------------

    def list_locked_pools(self) -> List[str]:
        """Return the names of imported pools whose root dataset is keystatus=unavailable."""
        locked: List[str] = []
        try:
            stdout, _ = self.engine.execute("zfs", "pool_list", check=False)
        except (CommandExecutionError, CommandTranslationError):
            return locked

        for line in stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            pool_name = fields[0]
            try:
                status, _ = self.engine.execute(
                    "zfs", "pool_keystatus", pool_name=pool_name, check=False
                )
            except (CommandExecutionError, CommandTranslationError):
                continue
            if status.strip() == "unavailable":
                locked.append(pool_name)
        return locked

    # ---- USB zero-touch -------------------------------------------------

    def try_usb_unlock(self, pool_name: str) -> bool:
        """Scan removable media for freedomnas.key and unlock pool_name if it matches."""
        store = KeyslotStore(pool_name)
        slot = store.get_slot("usb")
        if slot is None:
            logger.info("No USB keyslot configured for pool %s", pool_name)
            return False

        raw_key = self.find_usb_key(slot)
        if raw_key is None:
            logger.info("No matching USB key found for pool %s", pool_name)
            return False
        return self._load_raw_key(pool_name, raw_key)

    def find_usb_key(self, slot: dict) -> Optional[bytes]:
        """The raw key from the first USB stick whose freedomnas.key opens slot."""
        for device in self._list_removable_devices():
            key_bytes = self._read_usb_key(device)
            if key_bytes is None:
                continue
            try:
                return unwrap_key(slot, key_bytes)
            except SlotUnavailableError:
                logger.warning("USB key on %s did not match the keyslot", device)
        return None

    def removable_partitions(self) -> List[str]:
        return self._list_removable_devices()

    def _list_removable_devices(self) -> List[str]:
        try:
            stdout, _ = self.engine.execute("media", "list_removable", check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("Could not list removable media: %s", exc)
            return []

        devices: List[str] = []
        for line in stdout.splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            name, removable, dev_type = fields[0], fields[1], fields[2]
            if removable == "1" and dev_type == "part":
                devices.append(f"/dev/{name}")
        return devices

    def _read_usb_key(self, device: str) -> Optional[bytes]:
        USB_MOUNTPOINT.mkdir(parents=True, exist_ok=True)
        USB_MOUNTPOINT.chmod(0o700)
        try:
            self.engine.execute("media", "mount_ro", device=device, mountpoint=str(USB_MOUNTPOINT))
        except (CommandExecutionError, CommandTranslationError):
            return None

        try:
            key_path = USB_MOUNTPOINT / USB_KEY_FILENAME
            if not key_path.is_file():
                return None
            return key_path.read_bytes()
        finally:
            try:
                self.engine.execute("media", "unmount", mountpoint=str(USB_MOUNTPOINT), check=False)
            except (CommandExecutionError, CommandTranslationError):
                pass

    # ---- TTY passphrase fallback ----------------------------------------

    def try_tty_unlock(self, pool_name: str, tty_path: str, timeout: int = 300) -> bool:
        """
        Prompt for the Master Passphrase on tty_path, retrying until success
        or until `timeout` seconds elapse without a successful attempt.

        Fails open: on timeout, logs a warning and returns False rather than
        blocking forever, so firstboot.sh can still reach the WebUI (the
        pool stays locked until unlocked manually).
        """
        store = KeyslotStore(pool_name)
        slot = store.get_slot("passphrase")
        if slot is None:
            logger.info("No passphrase keyslot configured for pool %s", pool_name)
            return False

        try:
            tty_in = open(tty_path, "r")
            tty_out = open(tty_path, "w")
        except OSError as exc:
            logger.warning("Cannot open %s for the passphrase prompt: %s", tty_path, exc)
            return False

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                remaining = int(deadline - time.monotonic())
                tty_out.write(
                    f"\nFreedomNAS: pool '{pool_name}' is encrypted and no USB key was found.\n"
                    f"Enter Master Passphrase to unlock (giving up in {remaining}s): "
                )
                tty_out.flush()

                passphrase = self._read_line_no_echo(tty_in, tty_out, deadline)
                if passphrase is None or passphrase is _TIMED_OUT:
                    break  # EOF (tty closed / detached), or nobody answered in time
                if not passphrase:
                    continue

                try:
                    raw_key = unwrap_key(slot, passphrase.encode("utf-8"))
                except SlotUnavailableError:
                    tty_out.write("Incorrect passphrase.\n")
                    tty_out.flush()
                    continue

                return self._load_raw_key(pool_name, raw_key)

            tty_out.write(
                f"\nFreedomNAS: timed out waiting for the Master Passphrase for '{pool_name}'.\n"
                "Continuing boot with this pool locked -- unlock it later from the WebUI or CLI.\n"
            )
            tty_out.flush()
            logger.warning("Passphrase unlock for %s timed out after %ss", pool_name, timeout)
            return False
        finally:
            tty_in.close()
            tty_out.close()

    @staticmethod
    def _read_line_no_echo(tty_in, tty_out, deadline: float):
        """
        Read one line from an explicit tty file object with local echo
        disabled, waiting no later than `deadline` (a time.monotonic() value).

        Returns the line, None on EOF, or _TIMED_OUT. The wait has to be
        bounded here and not only between attempts: a console nobody sits at
        never sends a newline, and a blocking read would hold the boot there
        for good instead of failing open.
        """
        import termios

        fd = tty_in.fileno()
        old_settings = termios.tcgetattr(fd)
        new_settings = termios.tcgetattr(fd)
        new_settings[3] &= ~termios.ECHO
        data = b""
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new_settings)
            while not data.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _TIMED_OUT
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    return _TIMED_OUT
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                data += chunk
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            tty_out.write("\n")
        if not data:
            return None
        return data.decode("utf-8", errors="replace").rstrip("\n")

    # ---- shared: load an unwrapped raw key into the pool ------------------

    def _load_raw_key(self, pool_name: str, raw_key: bytes) -> bool:
        try:
            load_raw_key(self.engine, pool_name, raw_key)
            # Only this pool's datasets: `zfs mount -a` would also mount every
            # other imported pool, including one belonging to another system.
            # And only those meant to mount by themselves: `.freedomnas` is
            # canmount=noauto and would land on this system's state directory.
            self.engine.execute("zfs", "mount_pool_auto", pool_name=pool_name, check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to load key for pool %s: %s", pool_name, exc)
            return False
        logger.info("Unlocked ZFS pool: %s", pool_name)
        publish_keyslots(self.engine, pool_name)
        return True


# ----------------------------------------------------------------------
# Provisioning at pool-creation time (used by the setup wizard)
# ----------------------------------------------------------------------

class PoolProvisioner:
    """Creates a new ZFS-native-encrypted pool and its two independent keyslots."""

    def __init__(self, engine: FreedomNASEngine):
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
        """
        Create pool_name with ZFS native encryption, then provision two
        independent unlock keyslots: one wrapped with master_passphrase,
        one wrapped with a freshly generated random USB key that is written
        as freedomnas.key onto every device in usb_devices (same bytes on
        each stick, so any one of them unlocks).
        """
        raw_key = secrets.token_bytes(_KEY_LEN)
        usb_key_bytes = secrets.token_bytes(_KEY_LEN)

        keyfile = _write_transient_keyfile(pool_name, raw_key)
        try:
            self.engine.execute(
                "zfs", "pool_create_encrypted",
                pool_name=pool_name,
                vdev_type=vdev_type,
                devices=devices,
                ashift=ashift,
                compression=compression,
                keyfile_path=str(keyfile),
            )
        finally:
            _shred(keyfile)

        store = KeyslotStore(pool_name)
        store.set_slot("passphrase", wrap_key(raw_key, master_passphrase.encode("utf-8")))
        store.set_slot("usb", wrap_key(raw_key, usb_key_bytes))
        publish_keyslots(self.engine, pool_name)

        for device in usb_devices or []:
            self._write_usb_key(device, usb_key_bytes)

    def _write_usb_key(self, device: str, key_bytes: bytes) -> None:
        USB_MOUNTPOINT.mkdir(parents=True, exist_ok=True)
        USB_MOUNTPOINT.chmod(0o700)
        self.engine.execute("media", "mount_rw", device=device, mountpoint=str(USB_MOUNTPOINT))
        try:
            key_path = USB_MOUNTPOINT / USB_KEY_FILENAME
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                _write_all(fd, key_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            self.engine.execute("media", "unmount", mountpoint=str(USB_MOUNTPOINT), check=False)


# ----------------------------------------------------------------------
# Transient tmpfs keyfile helpers, shared by unlock and provisioning
# ----------------------------------------------------------------------

def _write_all(fd: int, data: bytes) -> None:
    """os.write may take less than it was given; a key cut short loads as another key."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _write_transient_keyfile(pool_name: str, raw_key: bytes) -> Path:
    RUNTIME_KEY_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_KEY_DIR.chmod(0o700)
    keyfile = RUNTIME_KEY_DIR / f"{pool_name}.key"
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, raw_key)
    finally:
        os.close(fd)
    return keyfile


def _shred(path: Path) -> None:
    """
    Overwrite, then unlink — and unlink even if the overwrite failed: a raw
    key left in /run is worse than one removed without being scrubbed.
    """
    try:
        size = path.stat().st_size
        with open(path, "r+b", buffering=0) as f:
            _write_all(f.fileno(), secrets.token_bytes(size))
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass
