"""
Which implementation each switch picks, for every combination that matters.

The modules whose logic runs in Nim drive the engine through its native
handle, so they cannot sit on top of the Python engine. Setting
FREEDOMNAS_ENGINE=python — the rollback — has to take them back to Python
too, even when their own variable still says native, instead of leaving
them to fail on the first call.

A switch decides once, at import, so each case runs in a fresh interpreter.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

ENGINE_BOUND = ["reconcile", "snapshots", "pools", "shadow", "keymgmt"]


def backend_of(module: str, env: dict) -> str:
    code = (
        "import sys\n"
        "sys.path = [p for p in sys.path if 'freedomnas/core' not in p]\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        f"import {module}\n"
        f"print({module}.BACKEND)\n"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FREEDOMNAS_")}
    result = subprocess.run(
        [sys.executable, "-c", code], env={**clean, **env},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("module", ENGINE_BOUND)
def test_python_engine_keeps_the_module_on_python(module):
    variable = f"FREEDOMNAS_{module.upper()}"
    assert backend_of(module, {"FREEDOMNAS_ENGINE": "python", variable: "native"}) == "python"
    assert backend_of(module, {"FREEDOMNAS_ENGINE": "python", variable: "auto"}) == "python"


@pytest.mark.parametrize("module", ENGINE_BOUND)
def test_default_is_python(module):
    assert backend_of(module, {}) == "python"


@pytest.mark.parametrize("module", ENGINE_BOUND)
def test_native_engine_can_still_pin_the_module_back(module):
    variable = f"FREEDOMNAS_{module.upper()}"
    assert backend_of(module, {"FREEDOMNAS_ENGINE": "native", variable: "python"}) == "python"


@pytest.mark.parametrize("module", ENGINE_BOUND)
def test_native_engine_carries_the_module(module):
    pytest.importorskip("freedomnas_native", reason="native engine not built")
    assert backend_of(module, {"FREEDOMNAS_ENGINE": "native"}) == "native"


def test_a_python_engine_handed_to_native_code_is_named_not_crashed():
    pytest.importorskip("freedomnas_native", reason="native engine not built")
    import engine_native
    import engine_py

    profiles = str(REPO.parent / "freedomnas-profiles")
    python_engine = engine_py.FreedomNASEngine.for_target("debian", profile_dir=profiles)
    with pytest.raises(TypeError, match="FREEDOMNAS_ENGINE=native"):
        engine_native.native_handle(python_engine)


def test_the_suite_tests_this_checkout():
    """
    conftest.py keeps an installed freedomnas-core out of the way. If it
    did not, the modules would catch the installed copy's exception classes
    while the tests raise this checkout's, and results would depend on what
    the machine happens to have installed.
    """
    import engine_py
    import pools_py
    import reconcile_py

    assert pools_py.CommandExecutionError is engine_py.CommandExecutionError
    assert reconcile_py.CommandExecutionError is engine_py.CommandExecutionError
    assert Path(pools_py.__file__).resolve().parent == REPO.resolve()
