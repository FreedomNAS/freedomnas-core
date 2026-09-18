"""
FreedomNAS Package Manager — backend selection.

Everything imports PackageManager from here, and this module decides which
implementation answers:

  FREEDOMNAS_PACKAGES=python   package_manager_py.py — pure Python (default)
  FREEDOMNAS_PACKAGES=native   package_manager_native.py — MANIFEST.json and
                               category JSON reading/querying run in Nim,
                               and an ImportError if it is not built
  FREEDOMNAS_PACKAGES=auto     native when importable, python otherwise

Unset, it follows FREEDOMNAS_ENGINE, so one variable moves the whole core
and the other pins this module back if only that needs reverting. Unlike
snapshots/pools/shadow/reconcile/keymgmt, the native side needs no engine
handle at all — it is pure, read-only JSON navigation — so it works
whichever engine backend is active, or with none open yet.

Both are held to the same behaviour by tests/test_package_manager_parity.py,
against the real freedomnas-profiles/packages/ tree.
"""

import logging
import os

logger = logging.getLogger("FreedomNAS.Packages")

BACKEND = os.environ.get(
    "FREEDOMNAS_PACKAGES",
    os.environ.get("FREEDOMNAS_ENGINE", "python"),
).strip().lower()


def _load_native():
    try:
        from freedomnas.package_manager_native import PackageManager
    except ImportError:
        from package_manager_native import PackageManager  # type: ignore[no-redef]
    return PackageManager


def _load_python():
    try:
        from freedomnas.package_manager_py import PackageManager
    except ImportError:
        from package_manager_py import PackageManager  # type: ignore[no-redef]
    return PackageManager


if BACKEND == "native":
    PackageManager = _load_native()
elif BACKEND == "auto":
    try:
        PackageManager = _load_native()
        BACKEND = "native"
    except ImportError as exc:
        logger.info("Native package manager unavailable (%s); using Python", exc)
        PackageManager = _load_python()
        BACKEND = "python"
else:
    PackageManager = _load_python()

__all__ = ["BACKEND", "PackageManager"]
