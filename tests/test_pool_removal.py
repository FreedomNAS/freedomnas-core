"""Exporting and destroying pools: never the system's, never the live configuration's."""

import pytest

import keymgmt
import pool_removal
import shadow
from pool_removal import RemovalProblem

ROWS = [
    ("rpool", "none"), ("rpool/ROOT", "/"),
    ("mainpool", "none"), ("mainpool/.freedomnas", "/etc/freedomnas/state"), ("mainpool/data", "/data"),
    ("homepool", "none"), ("homepool/home", "/home"),
    ("tank", "/tank"), ("tank/.freedomnas", "/etc/freedomnas/state"), ("tank/media", "/tank/media"),
]


class FakeEngine:
    def __init__(self, keystatus="-"):
        self.calls = []
        self.keystatus = keystatus

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        if action == "dataset_at_path":
            return ("rpool/ROOT" if kw["path"] == "/" else ""), ""
        if action == "dataset_list":
            return "\n".join(f"{n}\t1\t2\t3\t{mp}" for n, mp in ROWS), ""
        if action == "pool_keystatus":
            return self.keystatus, ""
        return "", ""

    def actions(self):
        return [a for a, _ in self.calls]


@pytest.fixture(autouse=True)
def live_config_on_mainpool(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "active_dataset", lambda engine: "mainpool/.freedomnas")
    monkeypatch.setattr(keymgmt, "KEYSLOT_DIR", tmp_path / "keyslots")


@pytest.mark.parametrize("pool, reason", [
    ("rpool", "runs from"),
    ("mainpool", "configuration this system is using"),
    ("homepool", "system directory"),
])
def test_what_the_system_needs_stays(pool, reason):
    engine = FakeEngine()
    assert reason in pool_removal.problem(engine, pool)
    for action in ("export", "destroy"):
        with pytest.raises(RemovalProblem, match=reason):
            pool_removal.remove(engine, pool, action)
    assert not {"pool_export", "pool_destroy"} & set(engine.actions())


def test_a_data_pool_with_its_own_state_dataset_can_go():
    assert pool_removal.problem(FakeEngine(), "tank") is None


def test_export_keeps_the_keyslots_and_destroy_forgets_them():
    keymgmt.KeyslotStore("tank").set_slot("usb", {"salt": "00"})
    engine = FakeEngine(keystatus="available")
    assert pool_removal.contents(engine, "tank")["encrypted"]
    assert pool_removal.remove(engine, "tank", "export") == {
        "pool": "tank", "action": "export", "forgot_keyslots": False}
    assert keymgmt.KeyslotStore("tank").exists()
    result = pool_removal.remove(engine, "tank", "destroy")
    assert result["forgot_keyslots"] and not keymgmt.KeyslotStore("tank").exists()
    [(_, kw)] = [c for c in engine.calls if c[0] == "pool_destroy"]
    assert kw == {"pool_name": "tank"}


def test_the_contents_are_the_pools_datasets_only():
    found = pool_removal.contents(FakeEngine(), "tank")
    assert found["datasets"] == ["tank", "tank/.freedomnas", "tank/media"]
    assert found["mountpoints"] == ["/tank", "/etc/freedomnas/state", "/tank/media"]
    assert not found["encrypted"]
