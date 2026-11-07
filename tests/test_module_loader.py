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


def isolated(dirs, profile_failures=None):
    engine = Engine()
    engine.profile_failures = profile_failures or {}
    instance = ModuleLoader(engine, None, modules_dir=str(dirs[0]))
    instance.modules_dirs = dirs
    return instance


def broken_module(base, folder, manifest=None, source=None, raw_manifest=None):
    path = base / folder
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(raw_manifest if raw_manifest is not None else json.dumps(manifest))
    (path / "module.py").write_text(source if source is not None else MODULE.format(id=folder))
    return path


@pytest.mark.parametrize("folder, kwargs, reason", [
    ("badjson", {"raw_manifest": '{"id": "badjson",'}, "line 1 column"),
    ("notobject", {"raw_manifest": '["x"]'}, "not a JSON object"),
    ("syntax", {"manifest": {"id": "syntax", "entry_point": "module:M"}, "source": "def broken(:\n"},
     "SyntaxError in"),
    ("imports", {"manifest": {"id": "imports", "entry_point": "module:M"}, "source": "import fnas_missing\n"},
     "ModuleNotFoundError"),
    ("raises", {"manifest": {"id": "raises", "entry_point": "module:M"},
                "source": "from orchestrator import FreedomNASModule\nclass M(FreedomNASModule):\n"
                          "    name = 'raises'\n    def __init__(self, e, c): raise RuntimeError('boom')\n"
                          "    def start(self): pass\n    def stop(self): pass\n    def health_check(self): return True\n"},
     "RuntimeError: boom"),
    ("noclass", {"manifest": {"id": "noclass", "entry_point": "module:Missing"}}, "no class 'Missing'"),
    ("noentry", {"manifest": {"id": "noentry"}}, "names no entry_point"),
    ("badentry", {"manifest": {"id": "badentry", "entry_point": "module"}}, "Malformed entry_point"),
])
def test_a_module_that_cannot_be_loaded_is_reported_and_the_others_load(tmp_path, folder, kwargs, reason):
    base = tmp_path / "community"
    broken_module(base, folder, **kwargs)
    module(base, "zz_good", {"id": "good", "entry_point": "module:M"})
    found = isolated([base])
    assert [m["id"] for m, _ in found.instances()] == ["good"]
    assert list(found.failed) == [folder]
    assert reason in found.failed[folder]


def test_a_module_whose_profile_was_left_out_is_not_loaded(tmp_path):
    base = tmp_path / "community"
    module(base, "proxy", {"id": "proxy", "entry_point": "module:M"})
    module(base, "zz_good", {"id": "good", "entry_point": "module:M"})
    found = isolated([base], {"proxy": "Invalid JSON in profile fragment proxy-debian.json, line 1 column 2: x"})
    assert [m["id"] for m, _ in found.instances()] == ["good"]
    assert "proxy-debian.json" in found.failed["proxy"]


def test_a_module_for_another_edition_is_not_a_failure(tmp_path):
    base = tmp_path / "community"
    broken_module(base, "bsd", {"id": "bsd", "entry_point": "module:M", "supported_os": ["freebsd"]},
                  source="def broken(:\n")
    found = isolated([base])
    assert found.instances() == [] and found.failed == {}
