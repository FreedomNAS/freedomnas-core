"""
Reference behaviour of the pure-Python key management module.

Establishes a baseline before any Nim port: wrap_key/unwrap_key round-trip
and tamper detection, KeyslotStore persistence, discovery, USB zero-touch
unlock, the TTY passphrase fallback (against a real pty — termios only
works on one), and pool-creation provisioning.
"""

import json
import os
import pty
import threading
import time

import pytest

import keymgmt_py as keymgmt


class FakeEngine:
    def __init__(self, pools=(), keystatus=None, removable=(), failing=()):
        self.pools = list(pools)
        self.keystatus = keystatus or {}
        self.removable = list(removable)  # (name, removable, type)
        self.failing = set(failing)
        self.calls = []

    def execute(self, category, action, **kwargs):
        self.calls.append((category, action, kwargs))
        if (category, action) in self.failing:
            raise keymgmt.CommandExecutionError(f"{category}.{action} failed")

        if (category, action) == ("zfs", "pool_list"):
            return "\n".join(f"{p}\t1\t2" for p in self.pools), ""
        if (category, action) == ("zfs", "pool_keystatus"):
            return self.keystatus.get(kwargs["pool_name"], "available"), ""
        if (category, action) == ("media", "list_removable"):
            return "\n".join(f"{n} {r} {t}" for n, r, t in self.removable), ""
        if (category, action) in (("media", "mount_ro"), ("media", "mount_rw")):
            return "", ""
        if (category, action) == ("media", "unmount"):
            return "", ""
        if (category, action) in (("zfs", "load_key_file"), ("zfs", "mount_pool"),
                                   ("zfs", "pool_create_encrypted")):
            return "", ""
        raise AssertionError(f"unexpected call: {category}.{action}")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    keyslot_dir = tmp_path / "keyslots"
    runtime_dir = tmp_path / "run"
    usb_mount = tmp_path / "usbkey"
    monkeypatch.setattr(keymgmt, "KEYSLOT_DIR", keyslot_dir)
    monkeypatch.setattr(keymgmt, "RUNTIME_KEY_DIR", runtime_dir)
    monkeypatch.setattr(keymgmt, "USB_MOUNTPOINT", usb_mount)
    return keyslot_dir, runtime_dir, usb_mount


# ---------------------------------------------------------------------------
# wrap_key / unwrap_key
# ---------------------------------------------------------------------------

def test_wrap_unwrap_round_trip():
    raw_key = os.urandom(32)
    slot = keymgmt.wrap_key(raw_key, b"correct horse battery staple")
    assert keymgmt.unwrap_key(slot, b"correct horse battery staple") == raw_key


def test_wrong_secret_is_rejected():
    slot = keymgmt.wrap_key(os.urandom(32), b"right-secret")
    with pytest.raises(keymgmt.SlotUnavailableError):
        keymgmt.unwrap_key(slot, b"wrong-secret")


def test_tampered_ciphertext_is_rejected():
    slot = keymgmt.wrap_key(os.urandom(32), b"secret")
    tampered = dict(slot)
    tampered["ciphertext"] = ("00" * (len(slot["ciphertext"]) // 2))
    with pytest.raises(keymgmt.SlotUnavailableError):
        keymgmt.unwrap_key(tampered, b"secret")


def test_two_wrappings_of_the_same_key_differ():
    raw_key = os.urandom(32)
    a = keymgmt.wrap_key(raw_key, b"secret")
    b = keymgmt.wrap_key(raw_key, b"secret")
    assert a["salt"] != b["salt"]
    assert a["nonce"] != b["nonce"]
    assert a["ciphertext"] != b["ciphertext"]


# ---------------------------------------------------------------------------
# KeyslotStore
# ---------------------------------------------------------------------------

def test_keyslot_store_round_trip(paths):
    store = keymgmt.KeyslotStore("tank")
    assert not store.exists()
    assert store.get_slot("usb") is None

    slot = keymgmt.wrap_key(os.urandom(32), b"secret")
    store.set_slot("usb", slot)

    assert store.exists()
    reloaded = keymgmt.KeyslotStore("tank")
    assert reloaded.get_slot("usb") == slot
    assert reloaded.get_slot("passphrase") is None


def test_keyslot_store_file_permissions(paths):
    keyslot_dir, _, _ = paths
    store = keymgmt.KeyslotStore("tank")
    store.set_slot("usb", keymgmt.wrap_key(os.urandom(32), b"secret"))

    path = keyslot_dir / "tank.json"
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert oct(keyslot_dir.stat().st_mode)[-3:] == "700"


def test_keyslot_store_two_slots_coexist(paths):
    store = keymgmt.KeyslotStore("tank")
    usb_slot = keymgmt.wrap_key(os.urandom(32), b"usb-secret")
    pass_slot = keymgmt.wrap_key(os.urandom(32), b"pass-secret")
    store.set_slot("usb", usb_slot)
    store.set_slot("passphrase", pass_slot)

    assert store.get_slot("usb") == usb_slot
    assert store.get_slot("passphrase") == pass_slot


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_locked_pools():
    engine = FakeEngine(pools=["rpool", "tank"], keystatus={"tank": "unavailable"})
    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.list_locked_pools() == ["tank"]


def test_list_locked_pools_when_all_unlocked():
    engine = FakeEngine(pools=["rpool"], keystatus={"rpool": "available"})
    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.list_locked_pools() == []


# ---------------------------------------------------------------------------
# USB zero-touch unlock
# ---------------------------------------------------------------------------

def test_usb_unlock_with_no_keyslot_configured(paths):
    engine = FakeEngine()
    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.try_usb_unlock("tank") is False


def test_usb_unlock_with_matching_key(paths):
    _, _, usb_mount = paths
    engine = FakeEngine(removable=[("sdb1", "1", "part")])
    raw_key = os.urandom(32)
    usb_key_bytes = os.urandom(32)
    keymgmt.KeyslotStore("tank").set_slot("usb", keymgmt.wrap_key(raw_key, usb_key_bytes))

    usb_mount.mkdir(parents=True)
    (usb_mount / keymgmt.USB_KEY_FILENAME).write_bytes(usb_key_bytes)

    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.try_usb_unlock("tank") is True
    assert any(c == "zfs" and a == "load_key_file" for c, a, _ in engine.calls)


def test_usb_unlock_with_wrong_key(paths):
    _, _, usb_mount = paths
    engine = FakeEngine(removable=[("sdb1", "1", "part")])
    keymgmt.KeyslotStore("tank").set_slot(
        "usb", keymgmt.wrap_key(os.urandom(32), os.urandom(32))
    )
    usb_mount.mkdir(parents=True)
    (usb_mount / keymgmt.USB_KEY_FILENAME).write_bytes(os.urandom(32))

    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.try_usb_unlock("tank") is False


def test_usb_unlock_ignores_non_removable_partitions(paths):
    engine = FakeEngine(removable=[("sda1", "0", "part"), ("sdb", "1", "disk")])
    keymgmt.KeyslotStore("tank").set_slot(
        "usb", keymgmt.wrap_key(os.urandom(32), os.urandom(32))
    )
    unlocker = keymgmt.PoolUnlocker(engine)
    assert unlocker.try_usb_unlock("tank") is False


# ---------------------------------------------------------------------------
# TTY passphrase fallback — against a real pty
# ---------------------------------------------------------------------------

def test_tty_unlock_with_correct_passphrase(paths):
    engine = FakeEngine()
    raw_key = os.urandom(32)
    keymgmt.KeyslotStore("tank").set_slot(
        "passphrase", keymgmt.wrap_key(raw_key, b"my passphrase")
    )
    unlocker = keymgmt.PoolUnlocker(engine)

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    result = {}

    def run():
        result["ok"] = unlocker.try_tty_unlock("tank", slave_path, timeout=10)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.2)
    os.write(master_fd, b"my passphrase\n")
    thread.join(timeout=10)

    os.close(master_fd)
    os.close(slave_fd)
    assert result["ok"] is True
    assert any(c == "zfs" and a == "load_key_file" for c, a, _ in engine.calls)


def test_tty_unlock_retries_after_wrong_passphrase(paths):
    engine = FakeEngine()
    raw_key = os.urandom(32)
    keymgmt.KeyslotStore("tank").set_slot(
        "passphrase", keymgmt.wrap_key(raw_key, b"correct")
    )
    unlocker = keymgmt.PoolUnlocker(engine)

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    result = {}

    def run():
        result["ok"] = unlocker.try_tty_unlock("tank", slave_path, timeout=10)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.2)
    os.write(master_fd, b"wrong\n")
    time.sleep(0.2)
    os.write(master_fd, b"correct\n")
    thread.join(timeout=10)

    os.close(master_fd)
    os.close(slave_fd)
    assert result["ok"] is True


def test_tty_unlock_gives_up_on_eof(paths):
    engine = FakeEngine()
    keymgmt.KeyslotStore("tank").set_slot(
        "passphrase", keymgmt.wrap_key(os.urandom(32), b"correct")
    )
    unlocker = keymgmt.PoolUnlocker(engine)

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    result = {}

    def run():
        result["ok"] = unlocker.try_tty_unlock("tank", slave_path, timeout=10)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.2)
    # Ctrl-D as the first byte of a line is how a real terminal signals EOF
    # to the foreground reader without a hangup (closing the master fd would
    # send SIGHUP and turn the slave's next read into EIO, not a clean EOF).
    os.write(master_fd, b"\x04")
    thread.join(timeout=10)

    os.close(master_fd)
    os.close(slave_fd)
    assert result["ok"] is False


def test_tty_unlock_gives_up_when_nobody_answers(paths):
    """
    A console nobody sits at sends nothing at all — no newline, no EOF.
    The prompt has to fail open when the timeout runs out, not wait for a
    line that will never come: firstboot.sh is blocked behind it.
    """
    engine = FakeEngine()
    keymgmt.KeyslotStore("tank").set_slot(
        "passphrase", keymgmt.wrap_key(os.urandom(32), b"correct")
    )
    unlocker = keymgmt.PoolUnlocker(engine)

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    result = {}

    def run():
        start = time.monotonic()
        result["ok"] = unlocker.try_tty_unlock("tank", slave_path, timeout=2)
        result["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=10)

    os.close(master_fd)
    os.close(slave_fd)
    assert not thread.is_alive(), "the prompt ignored its timeout"
    assert result["ok"] is False
    assert result["elapsed"] < 6
    assert not any(c == "zfs" and a == "load_key_file" for c, a, _ in engine.calls)


def test_tty_unlock_with_no_passphrase_keyslot(paths):
    engine = FakeEngine()
    unlocker = keymgmt.PoolUnlocker(engine)
    master_fd, slave_fd = pty.openpty()
    try:
        assert unlocker.try_tty_unlock("tank", os.ttyname(slave_fd), timeout=10) is False
    finally:
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def test_create_encrypted_pool_writes_two_keyslots(paths):
    engine = FakeEngine()
    provisioner = keymgmt.PoolProvisioner(engine)

    provisioner.create_encrypted_pool(
        pool_name="tank", vdev_type="mirror", devices="/dev/sda /dev/sdb",
        master_passphrase="correct horse battery staple",
    )

    store = keymgmt.KeyslotStore("tank")
    assert store.get_slot("passphrase") is not None
    assert store.get_slot("usb") is not None
    assert any(c == "zfs" and a == "pool_create_encrypted" for c, a, _ in engine.calls)


def test_create_encrypted_pool_writes_usb_devices(paths, tmp_path):
    engine = FakeEngine()
    usb_device_dir = tmp_path / "usbkey"

    provisioner = keymgmt.PoolProvisioner(engine)
    provisioner.create_encrypted_pool(
        pool_name="tank", vdev_type="mirror", devices="/dev/sda /dev/sdb",
        master_passphrase="a very long master passphrase",
        usb_devices=["/dev/sdc1"],
    )

    assert (usb_device_dir / keymgmt.USB_KEY_FILENAME).exists()
    written = (usb_device_dir / keymgmt.USB_KEY_FILENAME).read_bytes()
    assert len(written) == 32

    # the USB unlock keyslot must accept exactly what was written to the stick
    slot = keymgmt.KeyslotStore("tank").get_slot("usb")
    raw_key = keymgmt.unwrap_key(slot, written)
    assert len(raw_key) == 32
