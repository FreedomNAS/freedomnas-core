"""offline_restore: what an installer's chroot takes from a data pool's copy."""

from pathlib import Path

import pytest
import yaml

import offline_restore
from reconcile import ActionResult


class Engine:
    def __init__(self, tmp_path: Path, offered=()):
        self.config_dir = tmp_path / "etc"
        self.ssh_dir = tmp_path / "ssh"
        self.profile = {"paths": {"config_dir": str(self.config_dir)}}
        self.offered = list(offered)
        self.calls = []
        self.reloaded = 0

    def get_profile_value(self, category, key):
        return {("system", "ssh_host_key_dir"): str(self.ssh_dir),
                ("system", "ssh_service"): "sshd"}[(category, key)]

    def execute(self, category, action, **kwargs):
        self.calls.append((category, action, kwargs))
        if (category, action) == ("packages", "module_search"):
            return "".join(f"{p}\t1.0\tdesc\n" for p in self.offered), ""
        return "", ""

    def reload_profile(self):
        self.reloaded += 1


def copy(tmp_path: Path, modules) -> Path:
    source = tmp_path / "copy"
    (source / "identity" / "tls").mkdir(parents=True)
    (source / "identity" / "ssh").mkdir(parents=True)
    (source / "config.yaml").write_text(yaml.safe_dump({
        "system": {"hostname": "box", "initialized": True, "edition": "freebsd"},
        "modules": modules,
    }))
    (source / "identity" / "tls" / "ca.pem").write_text("CA")
    (source / "identity" / "tls" / "ca-key.pem").write_text("CA KEY")
    (source / "identity" / "ssh" / "ssh_host_ed25519_key").write_text("HOST")
    (source / "identity" / "ssh" / "ssh_host_ed25519_key.pub").write_text("HOST PUB")
    return source


class Module:
    prepared = []

    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail

    def prepare(self, desired):
        if self.fail:
            raise RuntimeError("no network")
        Module.prepared.append((self.name, desired))
        return [ActionResult(action=f"{self.name}.install", params={}, target_state="applied")]


def loader(present, modules):
    class Loader:
        def __init__(self, engine, config):
            pass

        def discover(self):
            return [{"id": m} for m in present]

        def instances(self):
            return [({"id": m.name}, m) for m in modules]
    return Loader


@pytest.fixture(autouse=True)
def reset():
    Module.prepared = []


def test_identity_and_modules_come_back_without_starting_anything(tmp_path, monkeypatch):
    engine = Engine(tmp_path, offered=["freedomnas-module-containers", "freedomnas-module-proxy"])
    source = copy(tmp_path, {"containers": {"enabled": True}, "proxy": {"enabled": True}, "empty": {}})
    modules = [Module("containers"), Module("proxy")]
    monkeypatch.setattr("reconcile.ModuleLoader", loader([], modules))
    monkeypatch.setattr(offline_restore, "ModuleLoader", loader([], modules))

    result = offline_restore.restore(engine, source)

    assert sorted(result["identity"]) == ["ssh/ssh_host_ed25519_key", "ssh/ssh_host_ed25519_key.pub",
                                          "tls/ca-key.pem", "tls/ca.pem"]
    assert (engine.config_dir / "tls" / "ca.pem").read_text() == "CA"
    assert (engine.ssh_dir / "ssh_host_ed25519_key").stat().st_mode & 0o777 == 0o600
    installs = [kw["package_name"] for c, a, kw in engine.calls if (c, a) == ("packages", "install")]
    assert installs == ["freedomnas-module-containers", "freedomnas-module-proxy"]
    assert not [c for c in engine.calls if c[0] == "services"]
    assert engine.reloaded == 1
    assert Module.prepared == [("containers", {"enabled": True}), ("proxy", {"enabled": True})]
    assert all(r.target_state == "applied" for r in result["results"])
    assert yaml.safe_load((source / "config.yaml").read_text())["system"]["edition"] == "freebsd"


def test_a_module_already_installed_is_only_prepared(tmp_path, monkeypatch):
    engine = Engine(tmp_path, offered=["freedomnas-module-proxy"])
    source = copy(tmp_path, {"proxy": {"enabled": True}})
    modules = [Module("proxy")]
    monkeypatch.setattr("reconcile.ModuleLoader", loader(["proxy"], modules))
    monkeypatch.setattr(offline_restore, "ModuleLoader", loader(["proxy"], modules))

    result = offline_restore.restore(engine, source)

    assert not [c for c in engine.calls if c[:2] == ("packages", "install")]
    assert engine.reloaded == 0
    assert [r.action for r in result["results"]] == ["proxy.install"]


def test_a_module_that_cannot_prepare_is_reported_and_the_others_go_on(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    source = copy(tmp_path, {"containers": {"enabled": True}, "proxy": {"enabled": True}})
    modules = [Module("containers", fail=True), Module("proxy")]
    monkeypatch.setattr("reconcile.ModuleLoader", loader(["containers", "proxy"], modules))
    monkeypatch.setattr(offline_restore, "ModuleLoader", loader(["containers", "proxy"], modules))

    result = offline_restore.restore(engine, source)

    assert [(r.action, r.target_state) for r in result["results"]] == [
        ("modules.containers.prepare", "failed"), ("proxy.install", "applied")]


def test_a_source_without_a_configuration_is_refused(tmp_path):
    with pytest.raises(offline_restore.OfflineRestoreError):
        offline_restore.restore(Engine(tmp_path), tmp_path)
