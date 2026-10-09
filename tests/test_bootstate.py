"""The rollback record on the ESP, as the core and the WebUI see it."""

from bootstate import (CommandExecutionError, CommandTranslationError, confirm_boot, forget,
                       set_autorollback, summary)

ROLLED_BACK = """autorollback: yes
state: rolled-back
trial-kernel: 6.18.60-1-freedomnas
failed-kernel: 6.18.60-1-freedomnas
restored-kernel: 6.18.54-3-freedomnas
reason: the initramfs reached the emergency shell
date: 20261009-101500
snapshot: rpool/ROOT/debian@pre-update-20261009-100000
fs-rollback: yes
failed-dataset: rpool/ROOT/debian-failed-20261009-101500
"""


class FakeConfig:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class FakeEngine:
    def __init__(self, state="", missing=False, failing=()):
        self.state = state
        self.missing = missing
        self.failing = set(failing)
        self.calls = []

    def execute(self, category, action, timeout=30, **kwargs):
        self.calls.append((action, kwargs))
        if self.missing and category == "bootloader":
            raise CommandTranslationError(f"Command not found: {category}.{action}")
        if action in self.failing:
            raise CommandExecutionError(f"{action} failed")
        return (self.state if action == "boot_state" else ""), ""


def test_the_boot_is_confirmed_with_the_configured_setting():
    engine = FakeEngine()
    assert confirm_boot(engine, FakeConfig()) is True
    assert engine.calls == [("boot_ok", {"autorollback": "yes"})]
    engine = FakeEngine()
    confirm_boot(engine, FakeConfig(**{"system.autorollback": False}))
    assert engine.calls == [("boot_ok", {"autorollback": "no"})]


def test_an_edition_without_a_uki_has_nothing_to_confirm():
    engine = FakeEngine(missing=True)
    assert confirm_boot(engine, FakeConfig()) is False
    assert summary(engine, FakeConfig())["supported"] is False


def test_a_failed_confirmation_never_stops_the_boot():
    assert confirm_boot(FakeEngine(failing={"boot_ok"}), FakeConfig()) is False


def test_the_summary_reports_a_rollback():
    report = summary(FakeEngine(ROLLED_BACK), FakeConfig())
    assert report["supported"] and report["autorollback"] and report["state"] == "rolled-back"
    assert report["rollback"] == {
        "date": "20261009-101500",
        "failed_kernel": "6.18.60-1-freedomnas",
        "restored_kernel": "6.18.54-3-freedomnas",
        "reason": "the initramfs reached the emergency shell",
        "snapshot": "rpool/ROOT/debian@pre-update-20261009-100000",
        "system_restored": True,
        "failed_dataset": "rpool/ROOT/debian-failed-20261009-101500",
    }


def test_no_rollback_no_record():
    report = summary(FakeEngine("autorollback: yes\nstate: trial\ntries-left: 2\n"), FakeConfig())
    assert report["state"] == "trial" and report["rollback"] is None


def test_the_setting_goes_to_the_esp_and_to_the_configuration():
    engine, config = FakeEngine(), FakeConfig()
    set_autorollback(engine, config, False)
    assert engine.calls == [("autorollback_set", {"autorollback": "no"})]
    assert config.get("system.autorollback") is False


def test_forgetting_can_destroy_the_failed_system():
    engine = FakeEngine(ROLLED_BACK)
    assert forget(engine, destroy_failed=True) == "rpool/ROOT/debian-failed-20261009-101500"
    assert ("dataset_destroy_recursive", {"dataset": "rpool/ROOT/debian-failed-20261009-101500"}) in engine.calls
    assert engine.calls[-1] == ("rollback_forget", {})


def test_only_a_failed_root_dataset_is_ever_destroyed():
    engine = FakeEngine(ROLLED_BACK.replace("debian-failed-20261009-101500", "debian"))
    assert forget(engine, destroy_failed=True) is None
    assert [a for a, _ in engine.calls] == ["boot_state", "rollback_forget"]
