"""The health check reads what the profile's pool_list actually prints."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modules" / "storage"))

from module import StorageModule

POOLS = (
    "tank\t3958241161216\t1979120580608\t1979120580608\t50\tONLINE\n"
    "backup\t1979120580608\t98956029030\t1880164551578\t5\tDEGRADED\n"
)


class FakeEngine:
    def __init__(self, stdout):
        self.stdout = stdout

    def execute(self, category, action, **kwargs):
        return self.stdout, ""


class FakeConfig:
    def __init__(self, names):
        self.names = names

    def get_pools(self):
        return [{"name": name} for name in self.names]


def _module(stdout, tracked):
    mod = StorageModule(engine=FakeEngine(stdout), config=FakeConfig(tracked))
    mod.start()
    return mod


def test_every_tracked_pool_online_passes():
    assert _module(POOLS, ["tank"]).health_check() is True


def test_a_degraded_pool_fails_the_check():
    assert _module(POOLS, ["tank", "backup"]).health_check() is False


def test_a_pool_that_is_not_tracked_is_not_our_problem():
    assert _module(POOLS, ["tank"]).health_check() is True


def test_short_lines_are_skipped():
    assert _module("tank\t3958241161216\n", ["tank"]).health_check() is True
