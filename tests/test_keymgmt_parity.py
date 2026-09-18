"""
The native key management backend must answer exactly like the pure-Python
one — and, critically, a keyslot written by one must open with the other:
that is the whole point of wrapping with a standard, deterministic KDF and
AEAD cipher instead of anything bespoke.

Both implementations run against a throwaway "zfs"/"media" profile whose
actions read a tiny state file or are plain no-ops instead of talking to
the real OS. PoolUnlocker/PoolProvisioner's own path constants (keyslot
directory, tmpfs runtime key directory, USB mountpoint) are monkeypatched
to temp directories, exactly like tests/test_keymgmt.py does for the pure
implementation. TTY unlock is exercised against a real pty — termios only
works on one.
"""

import json
import os
import pty
import time
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py
import keymgmt_native
import keymgmt_py


def zfs_profile(pools_state: Path, removable_state: Path) -> dict:
    return {
        "os_name": "Parity",
        "os_family": "linux",
        "os_version": "1",
        "init_system": "systemd",
        "zfs": {
            "pool_list": f"cat {pools_state}",
            "pool_keystatus": f"grep -x '{{pool_name}} .*' {pools_state} | cut -d' ' -f2",
            "load_key_file": "/bin/true",
            "mount_pool": "/bin/true",
            "pool_create_encrypted": "/bin/true",
        },
        "media": {
            "list_removable": f"cat {removable_state}",
            "mount_ro": "/bin/true",
            "mount_rw": "/bin/true",
            "unmount": "/bin/true",
        },
    }


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


@pytest.fixture
def backend_pair(tmp_path, monkeypatch):
    """Two (engine, keymgmt module) pairs, each with its own isolated state."""

    def build(pool_lines=(), removable_lines=()):
        pairs = {}
        for name, engine_mod, keymgmt_mod in [
            ("py", engine_py, keymgmt_py),
            ("native", engine_native, keymgmt_native),
        ]:
            root = tmp_path / name
            (root / "os").mkdir(parents=True)
            pools_state = root / "pools.state"
            pools_state.write_text("\n".join(pool_lines) + "\n" if pool_lines else "")
            removable_state = root / "removable.state"
            removable_state.write_text(
                "\n".join(removable_lines) + "\n" if removable_lines else ""
            )
            (root / "os" / "os-parity.json").write_text(
                json.dumps(zfs_profile(pools_state, removable_state))
            )

            keyslot_dir = root / "keyslots"
            runtime_dir = root / "run"
            usb_mount = root / "usbkey"
            monkeypatch.setattr(keymgmt_mod, "KEYSLOT_DIR", keyslot_dir)
            monkeypatch.setattr(keymgmt_mod, "RUNTIME_KEY_DIR", runtime_dir)
            monkeypatch.setattr(keymgmt_mod, "USB_MOUNTPOINT", usb_mount)

            engine = engine_mod.FreedomNASEngine.for_target(
                "parity", profile_dir=str(root)
            )
            pairs[name] = (engine, keymgmt_mod, keyslot_dir, runtime_dir, usb_mount)
        return pairs["py"], pairs["native"]

    return build


# ---------------------------------------------------------------------------
# wrap_key / unwrap_key — cross-backend interop, not just same-backend parity
# ---------------------------------------------------------------------------

def test_python_wraps_native_unwraps():
    raw_key = os.urandom(32)
    slot = keymgmt_py.wrap_key(raw_key, b"a shared secret")
    assert keymgmt_native.unwrap_key(slot, b"a shared secret") == raw_key


def test_native_wraps_python_unwraps():
    raw_key = os.urandom(32)
    slot = keymgmt_native.wrap_key(raw_key, b"a shared secret")
    assert keymgmt_py.unwrap_key(slot, b"a shared secret") == raw_key


def test_wrong_secret_rejected_identically():
    slot = keymgmt_py.wrap_key(os.urandom(32), b"right")
    assert (outcome(keymgmt_py.unwrap_key, slot, b"wrong")[0] == "raised")
    assert (outcome(keymgmt_native.unwrap_key, slot, b"wrong")[0] == "raised")
    assert (outcome(keymgmt_py.unwrap_key, slot, b"wrong")[1]
            == outcome(keymgmt_native.unwrap_key, slot, b"wrong")[1])  # exception class


# ---------------------------------------------------------------------------
# KeyslotStore — a file written by one backend opens with the other
# ---------------------------------------------------------------------------

def test_keyslot_file_written_by_python_reads_in_native(tmp_path):
    raw_key = os.urandom(32)
    slot = keymgmt_py.wrap_key(raw_key, b"secret")
    keymgmt_py.KeyslotStore("tank", base_dir=tmp_path).set_slot("usb", slot)

    reloaded = keymgmt_native.KeyslotStore("tank", base_dir=tmp_path).get_slot("usb")
    assert reloaded == slot
    assert keymgmt_native.unwrap_key(reloaded, b"secret") == raw_key


def test_keyslot_file_written_by_native_reads_in_python(tmp_path):
    raw_key = os.urandom(32)
    slot = keymgmt_native.wrap_key(raw_key, b"secret")
    keymgmt_native.KeyslotStore("tank", base_dir=tmp_path).set_slot("passphrase", slot)

    reloaded = keymgmt_py.KeyslotStore("tank", base_dir=tmp_path).get_slot("passphrase")
    assert reloaded == slot
    assert keymgmt_py.unwrap_key(reloaded, b"secret") == raw_key


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_locked_pools_matches(backend_pair):
    # pool_keystatus greps this same state file for "<name> <keystatus>";
    # pool_list only ever looks at the first field (the pool name).
    (py_engine, py_mod, *_), (native_engine, native_mod, *_) = backend_pair(
        pool_lines=["rpool available", "tank unavailable"]
    )
    assert (outcome(py_mod.PoolUnlocker(py_engine).list_locked_pools)
            == outcome(native_mod.PoolUnlocker(native_engine).list_locked_pools)
            == ("ok", ["tank"]))


# ---------------------------------------------------------------------------
# USB zero-touch unlock
# ---------------------------------------------------------------------------

def test_usb_unlock_with_no_keyslot_configured(backend_pair):
    (py_engine, py_mod, *_), (native_engine, native_mod, *_) = backend_pair()
    assert (outcome(py_mod.PoolUnlocker(py_engine).try_usb_unlock, "tank")
            == outcome(native_mod.PoolUnlocker(native_engine).try_usb_unlock, "tank"))


def test_usb_unlock_with_matching_and_wrong_key(backend_pair):
    (py_engine, py_mod, py_keyslots, _, py_usb), \
        (native_engine, native_mod, native_keyslots, _, native_usb) = backend_pair(
            removable_lines=["sdb1 1 part"]
        )

    raw_key = os.urandom(32)
    usb_key_bytes = os.urandom(32)
    slot = py_mod.wrap_key(raw_key, usb_key_bytes)
    py_mod.KeyslotStore("tank", base_dir=py_keyslots).set_slot("usb", slot)
    native_mod.KeyslotStore("tank", base_dir=native_keyslots).set_slot("usb", slot)

    py_usb.mkdir(parents=True)
    (py_usb / "freedomnas.key").write_bytes(usb_key_bytes)
    native_usb.mkdir(parents=True)
    (native_usb / "freedomnas.key").write_bytes(usb_key_bytes)

    assert (outcome(py_mod.PoolUnlocker(py_engine).try_usb_unlock, "tank")
            == outcome(native_mod.PoolUnlocker(native_engine).try_usb_unlock, "tank")
            == ("ok", True))

    # now with the wrong key on the stick
    (py_usb / "freedomnas.key").write_bytes(os.urandom(32))
    (native_usb / "freedomnas.key").write_bytes(os.urandom(32))
    assert (outcome(py_mod.PoolUnlocker(py_engine).try_usb_unlock, "tank")
            == outcome(native_mod.PoolUnlocker(native_engine).try_usb_unlock, "tank")
            == ("ok", False))


# ---------------------------------------------------------------------------
# TTY passphrase fallback — against a real pty, one backend at a time
# ---------------------------------------------------------------------------

def run_tty_unlock(unlocker, pool_name, feed, timeout=10):
    """
    Run try_tty_unlock in its own forked process, feeding bytes from `feed`
    on the pty master side. Returns the unlock result.

    This is how try_tty_unlock always runs in production —
    `python -m freedomnas unlock-pools` is its own short-lived process,
    started by firstboot.sh, never a background thread inside a
    long-running interpreter — so the test runs it the same way.

    Threads calling into the native core are not the reason: the TLS flags
    in nim/freedomnas.nimble fixed the segfault they used to cause, and
    test_native_threads.py keeps it fixed.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.close(read_fd)
        try:
            ok = unlocker.try_tty_unlock(pool_name, slave_path, timeout=timeout)
        except BaseException:
            ok = None
        os.write(write_fd, b"1" if ok is True else (b"0" if ok is False else b"?"))
        os._exit(0)

    os.close(slave_fd)
    os.close(write_fd)
    time.sleep(0.2)
    for chunk, delay in feed:
        os.write(master_fd, chunk)
        time.sleep(delay)

    # Bounded: a prompt that ignores its timeout must fail this test, not
    # hang the whole suite.
    limit = time.monotonic() + timeout + 10
    while os.waitpid(pid, os.WNOHANG) == (0, 0):
        if time.monotonic() > limit:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(master_fd)
            os.close(read_fd)
            return "hung"
        time.sleep(0.05)
    raw = os.read(read_fd, 1)
    os.close(master_fd)
    os.close(read_fd)
    return {b"1": True, b"0": False}.get(raw)


def test_tty_unlock_matches(backend_pair):
    (py_engine, py_mod, py_keyslots, *_), \
        (native_engine, native_mod, native_keyslots, *_) = backend_pair()

    raw_key = os.urandom(32)
    slot = py_mod.wrap_key(raw_key, b"the master passphrase")
    py_mod.KeyslotStore("tank", base_dir=py_keyslots).set_slot("passphrase", slot)
    native_mod.KeyslotStore("tank", base_dir=native_keyslots).set_slot("passphrase", slot)

    py_result = run_tty_unlock(
        py_mod.PoolUnlocker(py_engine), "tank",
        [(b"wrong\n", 0.2), (b"the master passphrase\n", 0.2)],
    )
    native_result = run_tty_unlock(
        native_mod.PoolUnlocker(native_engine), "tank",
        [(b"wrong\n", 0.2), (b"the master passphrase\n", 0.2)],
    )
    assert py_result is True
    assert native_result is True


def test_tty_unlock_eof_matches(backend_pair):
    (py_engine, py_mod, py_keyslots, *_), \
        (native_engine, native_mod, native_keyslots, *_) = backend_pair()

    slot = py_mod.wrap_key(os.urandom(32), b"secret")
    py_mod.KeyslotStore("tank", base_dir=py_keyslots).set_slot("passphrase", slot)
    native_mod.KeyslotStore("tank", base_dir=native_keyslots).set_slot("passphrase", slot)

    py_result = run_tty_unlock(py_mod.PoolUnlocker(py_engine), "tank", [(b"\x04", 0.2)])
    native_result = run_tty_unlock(
        native_mod.PoolUnlocker(native_engine), "tank", [(b"\x04", 0.2)]
    )
    assert py_result is False
    assert native_result is False


def test_tty_unlock_timeout_matches(backend_pair):
    """Nothing typed at all: both give up after the timeout instead of waiting."""
    (py_engine, py_mod, py_keyslots, *_), \
        (native_engine, native_mod, native_keyslots, *_) = backend_pair()

    slot = py_mod.wrap_key(os.urandom(32), b"secret")
    py_mod.KeyslotStore("tank", base_dir=py_keyslots).set_slot("passphrase", slot)
    native_mod.KeyslotStore("tank", base_dir=native_keyslots).set_slot("passphrase", slot)

    for mod, engine in ((py_mod, py_engine), (native_mod, native_engine)):
        start = time.monotonic()
        result = run_tty_unlock(mod.PoolUnlocker(engine), "tank", [], timeout=2)
        assert result is False, f"{mod.__name__}: {result}"
        assert time.monotonic() - start < 8


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def test_create_encrypted_pool_matches(backend_pair):
    (py_engine, py_mod, py_keyslots, _, py_usb), \
        (native_engine, native_mod, native_keyslots, _, native_usb) = backend_pair()

    py_mod.PoolProvisioner(py_engine).create_encrypted_pool(
        pool_name="tank", vdev_type="mirror", devices="/dev/sda /dev/sdb",
        master_passphrase="correct horse battery staple",
        usb_devices=["/dev/sdc1"],
    )
    native_mod.PoolProvisioner(native_engine).create_encrypted_pool(
        pool_name="tank", vdev_type="mirror", devices="/dev/sda /dev/sdb",
        master_passphrase="correct horse battery staple",
        usb_devices=["/dev/sdc1"],
    )

    py_usb_key = (py_usb / "freedomnas.key").read_bytes()
    native_usb_key = (native_usb / "freedomnas.key").read_bytes()
    assert len(py_usb_key) == len(native_usb_key) == 32

    py_slot = py_mod.KeyslotStore("tank", base_dir=py_keyslots).get_slot("usb")
    native_slot = native_mod.KeyslotStore("tank", base_dir=native_keyslots).get_slot("usb")

    # each backend's own USB key opens its own usb keyslot...
    py_raw = py_mod.unwrap_key(py_slot, py_usb_key)
    native_raw = native_mod.unwrap_key(native_slot, native_usb_key)
    assert len(py_raw) == len(native_raw) == 32

    # ...and the passphrase keyslot opens identically on both backends.
    py_pass_slot = py_mod.KeyslotStore("tank", base_dir=py_keyslots).get_slot("passphrase")
    native_pass_slot = native_mod.KeyslotStore(
        "tank", base_dir=native_keyslots
    ).get_slot("passphrase")
    py_pass_raw = py_mod.unwrap_key(py_pass_slot, b"correct horse battery staple")
    native_pass_raw = native_mod.unwrap_key(
        native_pass_slot, b"correct horse battery staple"
    )
    assert py_pass_raw == py_raw
    assert native_pass_raw == native_raw
