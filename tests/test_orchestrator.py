import json
import threading

import pytest

try:
    from freedomnas import orchestrator as orch
except ImportError:
    import orchestrator as orch


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Module(orch.FreedomNASModule):
    def __init__(self, answers):
        super().__init__(None, None)
        self.answers = list(answers)
        self.starts = 0
        self.stops = 0

    @property
    def name(self):
        return "fake"

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def health_check(self):
        return True

    def health(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def make(tmp_path, answers, module_id="storage"):
    clock = Clock()
    o = orch.Orchestrator(None, None, run_dir=tmp_path, clock=clock)
    module = Module(answers)
    o.register(module, module_id, "Storage")
    o.start_all()
    return o, module, clock


def state(o, module_id="storage"):
    return o.get_status()[module_id]


def test_a_healthy_module_is_checked_again_after_the_interval(tmp_path):
    o, module, clock = make(tmp_path, [(True, "tank ONLINE")])
    assert o.check_due() == ["storage"]
    assert (state(o).state, state(o).detail) == (orch.ModuleState.HEALTHY, "tank ONLINE")
    clock.now += orch.CHECK_INTERVAL - 1
    assert o.check_due() == []
    clock.now += 1
    assert o.check_due() == ["storage"]
    assert module.starts == 2


def test_a_problem_is_shown_and_never_blocks_the_module(tmp_path):
    o, _, clock = make(tmp_path, [(False, "tank is DEGRADED")])
    for _ in range(10):
        o.check_due()
        clock.now += orch.CHECK_INTERVAL
    assert (state(o).state, state(o).detail, state(o).attempt) == (orch.ModuleState.PROBLEM, "tank is DEGRADED", 0)


def test_a_failing_module_is_retried_after_10_20_30_60_seconds_then_blocked(tmp_path):
    o, module, clock = make(tmp_path, [RuntimeError("boom")])
    o.check_due()
    seen = []
    for delay in orch.RETRY_DELAYS:
        assert state(o).state == orch.ModuleState.RETRYING
        seen.append(state(o).attempt)
        clock.now += delay - 1
        assert o.check_due() == []
        clock.now += 1
        assert o.check_due() == ["storage"]
    assert seen == [1, 2, 3, 4]
    assert state(o).state == orch.ModuleState.BLOCKED and "boom" in state(o).detail
    assert module.stops == len(orch.RETRY_DELAYS)
    clock.now += 3600
    assert o.check_due() == []


def test_a_retry_that_works_makes_the_module_healthy_again(tmp_path):
    o, _, clock = make(tmp_path, [RuntimeError("boom"), (True, "")])
    o.check_due()
    clock.now += orch.RETRY_DELAYS[0]
    o.check_due()
    assert (state(o).state, state(o).attempt) == (orch.ModuleState.HEALTHY, 0)


def test_a_module_that_does_not_answer_in_time_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "CHECK_TIMEOUT", 0.05)
    release = threading.Event()
    o, module, _ = make(tmp_path, [(True, "")])
    module.health = lambda: release.wait(2) or (True, "")
    o.check_due()
    release.set()
    assert state(o).state == orch.ModuleState.RETRYING
    assert "did not answer" in state(o).detail


def test_a_restart_request_checks_a_blocked_module_again(tmp_path):
    o, module, clock = make(tmp_path, [RuntimeError("boom")])
    o.check_due()
    for delay in orch.RETRY_DELAYS:
        clock.now += delay
        o.check_due()
    assert state(o).state == orch.ModuleState.BLOCKED
    module.answers = [(True, "back")]
    (tmp_path / orch.RESTART_DIR).mkdir()
    (tmp_path / orch.RESTART_DIR / "storage").touch()
    (tmp_path / orch.RESTART_DIR / "nonexistent").touch()
    assert o.take_restart_requests() == ["storage"]
    assert list((tmp_path / orch.RESTART_DIR).iterdir()) == []
    o.check_due()
    assert (state(o).state, state(o).detail) == (orch.ModuleState.HEALTHY, "back")


def test_the_file_tells_the_webui_and_a_blocked_module_stays_blocked_after_a_restart_of_serve(tmp_path):
    o, _, clock = make(tmp_path, [RuntimeError("boom")])
    o.mark_failed("broken", "manifest.json names no entry_point")
    o.check_due()
    o.write()
    data = json.loads((tmp_path / orch.HEALTH_FILE).read_text())
    assert [(m["id"], m["state"]) for m in data["modules"]] == [("storage", "retrying"), ("broken", "failed")]
    assert (data["modules"][0]["attempt"], data["modules"][0]["attempts"]) == (1, 4)
    for delay in orch.RETRY_DELAYS:
        clock.now += delay
        o.check_due()
    o.write()

    again, _, _ = make(tmp_path, [(True, "")])
    again.keep_blocked()
    again.start_all()
    assert again.check_due() == []
    assert state(again).state == orch.ModuleState.BLOCKED and "boom" in state(again).detail
