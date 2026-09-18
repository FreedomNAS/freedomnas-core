"""
The native pools backend must answer exactly like the pure-Python one.

Both implementations run against a throwaway "zfs" profile whose
pool_add_cache/pool_add_log/pool_remove_device actions record their
arguments to a plain text file instead of talking to real ZFS — each
backend gets its own copy of that file, so the two runs cannot interfere
with each other. What is compared is the returned result — what went in,
what did not — and, for a refused removal, the exception class and message
text.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py
import pools_native
import pools_py


def zfs_profile(log_path: Path, failing_action: str = "") -> dict:
    def action(name: str, template: str) -> str:
        if name == failing_action:
            return "/bin/false"
        return f"printf '%s\\n' '{name} {{pool_name}} " + template + f"' >> {log_path}"

    return {
        "os_name": "Parity",
        "os_family": "linux",
        "os_version": "1",
        "init_system": "systemd",
        "zfs": {
            "pool_add_cache": action("pool_add_cache", "{devices}"),
            "pool_add_log": action("pool_add_log", "{devices}"),
            "pool_remove_device": action("pool_remove_device", "{device}"),
        },
    }


@pytest.fixture
def backend_pair(tmp_path):
    """Two (engine, module) pairs, each with its own isolated fake ZFS log."""

    def build(failing_action=""):
        pairs = {}
        for name, engine_mod, pool_mod in [
            ("py", engine_py, pools_py),
            ("native", engine_native, pools_native),
        ]:
            root = tmp_path / name
            (root / "os").mkdir(parents=True)
            log_path = root / "calls.log"
            log_path.write_text("")
            (root / "os" / "os-parity.json").write_text(
                json.dumps(zfs_profile(log_path, failing_action))
            )
            engine = engine_mod.FreedomNASEngine.for_target(
                "parity", profile_dir=str(root)
            )
            pairs[name] = (engine, pool_mod)
        return pairs["py"], pairs["native"]

    return build


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


def test_cache_and_log_go_in_together(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair()

    py_result = outcome(py_mod.attach_devices, py_engine, "tank",
                        cache=["/dev/ssd1"], log=["/dev/ssd2"])
    native_result = outcome(native_mod.attach_devices, native_engine, "tank",
                            cache=["/dev/ssd1"], log=["/dev/ssd2"])

    assert py_result == native_result


def test_several_devices_go_in_one_command(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair()

    py_result = outcome(py_mod.attach_devices, py_engine, "tank",
                        cache=["/dev/a", "/dev/b"])
    native_result = outcome(native_mod.attach_devices, native_engine, "tank",
                            cache=["/dev/a", "/dev/b"])

    assert py_result == native_result


def test_nothing_asked_for_runs_nothing(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair()

    assert (outcome(py_mod.attach_devices, py_engine, "tank")
            == outcome(native_mod.attach_devices, native_engine, "tank"))


def test_empty_and_falsy_devices_are_filtered(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair()

    py_result = outcome(py_mod.attach_devices, py_engine, "tank",
                        cache=["", "/dev/a", ""], log=[])
    native_result = outcome(native_mod.attach_devices, native_engine, "tank",
                            cache=["", "/dev/a", ""], log=[])

    assert py_result == native_result


def test_a_cache_that_will_not_attach_is_reported_not_raised(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair("pool_add_cache")

    py_result = outcome(py_mod.attach_devices, py_engine, "tank",
                        cache=["/dev/rotto"], log=["/dev/ssd"])
    native_result = outcome(native_mod.attach_devices, native_engine, "tank",
                            cache=["/dev/rotto"], log=["/dev/ssd"])

    assert py_result == native_result


def test_removal_lets_zfs_refuse_identically(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair("pool_remove_device")

    assert (outcome(py_mod.detach_device, py_engine, "tank", "/dev/ssd1")
            == outcome(native_mod.detach_device, native_engine, "tank", "/dev/ssd1"))


def test_removal_succeeds_identically(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair()

    assert (outcome(py_mod.detach_device, py_engine, "tank", "/dev/ssd1")
            == outcome(native_mod.detach_device, native_engine, "tank", "/dev/ssd1"))
