"""
FreedomNAS Reconciliation Engine

Applies configuration from config.yaml to bring a freshly installed OS to the
desired state. Used by the (parked) installer and by freedomnas-image-builder's
baked-in firstboot.sh (via `python -m freedomnas reconcile`, see __main__.py)
— never by the WebUI directly.

All OS operations delegate to FreedomNASEngine; no subprocess calls here.
"""

from __future__ import annotations

import logging
import yaml
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

try:
    from freedomnas.engine import (
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    from freedomnas.config import ConfigurationManager
except ImportError:
    from engine import (  # type: ignore[no-redef]
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    from config import ConfigurationManager  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Reconciler")


class ReconcileMode(str, Enum):
    HYBRID = "hybrid"
    DECLARATIVE = "declarative"
    DISASTER_RECOVERY = "disaster_recovery"


@dataclass
class Change:
    path: str        # dot-notation: "system.hostname", "users.admin", "smb_shares.media"
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
_FEATURE_SERVICE_MAP: Dict[str, tuple] = {
    "samba-cifs": ("samba", "smb_service"),
    "nfs":        ("nfs",   "nfs_service"),
}


class ReconciliationEngine:
    """
    Computes and applies the diff between desired and current system state.

    Three modes (spec §6.2):
    - hybrid           : diff desired vs current, apply minimal changes;
                         users absent from desired are never deleted (spec §6.5)
    - declarative      : treat current as empty, apply full desired state;
                         users and shares missing from desired are deleted
    - disaster_recovery: restore from config snapshot in fixed order
                         (hostname → timezone → pools → users → services → shares)

    Constraints (spec §6.5):
    - Idempotent and safe to re-run
    - Never assumes OS behaviour — all commands come from the engine profile
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

    def compute_diff(
        self,
        desired: dict,
        current: dict,
        mode: ReconcileMode = ReconcileMode.HYBRID,
    ) -> List[Change]:
        """
        Compare desired vs current state and return the list of Changes needed.

        Both arguments must follow the same schema as config.yaml.
        In HYBRID mode, users absent from desired are never emitted as "remove".
        In DECLARATIVE mode (current == {}), only additions are generated.
        """
        changes: List[Change] = []

        # System — hostname
        d_host = desired.get("system", {}).get("hostname")
        c_host = current.get("system", {}).get("hostname")
        if d_host and d_host != c_host:
            changes.append(Change(
                path="system.hostname",
                old_value=c_host,
                new_value=d_host,
                change_type="modify" if c_host else "add",
            ))

        # System — timezone
        d_tz = desired.get("system", {}).get("timezone")
        c_tz = current.get("system", {}).get("timezone")
        if d_tz and d_tz != c_tz:
            changes.append(Change(
                path="system.timezone",
                old_value=c_tz,
                new_value=d_tz,
                change_type="modify" if c_tz else "add",
            ))

        # Users — add or modify; in hybrid mode, never delete (spec §6.5)
        d_users: Dict[str, dict] = {u["username"]: u for u in desired.get("users", [])}
        c_users: Dict[str, dict] = {u["username"]: u for u in current.get("users", [])}
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
        if mode == ReconcileMode.DECLARATIVE:
            for username in c_users:
                if username not in d_users:
                    changes.append(Change(
                        path=f"users.{username}",
                        old_value=c_users[username],
                        new_value=None,
                        change_type="remove",
                    ))

        # ZFS pools — import pools not present; modification not supported
        d_pools: Dict[str, dict] = {p["name"]: p for p in desired.get("storage", {}).get("pools", [])}
        c_pools: Dict[str, dict] = {p["name"]: p for p in current.get("storage", {}).get("pools", [])}
        for pool_name, pool_cfg in d_pools.items():
            if pool_name not in c_pools:
                changes.append(Change(
                    path=f"storage.pools.{pool_name}",
                    old_value=None,
                    new_value=pool_cfg,
                    change_type="add",
                ))

        # SMB shares
        d_smb: Dict[str, dict] = {s["name"]: s for s in desired.get("smb_shares", [])}
        c_smb: Dict[str, dict] = {s["name"]: s for s in current.get("smb_shares", [])}
        for name, share_cfg in d_smb.items():
            if name not in c_smb:
                changes.append(Change(
                    path=f"smb_shares.{name}",
                    old_value=None,
                    new_value=share_cfg,
                    change_type="add",
                ))
            elif share_cfg != c_smb[name]:
                changes.append(Change(
                    path=f"smb_shares.{name}",
                    old_value=c_smb[name],
                    new_value=share_cfg,
                    change_type="modify",
                ))
        for name, share_cfg in c_smb.items():
            if name not in d_smb:
                changes.append(Change(
                    path=f"smb_shares.{name}",
                    old_value=share_cfg,
                    new_value=None,
                    change_type="remove",
                ))

        # NFS shares
        d_nfs: Dict[str, dict] = {s["name"]: s for s in desired.get("nfs_shares", [])}
        c_nfs: Dict[str, dict] = {s["name"]: s for s in current.get("nfs_shares", [])}
        for name, share_cfg in d_nfs.items():
            if name not in c_nfs:
                changes.append(Change(
                    path=f"nfs_shares.{name}",
                    old_value=None,
                    new_value=share_cfg,
                    change_type="add",
                ))
            elif share_cfg != c_nfs[name]:
                changes.append(Change(
                    path=f"nfs_shares.{name}",
                    old_value=c_nfs[name],
                    new_value=share_cfg,
                    change_type="modify",
                ))
        for name, share_cfg in c_nfs.items():
            if name not in d_nfs:
                changes.append(Change(
                    path=f"nfs_shares.{name}",
                    old_value=share_cfg,
                    new_value=None,
                    change_type="remove",
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
            desired_path: Path to a desired-state YAML file.
                          If None, uses self.config (the shadow-copy on disk).
            mode: "hybrid" | "declarative" | "disaster_recovery"

        Returns:
            List of ActionResult — one entry per engine call attempted.
        """
        rec_mode = ReconcileMode(mode)

        if rec_mode == ReconcileMode.DISASTER_RECOVERY:
            state = self.config.to_dict()
            return self._apply_disaster_recovery(state)

        desired = self.load_desired(desired_path) if desired_path else self.config.to_dict()
        current = {} if rec_mode == ReconcileMode.DECLARATIVE else self.config.to_dict()

        changes = self.compute_diff(desired, current, mode=rec_mode)
        return self._apply_changes(changes)

    # ------------------------------------------------------------------
    # Internal — disaster recovery
    # ------------------------------------------------------------------

    def _apply_disaster_recovery(self, state: dict) -> List[ActionResult]:
        """
        Full restore from config snapshot. Fixed order (spec §6.4):
        1. Hostname  2. Timezone  3. Pools  4. Users  5. Services  6. Shares
        """
        results: List[ActionResult] = []

        hostname = state.get("system", {}).get("hostname")
        if hostname:
            results.append(self._do_hostname(hostname))

        timezone = state.get("system", {}).get("timezone")
        if timezone:
            results.append(self._do_timezone(timezone))

        for pool in state.get("storage", {}).get("pools", []):
            results.append(self._do_pool_import(pool))

        for user in state.get("users", []):
            results.append(self._do_user_create(user))

        for feature in state.get("installed_features", []):
            if feature.get("enabled"):
                results.extend(self._do_service_enable(feature["id"]))

        for share in state.get("smb_shares", []):
            results.append(self._do_smb_share_create(share))

        for share in state.get("nfs_shares", []):
            results.append(self._do_nfs_share_create(share))

        return results

    # ------------------------------------------------------------------
    # Internal — change application
    # ------------------------------------------------------------------

    def _apply_changes(self, changes: List[Change]) -> List[ActionResult]:
        results: List[ActionResult] = []
        for change in changes:
            path = change.path

            if path == "system.hostname":
                results.append(self._do_hostname(change.new_value))

            elif path == "system.timezone":
                results.append(self._do_timezone(change.new_value))

            elif path.startswith("users."):
                if change.change_type == "add":
                    results.append(self._do_user_create(change.new_value))
                elif change.change_type == "modify":
                    results.extend(self._do_user_modify(change.new_value, change.old_value))
                elif change.change_type == "remove":
                    username = path.split(".", 1)[1]
                    results.append(self._do_user_delete(username))

            elif path.startswith("storage.pools."):
                if change.change_type == "add":
                    results.append(self._do_pool_import(change.new_value))

            elif path.startswith("smb_shares."):
                if change.change_type in ("add", "modify"):
                    results.append(self._do_smb_share_create(change.new_value))
                elif change.change_type == "remove":
                    share_name = path.split(".", 1)[1]
                    results.append(self._do_smb_share_delete(share_name))

            elif path.startswith("nfs_shares."):
                if change.change_type in ("add", "modify"):
                    results.append(self._do_nfs_share_create(change.new_value))
                elif change.change_type == "remove":
                    share_name = path.split(".", 1)[1]
                    results.append(self._do_nfs_share_delete(share_name))

        return results

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: system
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

    def _do_timezone(self, timezone: str) -> ActionResult:
        params = {"timezone": timezone}
        try:
            self.engine.execute("system", "timezone_set", timezone=timezone)
            logger.info("Timezone set to: %s", timezone)
            return ActionResult(action="system.timezone_set", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to set timezone %s: %s", timezone, exc)
            return ActionResult(action="system.timezone_set", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: ZFS
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: users
    # ------------------------------------------------------------------

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

    def _do_user_modify(self, user: dict, old_user: dict) -> List[ActionResult]:
        """Reconcile group membership changes for an existing user."""
        username = user.get("username", "")
        desired_groups: Set[str] = set(user.get("groups", []))
        current_groups: Set[str] = set(old_user.get("groups", []))
        results: List[ActionResult] = []

        for group in desired_groups - current_groups:
            params = {"username": username, "group": group}
            try:
                self.engine.execute("users", "add_to_group", username=username, group=group)
                logger.info("Added %s to group %s", username, group)
                results.append(ActionResult(action="users.add_to_group", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to add %s to group %s: %s", username, group, exc)
                results.append(ActionResult(action="users.add_to_group", params=params, target_state="failed", error=str(exc)))

        for group in current_groups - desired_groups:
            params = {"username": username, "group": group}
            try:
                self.engine.execute("users", "remove_from_group", username=username, group=group)
                logger.info("Removed %s from group %s", username, group)
                results.append(ActionResult(action="users.remove_from_group", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to remove %s from group %s: %s", username, group, exc)
                results.append(ActionResult(action="users.remove_from_group", params=params, target_state="failed", error=str(exc)))

        if not results:
            results.append(ActionResult(
                action="users.modify",
                params={"username": username},
                target_state="skipped",
                error="no group membership changes detected",
            ))

        return results

    def _do_user_delete(self, username: str) -> ActionResult:
        """Delete a user. Called only in declarative mode."""
        params = {"username": username}
        try:
            self.engine.execute("users", "delete", username=username)
            logger.info("Deleted user: %s", username)
            return ActionResult(action="users.delete", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to delete user %s: %s", username, exc)
            return ActionResult(action="users.delete", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: SMB shares
    # ------------------------------------------------------------------

    def _do_smb_share_create(self, share: dict) -> ActionResult:
        name = share.get("name", "")
        path = share.get("path", "")
        comment = share.get("comment", "")
        acl = share.get("acl", "Everyone:F")
        params = {"share_name": name, "path": path}
        try:
            self.engine.execute(
                "samba", "create_share",
                share_name=name,
                path=path,
                comment=comment,
                acl=acl,
            )
            logger.info("Created SMB share: %s → %s", name, path)
            return ActionResult(action="samba.create_share", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to create SMB share %s: %s", name, exc)
            return ActionResult(action="samba.create_share", params=params, target_state="failed", error=str(exc))

    def _do_smb_share_delete(self, share_name: str) -> ActionResult:
        params = {"share_name": share_name}
        try:
            self.engine.execute("samba", "delete_share", share_name=share_name)
            logger.info("Deleted SMB share: %s", share_name)
            return ActionResult(action="samba.delete_share", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to delete SMB share %s: %s", share_name, exc)
            return ActionResult(action="samba.delete_share", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: NFS shares
    # ------------------------------------------------------------------

    def _do_nfs_share_create(self, share: dict) -> ActionResult:
        name = share.get("name", "")
        path = share.get("path", "")
        clients = share.get("clients", "*")
        options = share.get("options", "ro,sync,no_subtree_check")
        params = {"name": name, "path": path, "clients": clients}
        try:
            self.engine.execute(
                "nfs", "export_add",
                path=path,
                clients=clients,
                options=options,
            )
            self.engine.execute("nfs", "exportfs_reload")
            logger.info("Created NFS share: %s → %s", name, path)
            return ActionResult(action="nfs.export_add", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to create NFS share %s: %s", name, exc)
            return ActionResult(action="nfs.export_add", params=params, target_state="failed", error=str(exc))

    def _do_nfs_share_delete(self, share_name: str) -> ActionResult:
        params = {"name": share_name}
        try:
            self.engine.execute("nfs", "export_remove", name=share_name)
            self.engine.execute("nfs", "exportfs_reload")
            logger.info("Deleted NFS share: %s", share_name)
            return ActionResult(action="nfs.export_remove", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to delete NFS share %s: %s", share_name, exc)
            return ActionResult(action="nfs.export_remove", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: services
    # ------------------------------------------------------------------

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
