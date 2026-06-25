"""
FreedomNAS Reconciliation Engine

Applies configuration from config.yaml to bring a freshly installed OS to the
desired state. Used exclusively by the installer — not the WebUI.

All OS operations delegate to FreedomNASEngine; no subprocess calls here.
"""

from __future__ import annotations

import logging
import yaml
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from freedomnas.engine import (
    FreedomNASEngine,
    CommandExecutionError,
    CommandTranslationError,
)
from freedomnas.config import ConfigurationManager

logger = logging.getLogger("FreedomNAS.Reconciler")


class ReconcileMode(str, Enum):
    HYBRID = "hybrid"
    DECLARATIVE = "declarative"
    DISASTER_RECOVERY = "disaster_recovery"


@dataclass
class Change:
    path: str        # dot-notation path, e.g. "system.hostname", "users.admin"
    old_value: Any
    new_value: Any
    change_type: str  # "add" | "remove" | "modify"


@dataclass
class ActionResult:
    action: str
    params: dict
    target_state: str  # "applied" | "failed" | "skipped"
    error: Optional[str] = None


# Maps abstract feature IDs to (profile_category, service_name_key).
# The actual service name is read from the OS profile at runtime — never hardcoded.
_FEATURE_SERVICE_MAP: dict[str, tuple[str, str]] = {
    "samba-cifs": ("samba", "smb_service"),
    "nfs":        ("nfs",   "nfs_service"),
}


class ReconciliationEngine:
    """
    Computes and applies the diff between desired and current system state.

    Three modes (spec §6.2):
    - hybrid           : diff desired vs current, apply minimal changes
    - declarative      : treat current as empty, apply full desired state
    - disaster_recovery: restore from config snapshot in fixed order
                         (hostname → pools → users → services)

    Constraints (spec §6.5):
    - Idempotent and safe to re-run
    - Never assumes OS behaviour — all commands come from the engine profile
    - In hybrid mode, users absent from desired are NOT deleted
    - Pool modification is not supported (use the storage API instead)
    """

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        self.engine = engine
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_desired(self, path: str) -> dict:
        """Load a desired-state YAML file and return it as a dict."""
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}

    def compute_diff(self, desired: dict, current: dict) -> List[Change]:
        """
        Compare desired vs current state and return the list of Changes needed.

        Both arguments are dicts in the same schema as config.yaml.
        """
        changes: List[Change] = []

        # Hostname
        d_host = desired.get("system", {}).get("hostname")
        c_host = current.get("system", {}).get("hostname")
        if d_host and d_host != c_host:
            changes.append(Change(
                path="system.hostname",
                old_value=c_host,
                new_value=d_host,
                change_type="modify" if c_host else "add",
            ))

        # Users — add or modify; never delete in hybrid (spec §6.5)
        d_users = {u["username"]: u for u in desired.get("users", [])}
        c_users = {u["username"]: u for u in current.get("users", [])}
        for username, user_cfg in d_users.items():
            if username not in c_users:
                changes.append(Change(
                    path=f"users.{username}",
                    old_value=None,
                    new_value=user_cfg,
                    change_type="add",
                ))
            elif user_cfg != c_users[username]:
                changes.append(Change(
                    path=f"users.{username}",
                    old_value=c_users[username],
                    new_value=user_cfg,
                    change_type="modify",
                ))

        # ZFS pools — import pools not yet present (modification not supported)
        d_pools = {p["name"]: p for p in desired.get("storage", {}).get("pools", [])}
        c_pools = {p["name"]: p for p in current.get("storage", {}).get("pools", [])}
        for pool_name, pool_cfg in d_pools.items():
            if pool_name not in c_pools:
                changes.append(Change(
                    path=f"storage.pools.{pool_name}",
                    old_value=None,
                    new_value=pool_cfg,
                    change_type="add",
                ))

        return changes

    def reconcile(
        self,
        desired_path: Optional[str] = None,
        mode: str = "hybrid",
    ) -> List[ActionResult]:
        """
        Bring the system to the desired state.

        Args:
            desired_path: Path to a desired-state YAML.
                          If None, uses self.config (the current shadow-copy).
            mode: "hybrid" | "declarative" | "disaster_recovery"

        Returns:
            List of ActionResult — one per engine call attempted.
        """
        rec_mode = ReconcileMode(mode)

        if rec_mode == ReconcileMode.DISASTER_RECOVERY:
            state = self.config.to_dict()
            return self._apply_disaster_recovery(state)

        desired = self.load_desired(desired_path) if desired_path else self.config.to_dict()
        current = {} if rec_mode == ReconcileMode.DECLARATIVE else self.config.to_dict()

        changes = self.compute_diff(desired, current)
        return self._apply_changes(changes)

    # ------------------------------------------------------------------
    # Internal — disaster recovery
    # ------------------------------------------------------------------

    def _apply_disaster_recovery(self, state: dict) -> List[ActionResult]:
        """
        Full restore from config snapshot. Fixed order (spec §6.4):
        1. Hostname  2. Pools  3. Users  4. Services
        """
        results: List[ActionResult] = []

        hostname = state.get("system", {}).get("hostname")
        if hostname:
            results.append(self._do_hostname(hostname))

        for pool in state.get("storage", {}).get("pools", []):
            results.append(self._do_pool_import(pool))

        for user in state.get("users", []):
            results.append(self._do_user_create(user))

        for feature in state.get("installed_features", []):
            if feature.get("enabled"):
                results.extend(self._do_service_enable(feature["id"]))

        return results

    # ------------------------------------------------------------------
    # Internal — change application
    # ------------------------------------------------------------------

    def _apply_changes(self, changes: List[Change]) -> List[ActionResult]:
        results: List[ActionResult] = []
        for change in changes:
            if change.path == "system.hostname":
                results.append(self._do_hostname(change.new_value))
            elif change.path.startswith("users."):
                results.append(self._do_user_create(change.new_value))
            elif change.path.startswith("storage.pools."):
                results.append(self._do_pool_import(change.new_value))
        return results

    # ------------------------------------------------------------------
    # Internal — atomic engine calls
    # ------------------------------------------------------------------

    def _do_hostname(self, hostname: str) -> ActionResult:
        params = {"hostname": hostname}
        try:
            self.engine.execute("system", "hostname_set", hostname=hostname)
            logger.info("Hostname set to: %s", hostname)
            return ActionResult(action="system.hostname_set", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to set hostname %s: %s", hostname, exc)
            return ActionResult(action="system.hostname_set", params=params, target_state="failed", error=str(exc))

    def _do_pool_import(self, pool: dict) -> ActionResult:
        pool_name = pool.get("name", "")
        params = {"pool_name": pool_name}
        try:
            self.engine.execute("zfs", "pool_import", pool_name=pool_name)
            logger.info("Imported ZFS pool: %s", pool_name)
            return ActionResult(action="zfs.pool_import", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to import pool %s: %s", pool_name, exc)
            return ActionResult(action="zfs.pool_import", params=params, target_state="failed", error=str(exc))

    def _do_user_create(self, user: dict) -> ActionResult:
        username = user.get("username", "")
        groups = ",".join(user.get("groups", []))
        params = {"username": username, "groups": groups}
        try:
            self.engine.execute(
                "users", "create",
                username=username,
                groups=groups,
                uid=user.get("uid", ""),
                gid=user.get("gid", ""),
                shell=user.get("shell", "/bin/bash"),
            )
            logger.info("Created user: %s", username)
            return ActionResult(action="users.create", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to create user %s: %s", username, exc)
            return ActionResult(action="users.create", params=params, target_state="failed", error=str(exc))

    def _do_service_enable(self, feature_id: str) -> List[ActionResult]:
        """Enable and start the service associated with a feature ID."""
        if feature_id not in _FEATURE_SERVICE_MAP:
            return [ActionResult(
                action=f"services.enable.{feature_id}",
                params={"feature_id": feature_id},
                target_state="skipped",
                error=f"No service mapping for feature '{feature_id}'",
            )]

        category, key = _FEATURE_SERVICE_MAP[feature_id]
        try:
            service_name = self.engine.get_profile_value(category, key)
        except CommandTranslationError as exc:
            return [ActionResult(
                action=f"services.enable.{feature_id}",
                params={"feature_id": feature_id},
                target_state="skipped",
                error=str(exc),
            )]

        results: List[ActionResult] = []
        for action in ("enable", "start"):
            params = {"service_name": service_name}
            try:
                self.engine.execute("services", action, service_name=service_name)
                logger.info("Service %s: %s", action, service_name)
                results.append(ActionResult(action=f"services.{action}", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to %s service %s: %s", action, service_name, exc)
                results.append(ActionResult(action=f"services.{action}", params=params, target_state="failed", error=str(exc)))

        return results
