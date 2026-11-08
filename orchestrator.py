"""
FreedomNAS Orchestrator — keeps the modules checked while the NAS runs.

`python -m freedomnas serve`, started by supervisor.sh next to the WebUI,
registers every module and calls check_due() about once a second. Each
module is checked every CHECK_INTERVAL seconds: start() (it reads the
configuration again) and health(), together within CHECK_TIMEOUT seconds.

  healthy  — health() said yes
  problem  — health() said no: a real state of the system (a degraded pool,
             a broken disk). Shown, checked again, never blocks the module;
             it turns healthy by itself
  retrying — the module raised or did not answer in time: it is stopped and
             started again after RETRY_DELAYS (10, 20, 30, 60 s)
  blocked  — the last of those tries failed too: left alone until someone
             asks for a restart (a file named after it in <run>/module-restart)
  failed   — the module could not be loaded at all

The state is written to <run>/module-health.json, which the WebUI reads.
A module blocked there stays blocked when serve starts again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Orchestrator")

CHECK_INTERVAL = 30
CHECK_TIMEOUT = 5
RETRY_DELAYS = (10, 20, 30, 60)
HEALTH_FILE = "module-health.json"
RESTART_DIR = "module-restart"


class ModuleState(str, Enum):
    STARTING = "starting"
    HEALTHY = "healthy"
    PROBLEM = "problem"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class ModuleStatus:
    id: str
    name: str
    state: ModuleState = ModuleState.STARTING
    detail: str = ""
    attempt: int = 0
    checked_at: Optional[str] = None
    next_check: float = field(default=0.0, repr=False)

    def as_dict(self) -> dict:
        entry = {"id": self.id, "name": self.name, "state": self.state.value,
                 "detail": self.detail, "checked_at": self.checked_at}
        if self.state == ModuleState.RETRYING:
            entry["attempt"] = self.attempt
            entry["attempts"] = len(RETRY_DELAYS)
        return entry


class FreedomNASModule(ABC):
    """
    Abstract base for all FreedomNAS pluggable modules.

    Subclasses implement start(), stop(), and health_check().
    """

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        self._engine = engine
        self._config = config

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique module identifier (e.g. 'storage', 'network', 'shares')."""

    @abstractmethod
    def start(self) -> None:
        """Start the module. Must be idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the module cleanly. Must be idempotent."""

    @abstractmethod
    def health_check(self) -> bool:
        """
        Return True if the module is operating normally, False when what it
        manages is not (a degraded pool): that is a state to show, not a
        failure of the module. Raising, or not answering within
        CHECK_TIMEOUT, is a failure.
        """

    def health(self) -> Tuple[bool, str]:
        """
        Whether the module is operating normally, and a line saying why, for
        the dashboard. Modules that can say more than yes or no override it.
        """
        healthy = self.health_check()
        return healthy, "" if healthy else "health check failed"

    def reconcile(self, desired: dict, mode: str = "declarative") -> list:
        """
        Bring what the module manages to `desired`, its section of the
        configuration (`modules.<id>`), at boot and when a configuration is
        restored; in "additive" mode nothing is removed. Returns the
        reconciler's ActionResults. Modules that manage nothing of their own
        keep this default.
        """
        return []

    def prepare(self, desired: dict) -> list:
        """
        Install what `desired` needs on a system that is not running yet: an
        installer restoring a configuration into the system it installs, in
        its chroot. Packages only — no service is started and no pool is
        touched; the first boot's reconcile does the rest. Returns
        ActionResults.
        """
        return []

    def links(self) -> list:
        """
        The web pages this module serves that every account of the NAS may
        open, for the WebUI's account page: dicts with "name", and "title",
        "path", "url" (an address of its own) or "scheme" and "port" (on the
        address the WebUI was reached at). The WebUI joins the entries of all
        modules that have the same name, so one module may give the title and
        another the address. Called after start().
        """
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Orchestrator:
    """
    Checks the registered modules, retries and blocks the ones that fail,
    and writes what it found for the WebUI.
    """

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager,
                 run_dir: Optional[Path] = None, clock: Callable[[], float] = time.monotonic):
        self._engine = engine
        self._config = config
        self._run_dir = Path(run_dir) if run_dir else None
        self._clock = clock
        self._modules: Dict[str, FreedomNASModule] = {}
        self._status: Dict[str, ModuleStatus] = {}
        self._order: List[str] = []
        self._busy: Dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def register(self, module: FreedomNASModule, module_id: Optional[str] = None,
                 name: Optional[str] = None) -> None:
        module_id = module_id or module.name
        with self._lock:
            self._modules[module_id] = module
            self._status[module_id] = ModuleStatus(id=module_id, name=name or module_id)
            if module_id not in self._order:
                self._order.append(module_id)

    def mark_failed(self, module_id: str, reason: str) -> None:
        """A module that could not be loaded: listed, never checked."""
        with self._lock:
            self._status[module_id] = ModuleStatus(id=module_id, name=module_id,
                                                   state=ModuleState.FAILED, detail=reason)
            if module_id not in self._order:
                self._order.append(module_id)

    def list_modules(self) -> List[str]:
        return list(self._order)

    def get_status(self) -> Dict[str, ModuleStatus]:
        with self._lock:
            return dict(self._status)

    def keep_blocked(self) -> None:
        """Modules the previous serve left blocked stay blocked."""
        try:
            data = json.loads(self._file(HEALTH_FILE).read_text())
        except (OSError, ValueError, TypeError):
            return
        for entry in data.get("modules") or []:
            status = self._status.get(entry.get("id"))
            if status and entry.get("state") == ModuleState.BLOCKED.value and status.id in self._modules:
                status.state = ModuleState.BLOCKED
                status.detail = entry.get("detail") or ""
                status.checked_at = entry.get("checked_at")

    def start_all(self) -> None:
        for module_id in self._order:
            if module_id in self._modules and self._status[module_id].state != ModuleState.BLOCKED:
                self._status[module_id].next_check = self._clock()

    def stop_all(self) -> None:
        for module_id in reversed(self._order):
            module = self._modules.get(module_id)
            if module is None:
                continue
            try:
                module.stop()
            except Exception as exc:
                logger.warning("Module %s raised during stop: %s", module_id, exc)

    def take_restart_requests(self) -> List[str]:
        """Restart requests left by the WebUI: the module is checked again at once."""
        taken: List[str] = []
        try:
            requests = sorted(self._file(RESTART_DIR).iterdir())
        except OSError:
            return taken
        for request in requests:
            try:
                request.unlink()
            except OSError:
                continue
            status = self._status.get(request.name)
            if status is None or request.name not in self._modules:
                continue
            status.state = ModuleState.STARTING
            status.attempt = 0
            status.detail = ""
            status.next_check = self._clock()
            taken.append(request.name)
            logger.info("Module %s restarted on request", request.name)
        return taken

    def check_due(self) -> List[str]:
        """Check every module whose time has come. Returns their ids."""
        now = self._clock()
        due = [m for m in self._order if m in self._modules
               and self._status[m].state not in (ModuleState.BLOCKED, ModuleState.FAILED)
               and self._status[m].next_check <= now]
        for module_id in due:
            self._check(module_id)
        return due

    def _check(self, module_id: str) -> None:
        status = self._status[module_id]
        retry = status.state == ModuleState.RETRYING
        outcome = self._call(module_id, self._modules[module_id], retry)
        status.checked_at = _now()
        if outcome[0] == "ok":
            healthy, detail = outcome[1]
            status.state = ModuleState.HEALTHY if healthy else ModuleState.PROBLEM
            status.detail = detail
            status.attempt = 0
            status.next_check = self._clock() + CHECK_INTERVAL
            return
        status.detail = outcome[1]
        if retry and status.attempt >= len(RETRY_DELAYS):
            status.state = ModuleState.BLOCKED
            logger.error("Module %s blocked after %d tries: %s", module_id, status.attempt, status.detail)
            return
        status.attempt = status.attempt + 1 if retry else 1
        status.state = ModuleState.RETRYING
        delay = RETRY_DELAYS[status.attempt - 1]
        status.next_check = self._clock() + delay
        logger.warning("Module %s failed (%s): try %d of %d in %d s", module_id, status.detail,
                       status.attempt, len(RETRY_DELAYS), delay)

    def _call(self, module_id: str, module: FreedomNASModule, restart: bool) -> tuple:
        previous = self._busy.get(module_id)
        if previous is not None and previous.is_alive():
            return ("failed", f"still busy with a check started more than {CHECK_TIMEOUT} s ago")
        result: list = []

        def run():
            try:
                if restart:
                    module.stop()
                module.start()
                result.append(("ok", module.health()))
            except Exception as exc:
                result.append(("failed", f"{type(exc).__name__}: {exc}"))

        worker = threading.Thread(target=run, name=f"fnas-check-{module_id}", daemon=True)
        self._busy[module_id] = worker
        worker.start()
        worker.join(CHECK_TIMEOUT)
        if worker.is_alive() or not result:
            return ("failed", f"did not answer within {CHECK_TIMEOUT} s")
        return result[0]

    def _file(self, name: str) -> Path:
        if self._run_dir is None:
            raise OSError("no run directory")
        return self._run_dir / name

    def write(self) -> None:
        """The state for the WebUI, replaced in one step."""
        if self._run_dir is None:
            return
        data = {"pid": os.getpid(), "updated_at": _now(), "interval": CHECK_INTERVAL,
                "modules": [self._status[m].as_dict() for m in self._order]}
        target = self._file(HEALTH_FILE)
        temporary = target.with_name(f".{HEALTH_FILE}.{os.getpid()}")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, target)

    def __repr__(self) -> str:
        counts: Dict[str, int] = {}
        for s in self._status.values():
            counts[s.state.value] = counts.get(s.state.value, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in counts.items())
        return f"Orchestrator(modules={len(self._modules)}, {summary})"
