"""
Boot environments: the copy before an update, pruning only our own copies,
and refusing to destroy the system that runs or boots next.
"""

import pytest

import bootenv
from bootenv import BootEnvProblem, CommandTranslationError


class FakeEngine:
    def __init__(self, rows, supported=True, inside=False):
        self.rows = list(rows)
        self.supported = supported
        self.inside = inside
        self.calls = []

    def get_command(self, category, action, **kwargs):
        if not self.supported or (category == "updates" and not self.inside):
            raise CommandTranslationError("no")
        return "bectl"

    fingerprint = "old"

    def get_profile_value(self, category, key):
        return "/tmp/freedomnas-be"

    def _flags(self, name, flags):
        self.rows = [(r[0], flags if r[0] == name else r[1], *r[2:]) for r in self.rows]

    def execute(self, category, action, **kwargs):
        if not self.supported:
            raise CommandTranslationError("no")
        self.calls.append((action, kwargs.get("be")))
        if action == "be_list":
            return "\n".join("\t".join(r) for r in self.rows), ""
        if action == "be_create":
            self.rows.append((kwargs["be"], "-", "-", "8K", "2026-11-01 10:00"))
        if action == "be_destroy":
            self.rows = [r for r in self.rows if r[0] != kwargs["be"]]
        if action == "be_fingerprint":
            return self.fingerprint, ""
        if action == "be_activate_once":
            self._flags(kwargs["be"], "T")
        if action == "be_activate":
            self.rows = [(r[0], r[1].replace("R", "").replace("T", "") or "-", *r[2:]) for r in self.rows]
            name = kwargs["be"]
            self.rows = [(r[0], (r[1].strip("-") + "R") if r[0] == name else r[1], *r[2:]) for r in self.rows]
        return "", ""


class Config:
    def __init__(self, autorollback=True):
        self.values = {"system.autorollback": autorollback}

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def state_file(monkeypatch, tmp_path):
    monkeypatch.setattr(bootenv, "state_path", lambda engine: tmp_path / "bootenv.json")
    return tmp_path / "bootenv.json"


def row(name, flags="-"):
    return (name, flags, "/" if "N" in flags else "-", "1G", "2026-10-10 15:51")


def test_the_listing_is_parsed():
    envs = bootenv.parse_list("freebsd\tNR\t/\t2.65G\t2026-10-10 15:51\n"
                              "pre-update-20261101-100000\tT\t-\t8K\t2026-11-01 10:00\nbroken\n")
    assert [e["name"] for e in envs] == ["freebsd", "pre-update-20261101-100000"]
    assert envs[0]["running"] and envs[0]["next_boot"] and not envs[0]["ours"]
    assert envs[1]["once"] and envs[1]["next_boot"] and envs[1]["ours"]


def test_nothing_happens_where_there_are_no_boot_environments():
    engine = FakeEngine([], supported=False)
    assert bootenv.create_before_update(engine, "20261101-100000") is None


def test_the_running_system_is_kept_before_an_update():
    engine = FakeEngine([row("freebsd", "NR")])
    assert bootenv.create_before_update(engine, "20261101-100000") == "pre-update-20261101-100000"
    assert ("be_create", "pre-update-20261101-100000") in engine.calls


def test_only_our_oldest_copies_are_pruned():
    ours = [row(f"pre-update-2026110{i}-100000") for i in range(1, 7)]
    engine = FakeEngine([row("freebsd", "NR"), row("15.1-RELEASE_2026-10-20_120000")] + ours)
    destroyed = bootenv.prune(engine, keep=5)
    assert destroyed == ["pre-update-20261101-100000"]
    assert "15.1-RELEASE_2026-10-20_120000" in [r[0] for r in engine.rows]


def test_an_active_copy_is_never_pruned():
    ours = [row("pre-update-20261101-100000", "R")] + [row(f"pre-update-2026110{i}-100000") for i in range(2, 8)]
    engine = FakeEngine([row("freebsd", "N")] + ours)
    destroyed = bootenv.prune(engine, keep=5)
    assert "pre-update-20261101-100000" not in destroyed
    assert destroyed == ["pre-update-20261102-100000"]


def test_the_running_or_next_system_cannot_be_destroyed():
    engine = FakeEngine([row("freebsd", "N"), row("pre-update-20261101-100000", "R")])
    with pytest.raises(BootEnvProblem):
        bootenv.destroy(engine, "freebsd")
    with pytest.raises(BootEnvProblem):
        bootenv.destroy(engine, "pre-update-20261101-100000")


def test_an_unknown_or_odd_name_is_refused():
    engine = FakeEngine([row("freebsd", "NR")])
    with pytest.raises(BootEnvProblem):
        bootenv.activate(engine, "nothere")
    with pytest.raises(BootEnvProblem):
        bootenv.destroy(engine, "-F x")


def test_activating_makes_it_the_default():
    engine = FakeEngine([row("freebsd", "NR"), row("pre-update-20261101-100000")])
    bootenv.activate(engine, "pre-update-20261101-100000")
    assert ("be_activate", "pre-update-20261101-100000") in engine.calls


def updated(engine, config=None, change=True, succeeded=True):
    update = bootenv.begin_update(engine, "20261101-100000")
    if change:
        engine.fingerprint = "new"
    return bootenv.finish_update(engine, config or Config(), update, succeeded)


def test_an_update_goes_into_a_copy_that_boots_once_on_trial(state_file):
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    assert updated(engine) == {"result": "trial", "be": "update-20261101-100000"}
    assert ("be_activate_once", "update-20261101-100000") in engine.calls
    assert ("be_fix_loader", None) in engine.calls
    assert bootenv.pending_trial(engine) == "update-20261101-100000"


def test_an_update_that_changed_nothing_leaves_no_copy():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    assert updated(engine, change=False)["result"] == "unchanged"
    assert [r[0] for r in engine.rows] == ["freebsd"]


def test_a_failed_update_leaves_no_copy():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    assert updated(engine, succeeded=False)["result"] == "failed"
    assert [r[0] for r in engine.rows] == ["freebsd"]
    assert bootenv.pending_trial(engine) is None


def test_without_automatic_rollback_the_copy_is_the_default_at_once():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    assert updated(engine, Config(autorollback=False))["result"] == "default"
    assert ("be_activate", "update-20261101-100000") in engine.calls
    assert bootenv.pending_trial(engine) is None


def test_a_copy_that_started_becomes_the_default():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    updated(engine)
    engine.rows = [("freebsd", "R", "-", "1G", "x"), ("update-20261101-100000", "N", "/", "1G", "x")]
    assert bootenv.confirm_boot(engine) == "confirmed"
    assert ("be_activate", "update-20261101-100000") in engine.calls
    assert bootenv.rollback_notice(engine) is None


def test_a_copy_that_did_not_start_is_recorded_as_rolled_back():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    updated(engine)
    engine.rows = [("freebsd", "NR", "/", "1G", "x"), ("update-20261101-100000", "-", "-", "1G", "x")]
    assert bootenv.confirm_boot(engine) == "rolled-back"
    notice = bootenv.rollback_notice(engine)
    assert notice["failed"] == "update-20261101-100000" and notice["restored"] == "freebsd"
    assert "update-20261101-100000" not in bootenv.prune(engine, keep=0)
    assert bootenv.forget(engine, destroy_failed=True) == "update-20261101-100000"
    assert bootenv.rollback_notice(engine) is None


def test_a_new_update_drops_one_never_started():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    updated(engine)
    engine.fingerprint = "old"
    update = bootenv.begin_update(engine, "20261102-100000")
    assert "update-20261101-100000" not in [r[0] for r in engine.rows]
    assert update["be"] == "update-20261102-100000"


def test_where_updates_go_into_copies_no_pre_update_copy_is_made():
    engine = FakeEngine([row("freebsd", "NR")], inside=True)
    assert bootenv.create_before_update(engine, "20261101-100000") is None


def test_old_pre_update_copies_go_first_where_updates_go_into_a_copy():
    old = [row("pre-update-20261020-100000"), row("pre-update-20261030-100000")]
    new = [row(f"update-2026110{i}-100000") for i in range(1, 7)]
    engine = FakeEngine([row("freebsd", "NR")] + old + new, inside=True)
    destroyed = bootenv.prune(engine, keep=5)
    assert destroyed == ["pre-update-20261020-100000", "pre-update-20261030-100000", "update-20261101-100000"]
    assert [r[0] for r in engine.rows if r[0].startswith("update-")] == [f"update-2026110{i}-100000" for i in range(2, 7)]


def test_a_running_pre_update_copy_is_kept_where_updates_go_into_a_copy():
    engine = FakeEngine([row("pre-update-20261030-100000", "NR"), row("update-20261101-100000")], inside=True)
    assert bootenv.prune(engine, keep=5) == []


def test_copies_are_ordered_by_their_stamp_not_their_name():
    rows = [row("update-20261101-100000"), row("pre-update-20261105-100000")]
    engine = FakeEngine([row("freebsd", "NR")] + rows)
    assert bootenv.prune(engine, keep=1) == ["update-20261101-100000"]
