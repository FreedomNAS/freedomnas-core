"""
FreedomNAS Orchestrator — Module lifecycle management with heartbeat monitoring.

Manages the lifecycle of FreedomNAS Python modules (storage, network, shares,
etc.) through a unified interface. A background heartbeat thread periodically
checks each module's health and applies a circuit breaker when a module fails
repeatedly (spec §9.2: stop after 5 consecutive failures).

Usage:
    engine = get_engine()
    config = ConfigurationManager()
    orch = Orchestrator(engine, config)
    orch.register(MyModule(engine, config))
    orch.start_all()
    ...
    orch.stop_all()
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Orchestrator")

# Circuit breaker threshold (spec §9.2)
MAX_FAILURES = 5
# Seconds between heartbeat checks
HEARTBEAT_INTERVAL = 30


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

class ModuleState(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    FAILED   = "failed"
    DISABLED = "disabled"


@dataclass
class ModuleStatus:
    name: str
    state: ModuleState
    last_heartbeat: Optional[datetime] = None
    failure_count: int = 0
    error: Optional[str] = None

    def is_healthy(self) -> bool:
        return self.state == ModuleState.RUNNING

    def is_circuit_open(self) -> bool:
        """Return True when the circuit breaker has tripped (too many failures)."""
        return self.failure_count >= MAX_FAILURES


# ---------------------------------------------------------------------------
# Module base class
# ---------------------------------------------------------------------------

class FreedomNASModule(ABC):
    """
    Abstract base for all FreedomNAS pluggable modules.

    Subclasses implement start(), stop(), and health_check().
    The orchestrator calls health_check() periodically via the heartbeat thread.
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
        Return True if the module is operating normally.
        Called by the heartbeat thread every HEARTBEAT_INTERVAL seconds.
        Must not raise — catch all exceptions internally and return False.
        """


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Manages the lifecycle of registered FreedomNAS modules.

    Responsibilities:
    - Start and stop modules in registration order (reverse for stop)
    - Run a background heartbeat thread that calls health_check() on each module
    - Apply circuit breaker: after MAX_FAILURES consecutive health-check failures,
      mark the module DISABLED and stop retrying (spec §9.2)
    - Provide status queries for the WebUI and the supervisor wrapper
    """

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        self._engine = engine
        self._config = config
        self._modules: Dict[str, FreedomNASModule] = {}
        self._status: Dict[str, ModuleStatus] = {}
        self._order: List[str] = []          # registration order
        self._lock = threading.RLock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, module: FreedomNASModule) -> None:
        """
        Register a module with the orchestrator.

        Must be called before start_all(). Registering the same name twice
        replaces the previous module.
        """
        with self._lock:
            name = module.name
            self._modules[name] = module
            self._status[name] = ModuleStatus(name=name, state=ModuleState.STOPPED)
            if name not in self._order:
                self._order.append(name)
            logger.debug("Module registered: %s", name)

    # ------------------------------------------------------------------
    # Lifecycle — bulk operations
    # ------------------------------------------------------------------

    def start_all(self) -> None:
        """Start all registered modules in registration order, then begin heartbeat."""
        logger.info("Starting all modules (%d registered)", len(self._modules))
        for name in self._order:
            self.start_module(name)
        self._start_heartbeat()

    def stop_all(self) -> None:
        """Stop heartbeat, then stop all modules in reverse registration order."""
        self._stop_heartbeat()
        for name in reversed(self._order):
            self.stop_module(name)
        logger.info("All modules stopped")

    # ------------------------------------------------------------------
    # Lifecycle — individual modules
    # ------------------------------------------------------------------

    def start_module(self, name: str) -> None:
        """Start a single module by name."""
        with self._lock:
            if name not in self._modules:
                raise KeyError(f"Module not registered: {name}")
            status = self._status[name]
            if status.state == ModuleState.DISABLED:
                logger.warning("Module %s is disabled (circuit open); skipping start", name)
                return
            if status.state == ModuleState.RUNNING:
                logger.debug("Module %s already running", name)
                return
            status.state = ModuleState.STARTING

        module = self._modules[name]
        try:
            module.start()
            with self._lock:
                self._status[name].state = ModuleState.RUNNING
                self._status[name].last_heartbeat = datetime.now()
                self._status[name].error = None
            logger.info("Module started: %s", name)
        except Exception as exc:
            with self._lock:
                self._status[name].state = ModuleState.FAILED
                self._status[name].error = str(exc)
            logger.error("Module %s failed to start: %s", name, exc)

    def stop_module(self, name: str) -> None:
        """Stop a single module by name."""
        with self._lock:
            if name not in self._modules:
                raise KeyError(f"Module not registered: {name}")
            if self._status[name].state == ModuleState.STOPPED:
                return

        module = self._modules[name]
        try:
            module.stop()
        except Exception as exc:
            logger.warning("Module %s raised during stop: %s", name, exc)
        finally:
            with self._lock:
                self._status[name].state = ModuleState.STOPPED
            logger.info("Module stopped: %s", name)

    def restart_module(self, name: str) -> None:
        """Stop then start a module, resetting its failure count and re-enabling after a circuit trip."""
        with self._lock:
            if name not in self._modules:
                raise KeyError(f"Module not registered: {name}")
            self._status[name].failure_count = 0
            self._status[name].state = ModuleState.STOPPED

        self.stop_module(name)
        self.start_module(name)

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, ModuleStatus]:
        """Return a snapshot of all module statuses."""
        with self._lock:
            return dict(self._status)

    def get_module_status(self, name: str) -> ModuleStatus:
        """Return the status of a single module."""
        with self._lock:
            if name not in self._status:
                raise KeyError(f"Module not registered: {name}")
            return self._status[name]

    def list_modules(self) -> List[str]:
        """Return module names in registration order."""
        return list(self._order)

    # ------------------------------------------------------------------
    # Heartbeat — internal
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="fnas-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.debug("Heartbeat thread started (interval=%ds)", HEARTBEAT_INTERVAL)

    def _stop_heartbeat(self) -> None:
        self._stop_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL + 5)
        logger.debug("Heartbeat thread stopped")

    def _heartbeat_loop(self) -> None:
        """Background thread: check all running modules every HEARTBEAT_INTERVAL seconds."""
        while not self._stop_event.wait(timeout=HEARTBEAT_INTERVAL):
            with self._lock:
                names = list(self._order)
            for name in names:
                self._check_module(name)

    def _check_module(self, name: str) -> None:
        """
        Run health_check() on one module and apply the circuit breaker.

        On success: reset failure_count, update last_heartbeat.
        On failure: increment failure_count.
        At MAX_FAILURES: mark DISABLED — circuit open, no more restarts.
        """
        with self._lock:
            status = self._status.get(name)
            if status is None or status.state not in (ModuleState.RUNNING, ModuleState.FAILED):
                return
            if status.is_circuit_open():
                return

        module = self._modules[name]
        try:
            healthy = module.health_check()
        except Exception as exc:
            healthy = False
            logger.warning("Module %s health_check raised: %s", name, exc)

        with self._lock:
            status = self._status[name]
            if healthy:
                status.state = ModuleState.RUNNING
                status.last_heartbeat = datetime.now()
                status.failure_count = 0
                status.error = None
            else:
                status.failure_count += 1
                status.state = ModuleState.FAILED
                logger.warning(
                    "Module %s unhealthy (failure %d/%d)",
                    name, status.failure_count, MAX_FAILURES,
                )
                if status.is_circuit_open():
                    status.state = ModuleState.DISABLED
                    logger.error(
                        "Module %s circuit breaker OPEN — "
                        "disabled after %d consecutive failures",
                        name, MAX_FAILURES,
                    )

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        counts = {}
        for s in self._status.values():
            counts[s.state] = counts.get(s.state, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in counts.items())
        return f"Orchestrator(modules={len(self._modules)}, {summary})"
