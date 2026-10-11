"""Core and community modules found together, core ids kept."""

import json

import pytest

from module_loader import ModuleLoader, ModuleLoaderException

MODULE = '''
from orchestrator import FreedomNASModule

class M(FreedomNASModule):
    name = "{id}"
    def start(self): pass
    def stop(self): pass
    def health_check(self): return True
'''


class Engine:
    os_type = "debian"


def module(base, folder, manifest):
    path = base / folder
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "module.py").write_text(MODULE.format(id=manifest["id"]))
    return path


def loader(dirs):
    instance = ModuleLoader.__new__(ModuleLoader)
    instance._engine = Engine()
    instance._config = None
    instance.modules_dirs = dirs
    return instance


def test_every_directory_is_scanned_core_first(tmp_path):
    core, community = tmp_path / "core", tmp_path / "community"
    module(core, "storage", {"id": "storage", "entry_point": "module:M"})
    module(community, "containers", {"id": "containers", "entry_point": "module:M"})
    assert [m["id"] for m in loader([core, community]).discover()] == ["storage", "containers"]


def test_a_community_module_cannot_take_a_core_id(tmp_path):
    core, community = tmp_path / "core", tmp_path / "community"
    module(core, "storage", {"id": "storage", "entry_point": "module:M"})
    shadow = module(community, "storage", {"id": "storage", "entry_point": "module:M"})
    [found] = loader([core, community]).discover()
    assert found["_dir"] != shadow


def test_the_default_directories_are_those_that_exist(tmp_path, monkeypatch):
    import paths
    present = tmp_path / "modules"
    present.mkdir()
    monkeypatch.setattr(paths, "modules_candidates", lambda: [tmp_path / "missing", present])
    assert ModuleLoader._get_default_modules_dirs() == [present]


def test_no_directory_at_all_is_an_error(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "modules_candidates", lambda: [tmp_path / "missing"])
    with pytest.raises(ModuleLoaderException):
        ModuleLoader._get_default_modules_dirs()


def test_an_explicit_directory_is_the_only_one(tmp_path):
    module(tmp_path, "containers", {"id": "containers", "entry_point": "module:M"})
    instance = ModuleLoader(Engine(), None, modules_dir=str(tmp_path))
    assert [m.name for _, m in instance.instances()] == ["containers"]
