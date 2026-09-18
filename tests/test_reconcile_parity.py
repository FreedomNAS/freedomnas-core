"""
The native ReconciliationEngine must answer exactly like the pure-Python
one, end to end through `reconcile()` — the only method the native backend
implements (see reconcile_native.py; the internal `_do_*`/`_apply_*` split
is tested against the pure implementation alone in test_reconcile.py).

Both implementations run against a throwaway profile whose system/zfs/
users/samba/nfs/services actions are no-ops or read a tiny state file
instead of talking to the real OS — each backend gets its own copy of
everything, so the two runs cannot interfere with each other. What is
compared is the returned ActionResult list — action, params, target_state,
error — and, for a refused mode, the exception class and message text.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py
import reconcile_native
import reconcile_py


def zfs_profile(pools_state: Path, users_state: Path, failing: set) -> dict:
    def cmd(name: str) -> str:
        return "/bin/false" if name in failing else "/bin/true"

    return {
        "os_name": "Parity",
        "os_family": "linux",
        "os_version": "1",
        "init_system": "systemd",
        "system": {
            "hostname_set": cmd("hostname_set"),
            "timezone_set": cmd("timezone_set"),
        },
        "zfs": {
            "pool_list": f"cat {pools_state}",
            "pool_import": cmd("pool_import"),
        },
        "users": {
            "get_user_info": f"grep -x {{username}} {users_state}",
            "create": cmd("users.create"),
            "add_to_group": cmd("add_to_group"),
            "remove_from_group": cmd("remove_from_group"),
            "delete": cmd("delete"),
        },
        "samba": {
            "smb_service": "smbd",
            "create_share": cmd("create_share"),
            "delete_share": cmd("delete_share"),
        },
        "nfs": {
            "nfs_service": "nfs-server",
            "export_add": cmd("export_add"),
            "export_remove": cmd("export_remove"),
            "exportfs_reload": cmd("exportfs_reload"),
        },
        "services": {
            "enable": cmd("enable"),
            "start": cmd("start"),
        },
    }


class FakeConfig:
    def __init__(self, state, nfs_shares=None):
        self.state = state
        self._nfs_shares = nfs_shares if nfs_shares is not None else state.get("nfs_shares", [])

    def to_dict(self):
        return self.state

    def get_nfs_shares(self):
        return self._nfs_shares


@pytest.fixture
def backend_pair(tmp_path):
    """Two (ReconciliationEngine, ) instances, each with its own fake OS state."""

    def build(state, imported_pools=(), existing_users=(), nfs_shares=None, failing=()):
        pairs = {}
        for name, engine_mod, reconcile_mod in [
            ("py", engine_py, reconcile_py),
            ("native", engine_native, reconcile_native),
        ]:
            root = tmp_path / name
            (root / "os").mkdir(parents=True)
            pools_state = root / "pools.state"
            pools_state.write_text("\n".join(imported_pools) + "\n" if imported_pools else "")
            users_state = root / "users.state"
            users_state.write_text("\n".join(existing_users) + "\n" if existing_users else "")
            (root / "os" / "os-parity.json").write_text(
                json.dumps(zfs_profile(pools_state, users_state, set(failing)))
            )
            engine = engine_mod.FreedomNASEngine.for_target(
                "parity", profile_dir=str(root)
            )
            config = FakeConfig(state, nfs_shares)
            pairs[name] = reconcile_mod.ReconciliationEngine(engine, config)
        return pairs["py"], pairs["native"]

    return build


def as_tuples(results):
    return [(r.action, r.params, r.target_state, r.error) for r in results]


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", as_tuples(fn(*args, **kwargs)))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


DISASTER_STATE = {
    "system": {"hostname": "nas", "timezone": "UTC"},
    "storage": {"pools": [{"name": "already"}, {"name": "tank"}]},
    "users": [{"username": "alice", "groups": ["sudo"]}],
    "installed_features": [
        {"id": "samba-cifs", "enabled": True},
        {"id": "nfs", "enabled": False},
        {"id": "unmapped-thing", "enabled": True},
    ],
    "smb_shares": [{"name": "media", "path": "/tank/media"}],
    "nfs_shares": [{"name": "home", "path": "/tank/home", "clients": "*"}],
}


def test_disaster_recovery_end_to_end(backend_pair):
    py, native = backend_pair(DISASTER_STATE, imported_pools=["already"])

    assert (outcome(py.reconcile, mode="disaster_recovery")
            == outcome(native.reconcile, mode="disaster_recovery"))


def test_disaster_recovery_with_a_failing_action(backend_pair):
    py, native = backend_pair(
        DISASTER_STATE, imported_pools=["already"], failing={"users.create"}
    )

    assert (outcome(py.reconcile, mode="disaster_recovery")
            == outcome(native.reconcile, mode="disaster_recovery"))


def test_declarative_reapplies_the_whole_current_config(backend_pair):
    state = {
        "system": {"hostname": "nas"},
        "users": [{"username": "alice", "groups": []}],
        "storage": {"pools": [{"name": "tank"}]},
        "smb_shares": [{"name": "media", "path": "/tank/media"}],
    }
    py, native = backend_pair(state, existing_users=["alice"])

    assert (outcome(py.reconcile, None, "declarative")
            == outcome(native.reconcile, None, "declarative"))


def test_hybrid_with_desired_path(backend_pair, tmp_path):
    state = {
        "system": {"hostname": "old-name"},
        "smb_shares": [{"name": "old-share", "path": "/tank/old"}],
    }
    py, native = backend_pair(state)

    desired_text = (
        "system:\n  hostname: new-name\n"
        "smb_shares:\n  - name: new-share\n    path: /tank/new\n"
    )
    py_desired = tmp_path / "py-desired.yaml"
    py_desired.write_text(desired_text)
    native_desired = tmp_path / "native-desired.yaml"
    native_desired.write_text(desired_text)

    assert (outcome(py.reconcile, str(py_desired), "hybrid")
            == outcome(native.reconcile, str(native_desired), "hybrid"))


def test_unknown_mode_fails_identically(backend_pair):
    py, native = backend_pair({})

    assert outcome(py.reconcile, None, "yearly") == outcome(native.reconcile, None, "yearly")


def test_no_changes_is_an_empty_run(backend_pair):
    py, native = backend_pair({"system": {"hostname": "nas"}})

    assert outcome(py.reconcile) == outcome(native.reconcile) == ("ok", [])
