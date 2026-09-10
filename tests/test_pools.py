"""Attaching a cache must never be able to take the pool with it."""

import pytest

from pools import CommandExecutionError, attach_devices, detach_device


class FakeEngine:
    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)

    def execute(self, category, action, **kwargs):
        self.calls.append((action, kwargs.get("devices") or kwargs.get("device")))
        if action in self.failing:
            raise CommandExecutionError("no such device")
        return "", ""


def test_cache_and_log_go_in_together():
    engine = FakeEngine()
    result = attach_devices(engine, "tank", cache=["/dev/ssd1"], log=["/dev/ssd2"])

    assert result["cache"] == ["/dev/ssd1"] and result["log"] == ["/dev/ssd2"]
    assert result["errors"] == []
    assert engine.calls == [("pool_add_cache", "/dev/ssd1"), ("pool_add_log", "/dev/ssd2")]


def test_several_devices_go_in_one_command():
    engine = FakeEngine()
    attach_devices(engine, "tank", cache=["/dev/a", "/dev/b"])
    assert engine.calls == [("pool_add_cache", "/dev/a /dev/b")]


def test_nothing_asked_for_runs_nothing():
    engine = FakeEngine()
    assert attach_devices(engine, "tank") == {"cache": [], "log": [], "errors": []}
    assert engine.calls == []


def test_a_cache_that_will_not_attach_is_reported_not_raised():
    engine = FakeEngine(failing={"pool_add_cache"})
    result = attach_devices(engine, "tank", cache=["/dev/rotto"], log=["/dev/ssd"])

    assert result["cache"] == []
    assert result["log"] == ["/dev/ssd"]          # l'altro entra comunque
    assert result["errors"][0]["role"] == "cache"


def test_removal_lets_zfs_refuse():
    engine = FakeEngine(failing={"pool_remove_device"})
    with pytest.raises(CommandExecutionError):
        detach_device(engine, "tank", "/dev/ssd1")
