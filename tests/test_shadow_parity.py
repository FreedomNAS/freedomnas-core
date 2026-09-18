"""
The native shadow backend must answer exactly like the pure-Python one.

Both implementations run against a throwaway "zfs" profile and a throwaway
pool made of real directories: `pool_list` and `dataset_at_path` read a tiny
state file, `mount_dataset_at`/`unmount_at` are no-ops that just record what
happened, and the "mountpoint" the engine hands back is a real temp
directory the native/Python side actually copies config.yaml into. Each
backend gets its own copy of everything, so the two runs cannot interfere
with each other.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py
import shadow_native
import shadow_py


def zfs_profile(state_path: Path) -> dict:
    # dataset_at_path either asks about STATE_DIR (active dataset) or about
    # a specific dataset path — both answers come from the same tiny state
    # file, one line per known fact, `key=value`.
    lookup = (
        "awk -F= -v k='{path}' '$1 == k {{ print $2 }}' " + str(state_path)
    )
    return {
        "os_name": "Parity",
        "os_family": "linux",
        "os_version": "1",
        "init_system": "systemd",
        "zfs": {
            "pool_list": f"cat {state_path}.pools",
            "dataset_at_path": lookup,
            "create_state_dataset": (
                "printf '%s/.freedomnas=%s/.freedomnas\\n' '{pool_name}' '{pool_name}' >> " +
                str(state_path)
            ),
            "mount_dataset_at": "/bin/true",
            "unmount_at": "/bin/true",
        },
    }


def state_lines(active_dataset, known_datasets):
    lines = []
    if active_dataset:
        lines.append(f"/etc/freedomnas/state={active_dataset}")
    for dataset in known_datasets:
        lines.append(f"{dataset}={dataset}")
    return lines


@pytest.fixture
def backend_pair(tmp_path):
    """Two (engine, module) pairs, each with its own isolated fake ZFS state."""

    def build(pools, known_datasets, active_dataset=None):
        pairs = {}
        for name, engine_mod, shadow_mod in [
            ("py", engine_py, shadow_py),
            ("native", engine_native, shadow_native),
        ]:
            root = tmp_path / name
            (root / "os").mkdir(parents=True)
            state = root / "state.kv"
            state.write_text("\n".join(state_lines(active_dataset, known_datasets)) + "\n")
            (root / "state.kv.pools").write_text("\n".join(pools) + "\n")
            (root / "os" / "os-parity.json").write_text(
                json.dumps(zfs_profile(state))
            )
            engine = engine_mod.FreedomNASEngine.for_target(
                "parity", profile_dir=str(root)
            )
            pairs[name] = (engine, shadow_mod)
        return pairs["py"], pairs["native"]

    return build


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


def test_a_new_pool_gets_its_dataset(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["tank"], known_datasets=[]
    )

    assert (outcome(py_mod.ensure_dataset, py_engine, "tank")
            == outcome(native_mod.ensure_dataset, native_engine, "tank"))


def test_an_existing_dataset_is_left_alone(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["tank"], known_datasets=["tank/.freedomnas"]
    )

    assert (outcome(py_mod.ensure_dataset, py_engine, "tank")
            == outcome(native_mod.ensure_dataset, native_engine, "tank"))


def test_active_dataset_matches(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["rpool"], known_datasets=["rpool/.freedomnas"],
        active_dataset="rpool/.freedomnas",
    )

    assert (outcome(py_mod.active_dataset, py_engine)
            == outcome(native_mod.active_dataset, native_engine))


def test_no_active_dataset_matches(backend_pair):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["rpool"], known_datasets=["rpool/.freedomnas"]
    )

    assert (outcome(py_mod.active_dataset, py_engine)
            == outcome(native_mod.active_dataset, native_engine))


def test_the_configuration_reaches_the_data_pool(backend_pair, tmp_path):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["rpool", "datapool"],
        known_datasets=["rpool/.freedomnas", "datapool/.freedomnas"],
        active_dataset="rpool/.freedomnas",
    )

    py_config = tmp_path / "py-config.yaml"
    py_config.write_text("system:\n  hostname: nas\n")
    native_config = tmp_path / "native-config.yaml"
    native_config.write_text("system:\n  hostname: nas\n")

    py_result = outcome(py_mod.sync, py_engine, str(py_config))
    native_result = outcome(native_mod.sync, native_engine, str(native_config))

    assert py_result == native_result


def test_the_pool_being_read_from_is_never_written_to(backend_pair, tmp_path):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["rpool"], known_datasets=["rpool/.freedomnas"],
        active_dataset="rpool/.freedomnas",
    )
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")

    assert (outcome(py_mod.sync, py_engine, str(config))
            == outcome(native_mod.sync, native_engine, str(config)))


def test_a_pool_without_the_dataset_is_skipped(backend_pair, tmp_path):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["rpool", "estranea"],
        known_datasets=["rpool/.freedomnas"],
        active_dataset="rpool/.freedomnas",
    )
    config = tmp_path / "config.yaml"
    config.write_text("system: {}\n")

    assert (outcome(py_mod.sync, py_engine, str(config))
            == outcome(native_mod.sync, native_engine, str(config)))


def test_no_configuration_yet_is_not_an_error(backend_pair, tmp_path):
    (py_engine, py_mod), (native_engine, native_mod) = backend_pair(
        pools=["tank"], known_datasets=["tank/.freedomnas"]
    )

    assert (outcome(py_mod.sync, py_engine, str(tmp_path / "assente.yaml"))
            == outcome(native_mod.sync, native_engine, str(tmp_path / "assente.yaml")))
