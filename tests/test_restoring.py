"""
The restore lock: the WebUI shows only the restore page while it exists, the
reconcile writes its steps into it, and only a finished one can be taken away.
"""

import restoring


def test_a_restore_that_ends_well_takes_the_lock_away(tmp_path):
    lock = tmp_path / restoring.LOCK_NAME
    restoring.begin(lock, "restored")
    progress = restoring.Progress(lock)
    progress.plan(["Hostname", "Account sarah"])
    progress.start(0)
    assert restoring.read(lock)["steps"][0]["state"] == "running"
    progress.done(0, [])
    progress.done(1, [])
    assert [s["state"] for s in restoring.read(lock)["steps"]] == ["done", "done"]
    progress.finish([])
    assert not lock.exists()


def test_a_failed_step_keeps_the_lock_finished_with_its_error(tmp_path):
    lock = tmp_path / restoring.LOCK_NAME
    restoring.begin(lock, "reinitialized")
    progress = restoring.Progress(lock)
    progress.plan(["Module containers"])
    progress.done(0, ["containers.runtime: Docker could not be installed"])
    progress.finish(["containers.runtime: Docker could not be installed"])
    data = restoring.read(lock)
    assert data["reason"] == "reinitialized" and data["finished"] and data["failed"]
    assert data["steps"][0] == {"label": "Module containers", "state": "failed",
                                "error": "containers.runtime: Docker could not be installed"}


def test_only_a_finished_restore_can_be_taken_away(tmp_path):
    lock = tmp_path / restoring.LOCK_NAME
    restoring.begin(lock)
    assert restoring.clear(lock) is False and lock.exists()
    restoring.Progress(lock).finish(["x: broken"])
    assert restoring.clear(lock) is True and not lock.exists()


def test_steps_are_named_for_people():
    assert restoring.step_label("users.sarah") == "Account sarah"
    assert restoring.step_label("share_groups.media") == "Group share media"
    assert restoring.step_label("system.hostname") == "Hostname"
