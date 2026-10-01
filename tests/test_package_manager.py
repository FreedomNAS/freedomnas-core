"""
Behaviour of the PackageManager, against the real
freedomnas-profiles/packages/ tree — there is no throwaway fixture for this
one: the manifest/category shape is the contract, and the real data is what
exercises every branch (init-system overlays, pkgbuilds, required vs
optional categories) without having to hand-write a second copy of it.
"""

from pathlib import Path

import pytest

from package_manager import PackageManager

PACKAGES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles" / "packages")


@pytest.fixture
def pm():
    return PackageManager(packages_dir=PACKAGES_DIR)


def test_missing_directory_raises():
    with pytest.raises(FileNotFoundError):
        PackageManager(packages_dir="/nonexistent/path")


def test_categories_include_core(pm):
    ids = [c["id"] for c in pm.get_categories()]
    assert "core" in ids
    assert "samba-cifs" in ids


def test_category_info(pm):
    info = pm.get_category_info("core")
    assert info["required"] is True
    assert info["file"] == "core.json"


def test_category_info_unknown_raises(pm):
    with pytest.raises(ValueError):
        pm.get_category_info("not-a-category")


def test_required_and_optional_categories(pm):
    required = pm.get_required_categories()
    optional = pm.get_optional_categories()
    assert "core" in required
    assert "samba-cifs" in optional
    assert set(required).isdisjoint(optional)
    all_ids = {c["id"] for c in pm.get_categories()}
    assert set(required) | set(optional) == all_ids


def test_packages_for_debian(pm):
    packages = pm.get_packages_for_os("core", "debian")
    assert "zfsutils-linux" in packages
    assert "python3" in packages


def test_packages_for_unsupported_edition_raises(pm):
    with pytest.raises(ValueError):
        pm.get_packages_for_os("core", "windows")


def test_packages_for_edition_not_in_category_raises(pm):
    # supported_editions lists it, but a category's JSON might not define it —
    # exercised generically: any category/edition pair not present raises.
    category = pm._load_category("core")
    missing = next(
        (e for e in ("debian", "freebsd", "arch", "artix", "void", "gentoo")
         if e not in category),
        None,
    )
    if missing is None:
        pytest.skip("core.json defines every supported edition")
    with pytest.raises(ValueError):
        pm.get_packages_for_os("core", missing)


def test_init_system_adds_packages_for_artix(pm):
    without_init = set(pm.get_packages_for_os("core", "artix"))
    with_openrc = set(pm.get_packages_for_os("core", "artix", init_system="openrc"))
    assert with_openrc >= without_init
    assert with_openrc - without_init  # openrc adds at least one package


def test_unknown_init_system_adds_nothing(pm):
    without_init = pm.get_packages_for_os("core", "artix")
    with_bogus = pm.get_packages_for_os("core", "artix", init_system="not-a-real-init")
    assert with_bogus == without_init


def test_services_for_os(pm):
    services = pm.get_services_for_os("core", "debian")
    assert isinstance(services, list)


def test_early_services_come_from_init_overlay(pm):
    early = pm.get_early_services_for_os("core", "artix", init_system="openrc")
    assert "zfs-import" in early or "zfs-mount" in early


def test_artix_openrc_gets_zfs_openrc_from_the_repository(pm):
    assert "zfs-openrc" in pm.get_packages_for_os("core", "artix", init_system="openrc")
    assert pm.get_pkgbuilds_for_os("core", "artix", init_system="openrc") == []


def test_pkgbuilds_empty_without_matching_init(pm):
    pkgbuilds = pm.get_pkgbuilds_for_os("core", "artix")
    assert pkgbuilds == []


def test_validate_category(pm):
    assert pm.validate_category("core") is True
    assert pm.validate_category("not-a-category") is False


def test_validate_edition(pm):
    assert pm.validate_edition("debian") is True
    assert pm.validate_edition("windows") is False


def test_category_is_cached_after_first_load(pm):
    first = pm.get_packages_for_os("core", "debian")
    # Mutate the cache directly; a second call must see the mutation if (and
    # only if) the category is truly cached rather than reloaded from disk.
    pm.categories["core"]["debian"]["packages"] = ["only-this-one"]
    second = pm.get_packages_for_os("core", "debian")
    assert second == ["only-this-one"]
    assert first != second


def test_get_all_packages_for_os_defaults_to_optional_categories(pm):
    result = pm.get_all_packages_for_os("debian")
    assert "core" not in result  # core is required, not optional
    assert "samba-cifs" in result


def test_get_all_packages_for_os_with_explicit_categories(pm):
    result = pm.get_all_packages_for_os("debian", categories=["core"])
    assert list(result.keys()) == ["core"]
    assert "zfsutils-linux" in result["core"]


def test_config_paths(pm):
    # Whatever core.json declares (possibly none) — just check the type and
    # that an unknown edition still raises like everything else here.
    paths = pm.get_config_paths("core", "debian")
    assert isinstance(paths, list)
    with pytest.raises(ValueError):
        pm.get_config_paths("core", "windows-nt")
