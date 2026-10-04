"""
Local ZFS replication: zfs send | zfs receive between pools of this system,
or into and out of a stream file.

A send can take hours, so each one runs as a job in a thread. The pipeline is
the profile's (`zfs.replicate` and friends): it writes the shell's PID and the
`zfs send -vP` progress lines into files under the runtime directory, which is
how a job reports how far it got and how it is cancelled — through the
profile's `zfs.replication_cancel`, never by signalling processes from here.
"""

import itertools
import logging
import os
import posixpath
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    from freedomnas import datasets, pool_import, snapshots
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    import datasets  # type: ignore
    import pool_import  # type: ignore
    import snapshots  # type: ignore
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

logger = logging.getLogger("FreedomNAS.Replication")

RUN_DIR = Path("/run/freedomnas/replication")
JOB_TIMEOUT = 7 * 24 * 3600

SEND_FLAGS = {"replicate": "-R", "raw": "-w", "compressed": "-c", "large_blocks": "-L",
              "embed": "-e", "props": "-p", "holds": "-h", "skip_missing": "-s"}
_TOKEN = re.compile(r"^[A-Za-z0-9-]+$")


class ReplicationProblem(Exception):
    pass


def _bool(params: Dict, key: str) -> bool:
    value = params.get(key, False)
    if not isinstance(value, bool):
        raise ReplicationProblem(f"{key} must be true or false")
    return value


def send_flags(params: Dict) -> str:
    return " ".join(flag for key, flag in SEND_FLAGS.items() if _bool(params, key))


def incremental(params: Dict, snapshot: str) -> str:
    base = str(params.get("from") or "").strip()
    if not base:
        return ""
    dataset = snapshot.split("@")[0]
    if "#" in base:
        ds, _, mark = base.partition("#")
        try:
            snapshots.split_name(f"{ds}@{mark}")
        except ValueError:
            raise ReplicationProblem(f"{base!r} is not a bookmark name (dataset#name)") from None
        if ds != dataset:
            raise ReplicationProblem("The incremental base belongs to the same dataset")
        return f"-i {base}"
    try:
        ds, _ = snapshots.split_name(base)
    except ValueError as exc:
        raise ReplicationProblem(str(exc)) from None
    if ds != dataset:
        raise ReplicationProblem("The incremental base belongs to the same dataset")
    return f"{'-I' if _bool(params, 'intermediates') else '-i'} {base}"


def receive_flags(params: Dict, resumable_default: bool = True) -> str:
    flags = []
    if _bool(params, "force"):
        flags.append("-F")
    if params.get("no_mount", True) is not False:
        flags.append("-u")
    if params.get("resumable", resumable_default) is not False:
        flags.append("-s")
    mode = params.get("name_mode") or "exact"
    if mode == "d":
        flags.append("-d")
    elif mode == "e":
        flags.append("-e")
    elif mode != "exact":
        raise ReplicationProblem("name_mode is exact, d or e")
    if params.get("exclude_mountpoint", True) is not False:
        flags += ["-x", "mountpoint"]
    return " ".join(flags)


def check_target(engine, target: str) -> str:
    target = (target or "").strip()
    parts = target.split("/")
    if not target or not all(re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", p) for p in parts):
        raise ReplicationProblem("The target is a dataset name such as backup/documents")
    reason = datasets.protected_reason(target, datasets.root_pool(engine))
    if reason:
        raise ReplicationProblem(f"{target} cannot be written here: {reason}")
    if not datasets.exists(engine, parts[0]):
        raise ReplicationProblem(f"There is no pool called {parts[0]}")
    return target


def check_file(path: str, must_exist: bool) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or any(c in path for c in "\n\r\0") or ".." in path.split("/"):
        raise ReplicationProblem("The stream file is an absolute path")
    path = posixpath.normpath(path)
    if pool_import.is_system_path(posixpath.dirname(path)):
        raise ReplicationProblem(f"{posixpath.dirname(path)} is a system directory: use a dataset")
    if not os.path.isdir(posixpath.dirname(path)):
        raise ReplicationProblem(f"{posixpath.dirname(path)} does not exist")
    if must_exist and not os.path.isfile(path):
        raise ReplicationProblem(f"{path} is not a file")
    if not must_exist and os.path.lexists(path):
        raise ReplicationProblem(f"{path} already exists")
    return path


class Progress:
    """The `zfs send -vP` lines: `size\\t<total>`, then `<time>\\t<bytes>\\t<snapshot>`."""

    def __init__(self):
        self.total = 0
        self.done_before = 0
        self.current = None
        self.current_bytes = 0

    def feed(self, text: str) -> "Progress":
        for line in text.splitlines():
            parts = line.split("\t")
            if parts[0] == "size" and len(parts) > 1 and parts[1].isdigit():
                self.total = int(parts[1])
            elif len(parts) >= 3 and ":" in parts[0] and parts[1].isdigit():
                if parts[2] != self.current:
                    self.done_before += self.current_bytes
                    self.current = parts[2]
                    self.current_bytes = 0
                self.current_bytes = int(parts[1])
        return self

    @property
    def done(self) -> int:
        return self.done_before + self.current_bytes


def parse_estimate(text: str) -> Optional[int]:
    return Progress().feed(text).total or None


_ids = itertools.count(1)
_jobs: Dict[int, "Job"] = {}
_lock = threading.Lock()


class Job:
    def __init__(self, engine, kind: str, action: str, describe: Dict,
                 with_progress: bool = True, **kwargs):
        self.id = next(_ids)
        self.engine = engine
        self.kind = kind
        self.action = action
        self.describe = describe
        self.kwargs = kwargs
        self.state = "running"
        self.error = ""
        self.started = time.time()
        self.finished: Optional[float] = None
        self.cancelled = False
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.pid_file = RUN_DIR / f"{self.id}.pid"
        self.progress_file = RUN_DIR / f"{self.id}.progress"
        for f in (self.pid_file, self.progress_file):
            if f.exists():
                f.unlink()
        if with_progress:
            self.kwargs["progress_file"] = str(self.progress_file)

    def start(self) -> "Job":
        with _lock:
            _jobs[self.id] = self
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self) -> None:
        try:
            self.engine.execute("zfs", self.action, timeout=JOB_TIMEOUT,
                                pid_file=str(self.pid_file), **self.kwargs)
            self.state = "cancelled" if self.cancelled else "done"
        except (CommandExecutionError, CommandTranslationError) as exc:
            self.state = "cancelled" if self.cancelled else "failed"
            self.error = "" if self.cancelled else str(exc)
            logger.error("Replication job %s %s: %s", self.id, self.state, exc)
        self.finished = time.time()

    def progress(self) -> Progress:
        try:
            return Progress().feed(self.progress_file.read_text(errors="replace"))
        except OSError:
            return Progress()

    def cancel(self) -> bool:
        if self.state != "running":
            return False
        try:
            pid = self.pid_file.read_text().strip()
        except OSError:
            return False
        if not pid.isdigit():
            return False
        self.cancelled = True
        try:
            self.engine.execute("zfs", "replication_cancel", pid=pid, check=False)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Cancelling job %s: %s", self.id, exc)
            return False
        return True

    def to_dict(self) -> Dict:
        prog = self.progress()
        done = prog.total if self.state == "done" and prog.total else prog.done
        return {"id": self.id, "kind": self.kind, "state": self.state, "error": self.error,
                "started": self.started, "finished": self.finished,
                "total": prog.total, "done": done, **self.describe}


def jobs() -> List[Dict]:
    with _lock:
        return [j.to_dict() for j in sorted(_jobs.values(), key=lambda j: -j.id)]


def get(job_id: int) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def _busy(target: str) -> None:
    with _lock:
        for job in _jobs.values():
            if job.state == "running" and job.describe.get("target") == target:
                raise ReplicationProblem(f"A transfer into {target} is already running")


def _source(params: Dict) -> str:
    try:
        snapshots.split_name(str(params.get("snapshot") or ""))
    except ValueError as exc:
        raise ReplicationProblem(str(exc)) from None
    return params["snapshot"]


def estimate(engine, params: Dict) -> Optional[int]:
    snap = _source(params)
    stdout, stderr = engine.execute("zfs", "send_estimate", timeout=300,
                                    send_flags=send_flags(params),
                                    incremental=incremental(params, snap), snapshot=snap)
    return parse_estimate(stdout + "\n" + stderr)


def replicate(engine, params: Dict) -> Job:
    snap = _source(params)
    target = check_target(engine, params.get("target"))
    _busy(target)
    return Job(engine, "replicate", "replicate",
               {"source": snap, "target": target, "from": params.get("from") or ""},
               send_flags=send_flags(params), incremental=incremental(params, snap),
               snapshot=snap, receive_flags=receive_flags(params), target=target).start()


def send_to_file(engine, params: Dict) -> Job:
    snap = _source(params)
    path = check_file(params.get("file"), must_exist=False)
    return Job(engine, "send_to_file", "send_to_file",
               {"source": snap, "target": path, "from": params.get("from") or ""},
               send_flags=send_flags(params), incremental=incremental(params, snap),
               snapshot=snap, file=path).start()


def receive_from_file(engine, params: Dict) -> Job:
    path = check_file(params.get("file"), must_exist=True)
    target = check_target(engine, params.get("target"))
    _busy(target)
    return Job(engine, "receive_from_file", "receive_from_file",
               {"source": path, "target": target},
               with_progress=False,
               receive_flags=receive_flags(params, resumable_default=False), target=target,
               file=path).start()


def resume(engine, target: str, token: str, params: Optional[Dict] = None) -> Job:
    target = check_target(engine, target)
    if not _TOKEN.match(token or "") or len(token) > 16384:
        raise ReplicationProblem("The resume token is not valid")
    _busy(target)
    flags = receive_flags({**(params or {}), "resumable": False})
    return Job(engine, "resume", "replicate_resume", {"source": "resume token", "target": target},
               token=token, receive_flags=flags, target=target).start()


def resume_tokens(engine) -> List[Dict]:
    """Interrupted receives: a token on a dataset no running job is writing into."""
    stdout, _ = engine.execute("zfs", "resume_tokens")
    with _lock:
        busy = {j.describe.get("target") for j in _jobs.values() if j.state == "running"}
    found = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] not in ("-", "") and parts[0] not in busy:
            found.append({"dataset": parts[0], "token": parts[1]})
    return found


def abort_receive(engine, target: str) -> None:
    target = check_target(engine, target)
    engine.execute("zfs", "receive_abort", target=target)
