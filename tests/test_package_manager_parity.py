"""
The native PackageManager must answer exactly like the pure-Python one,
against the real freedomnas-profiles/packages/ tree — no throwaway
fixture, for the same reason as test_package_manager.py: the manifest and
category shape is the contract.

Unlike the other ported modules, this one needs no engine handle at all —
it never calls engine.execute(), so there is nothing to fake with a
throwaway profile. Both backends just read the same files.
"""

from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import package_manager_native
import package_manager_py

PACKAGES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles" / "packages")

EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]
CATEGORIES = ["core", "samba-cifs", "nfs", "containers", "virtualization"]


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


@pytest.fixture
def pair():
    return (
        package_manager_py.PackageManager(packages_dir=PACKAGES_DIR),
        package_manager_native.PackageManager(packages_dir=PACKAGES_DIR),
    )


def test_missing_directory_fails_identically():
    assert (
        outcome(package_manager_py.PackageManager, packages_dir="/nonexistent/path")
        == outcome(package_manager_native.PackageManager, packages_dir="/nonexistent/path")
    )


def test_categories_match(pair):
    py, native = pair
    assert py.get_categories() == native.get_categories()


def test_required_and_optional_categories_match(pair):
    py, native = pair
    assert py.get_required_categories() == native.get_required_categories()
    assert py.get_optional_categories() == native.get_optional_categories()


@pytest.mark.parametrize("category_id", CATEGORIES)
def test_category_info_matches(pair, category_id):
    py, native = pair
    assert (outcome(py.get_category_info, category_id)
            == outcome(native.get_category_info, category_id))


def test_unknown_category_info_fails_identically(pair):
    py, native = pair
    assert (outcome(py.get_category_info, "not-a-category")
            == outcome(native.get_category_info, "not-a-category"))


@pytest.mark.parametrize("category_id", CATEGORIES)
@pytest.mark.parametrize("edition", EDITIONS)
def test_packages_match(pair, category_id, edition):
    py, native = pair
    assert (outcome(py.get_packages_for_os, category_id, edition)
            == outcome(native.get_packages_for_os, category_id, edition))


@pytest.mark.parametrize("category_id", CATEGORIES)
@pytest.mark.parametrize("edition", EDITIONS)
@pytest.mark.parametrize("init_system", ["", "openrc", "dinit", "runit", "s6", "bsd_rc", "bogus-init"])
def test_packages_with_init_system_match(pair, category_id, edition, init_system):
    py, native = pair
    kwargs = {"init_system": init_system} if init_system else {}
    assert (outcome(py.get_packages_for_os, category_id, edition, **kwargs)
            == outcome(native.get_packages_for_os, category_id, edition, **kwargs))
    assert (outcome(py.get_services_for_os, category_id, edition, **kwargs)
            == outcome(native.get_services_for_os, category_id, edition, **kwargs))
    assert (outcome(py.get_early_services_for_os, category_id, edition, **kwargs)
            == outcome(native.get_early_services_for_os, category_id, edition, **kwargs))
    assert (outcome(py.get_pkgbuilds_for_os, category_id, edition, **kwargs)
            == outcome(native.get_pkgbuilds_for_os, category_id, edition, **kwargs))


def test_unsupported_edition_fails_identically(pair):
    py, native = pair
    assert (outcome(py.get_packages_for_os, "core", "windows")
            == outcome(native.get_packages_for_os, "core", "windows"))


@pytest.mark.parametrize("category_id", CATEGORIES)
@pytest.mark.parametrize("edition", EDITIONS)
def test_config_paths_match(pair, category_id, edition):
    py, native = pair
    assert (outcome(py.get_config_paths, category_id, edition)
            == outcome(native.get_config_paths, category_id, edition))


@pytest.mark.parametrize("category_id", CATEGORIES)
def test_validate_category_matches(pair, category_id):
    py, native = pair
    assert py.validate_category(category_id) == native.validate_category(category_id)


def test_validate_category_unknown_matches(pair):
    py, native = pair
    assert py.validate_category("nope") is native.validate_category("nope") is False


@pytest.mark.parametrize("edition", EDITIONS + ["windows"])
def test_validate_edition_matches(pair, edition):
    py, native = pair
    assert py.validate_edition(edition) == native.validate_edition(edition)


@pytest.mark.parametrize("edition", EDITIONS)
def test_get_all_packages_for_os_default_matches(pair, edition):
    py, native = pair
    assert (outcome(py.get_all_packages_for_os, edition)
            == outcome(native.get_all_packages_for_os, edition))


@pytest.mark.parametrize("edition", EDITIONS)
def test_get_all_packages_for_os_explicit_matches(pair, edition):
    py, native = pair
    assert (outcome(py.get_all_packages_for_os, edition, categories=["core", "nfs"])
            == outcome(native.get_all_packages_for_os, edition, categories=["core", "nfs"]))
