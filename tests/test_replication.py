"""Local replication: flags, checks, and jobs run through the real profile pipelines."""

import os
import time

import pytest

import replication
from engine import FreedomNASEngine
from replication import ReplicationProblem

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "..", "freedomnas-profiles")

FAKE_ZFS = r"""#!/bin/sh
trap 'kill $! 2>/dev/null; exit 130' INT
echo "$@" >> "$FAKE_LOG"
case "$1" in
  send)
    if [ "$2" = "-nvP" ]; then printf 'full\t%s\t3000\nsize\t3000\n' "$4"; exit 0; fi
    printf 'full\tx\t3000\n' >&2; printf 'size\t3000\n' >&2
    for i in 1 2 3; do
      printf '12:00:0%s\t%s\ttank/a@s1\n' "$i" "$((i * 1000))" >&2
      printf 'data%s' "$i"
      sleep "${FAKE_SLEEP:-0}" & wait $!
    done ;;
  receive) exec 3<&0; cat <&3 > "$FAKE_RECEIVED" & wait $! ;;
  get) printf 'tank/a\t-\nbackup/a\t1-abc-def\n' ;;
esac
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "zfs").write_text(FAKE_ZFS)
    (bin_dir / "zfs").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("FAKE_RECEIVED", str(tmp_path / "received"))
    monkeypatch.setattr(replication, "RUN_DIR", tmp_path / "run")
    eng = FreedomNASEngine.for_target("debian", profile_dir=PROFILES_DIR)
    monkeypatch.setattr(replication.datasets, "root_pool", lambda e: "rpool")
    monkeypatch.setattr(replication.datasets, "exists", lambda e, name: name in ("tank", "backup"))
    monkeypatch.setattr(replication.pool_import, "is_system_path",
                        lambda path: path == "/etc" or path.startswith("/etc/"))
    return eng


def wait(job, timeout=10):
    end = time.time() + timeout
    while job.state == "running" and time.time() < end:
        time.sleep(0.05)
    return job


def test_flags():
    assert replication.send_flags({"replicate": True, "raw": True, "compressed": True}) == "-R -w -c"
    assert replication.receive_flags({}) == "-u -s -x mountpoint"
    assert replication.receive_flags({"force": True, "no_mount": False, "resumable": False,
                                      "name_mode": "d", "exclude_mountpoint": False}) == "-F -d"
    assert replication.incremental({"from": "tank/a@s0"}, "tank/a@s1") == "-i tank/a@s0"
    assert replication.incremental({"from": "tank/a@s0", "intermediates": True}, "tank/a@s1") == "-I tank/a@s0"
    assert replication.incremental({"from": "tank/a#b0"}, "tank/a@s1") == "-i tank/a#b0"
    for bad in ({"from": "tank/b@s0"}, {"from": "-x"}, {"from": "tank/a#-b"}):
        with pytest.raises(ReplicationProblem):
            replication.incremental(bad, "tank/a@s1")
    with pytest.raises(ReplicationProblem):
        replication.send_flags({"raw": "yes"})
    with pytest.raises(ReplicationProblem):
        replication.receive_flags({"name_mode": "z"})


def test_targets_and_files(engine, tmp_path):
    assert replication.check_target(engine, "backup/a") == "backup/a"
    for bad in ("", "-F", "backup/a b", "rpool/x", "nopool/x", "backup/.freedomnas"):
        with pytest.raises(ReplicationProblem):
            replication.check_target(engine, bad)
    data = tmp_path / "data"
    data.mkdir()
    assert replication.check_file(str(data / "s.zfs"), must_exist=False) == str(data / "s.zfs")
    (data / "old.zfs").write_text("x")
    for path, exist in ((str(data / "old.zfs"), False), (str(data / "none.zfs"), True),
                        ("relative.zfs", False), ("/etc/x.zfs", False), (str(tmp_path / "no/x"), False)):
        with pytest.raises(ReplicationProblem):
            replication.check_file(path, must_exist=exist)


def test_replicate_job_reports_progress(engine, tmp_path):
    job = wait(replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/a",
                                              "raw": True}))
    assert job.state == "done", job.error
    d = job.to_dict()
    assert d["total"] == 3000 and d["done"] == 3000 and d["target"] == "backup/a"
    assert (tmp_path / "received").read_text() == "data1data2data3"
    log = sorted((tmp_path / "log").read_text().splitlines())
    assert log == ["receive -u -s -x mountpoint backup/a", "send -vP -w tank/a@s1"]


def test_a_second_job_into_the_same_target_is_refused(engine, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1")
    job = replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/a"})
    try:
        with pytest.raises(ReplicationProblem):
            replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/a"})
    finally:
        time.sleep(0.3)
        job.cancel()
        wait(job)


def test_cancel_stops_the_pipeline(engine, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    job = replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/b"})
    time.sleep(0.5)
    started = time.time()
    assert job.cancel()
    wait(job, timeout=4)
    assert time.time() - started < 3
    assert job.state == "cancelled" and job.error == ""
    assert job.to_dict()["done"] < 3000


def test_send_to_file_never_overwrites(engine, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    out = data / "s.zfs"
    job = wait(replication.send_to_file(engine, {"snapshot": "tank/a@s1", "file": str(out)}))
    assert job.state == "done" and out.read_text() == "data1data2data3"
    with pytest.raises(ReplicationProblem):
        replication.send_to_file(engine, {"snapshot": "tank/a@s1", "file": str(out)})


def test_receive_from_file(engine, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "in.zfs").write_text("stream")
    job = wait(replication.receive_from_file(engine, {"file": str(data / "in.zfs"),
                                                      "target": "backup/c"}))
    assert job.state == "done"
    assert (tmp_path / "received").read_text() == "stream"
    assert (tmp_path / "log").read_text().splitlines() == ["receive -u -x mountpoint backup/c"]


def test_estimate_tokens_resume_abort(engine, tmp_path):
    assert replication.estimate(engine, {"snapshot": "tank/a@s1"}) == 3000
    assert replication.resume_tokens(engine) == [{"dataset": "backup/a", "token": "1-abc-def"}]
    job = wait(replication.resume(engine, "backup/a", "1-abc-def"))
    assert job.state == "done"
    replication.abort_receive(engine, "backup/a")
    log = (tmp_path / "log").read_text().splitlines()
    assert "send -vP -t 1-abc-def" in log and "receive -s -u -x mountpoint backup/a" in log
    assert log[-1] == "receive -A backup/a"
    with pytest.raises(ReplicationProblem):
        replication.resume(engine, "backup/a", "bad token")


def test_failed_job_keeps_the_error(engine, tmp_path, monkeypatch):
    (tmp_path / "bin" / "zfs").write_text("#!/bin/sh\necho 'cannot receive: destination exists' >&2\nexit 1\n")
    job = wait(replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/a"}))
    assert job.state == "failed" and "destination exists" in job.error


def test_a_finished_job_is_complete_even_without_progress_lines(engine, tmp_path):
    (tmp_path / "bin" / "zfs").write_text(
        "#!/bin/sh\ncase \"$1\" in send) printf 'size\\t500\\n' >&2 ;; receive) cat >/dev/null ;; esac\n")
    job = wait(replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/z"}))
    assert job.state == "done" and job.to_dict()["done"] == 500


def test_a_running_receive_is_not_offered_for_resuming(engine, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    job = replication.replicate(engine, {"snapshot": "tank/a@s1", "target": "backup/a"})
    try:
        time.sleep(0.3)
        assert replication.resume_tokens(engine) == []
    finally:
        job.cancel()
        wait(job)
    assert replication.resume_tokens(engine) == [{"dataset": "backup/a", "token": "1-abc-def"}]
