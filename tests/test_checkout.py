"""
conftest.py keeps an installed freedomnas-core out of the way. If it did
not, the modules would catch the installed copy's exception classes while
the tests raise this checkout's, and results would depend on what the
machine happens to have installed.
"""

from pathlib import Path

REPO = Path(__file__).parent.parent


def test_the_suite_tests_this_checkout():
    import engine
    import pools
    import reconcile

    assert pools.CommandExecutionError is engine.CommandExecutionError
    assert reconcile.CommandExecutionError is engine.CommandExecutionError
    assert Path(pools.__file__).resolve().parent == REPO.resolve()
