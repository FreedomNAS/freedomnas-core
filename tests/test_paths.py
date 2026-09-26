"""
An installed FreedomNAS finds its own pieces from where it is, whatever root
the edition chose: nothing in the code names /usr/lib or /usr/local.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def _installed_paths(root: Path):
    """paths.py as a package would put it: <root>/core/freedomnas/paths.py."""
    package = root / "core" / "freedomnas"
    package.mkdir(parents=True)
    shutil.copy(REPO / "paths.py", package / "paths.py")
    spec = importlib.util.spec_from_file_location("installed_paths", package / "paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("root", ["usr/lib/freedomnas", "usr/local/lib/freedomnas"])
def test_the_root_is_found_from_the_installed_file(tmp_path, root):
    install_root = tmp_path / root
    (install_root / "profiles" / "os").mkdir(parents=True)
    paths = _installed_paths(install_root)

    assert paths.INSTALL_ROOT == install_root
    assert paths.profiles_dir() == install_root / "profiles"
    assert paths.modules_candidates()[0] == install_root / "modules"


def test_no_profiles_is_reported_not_guessed(tmp_path):
    paths = _installed_paths(tmp_path / "usr/local/lib/freedomnas")
    assert paths.profiles_dir() is None
