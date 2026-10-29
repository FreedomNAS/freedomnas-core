"""
FreedomNAS Reconciliation Engine

Brings the running system to the state config.yaml describes. Used by
firstboot.sh at every boot (via `python -m freedomnas reconcile`, see
__main__.py) and by the WebUI when a configuration is loaded.

The current state is read from the system itself (`observe()`), never from
config.yaml: the configuration says what should be, only the system can say
what is.

All OS operations delegate to FreedomNASEngine; no subprocess calls here.
"""

from __future__ import annotations

import json
from pathlib import Path
import logging
import time
import yaml
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set

try:
    from freedomnas.engine import (
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    from freedomnas.config import ConfigurationManager, known_admin_groups, normalize_user
    from freedomnas.datasets import nfs_export_options, owner_group
    from freedomnas.package_manager import PackageManager, admin_groups_present, module_packages
    from freedomnas import groupshares, netconfig, netnames, restoring, shadow, usershares, wireguard
    from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
except ImportError:
    from engine import (  # type: ignore[no-redef]
        FreedomNASEngine,
        CommandExecutionError,
        CommandTranslationError,
    )
    from config import ConfigurationManager, known_admin_groups, normalize_user  # type: ignore[no-redef]
    from datasets import nfs_export_options, owner_group  # type: ignore[no-redef]
    from package_manager import PackageManager, admin_groups_present, module_packages  # type: ignore[no-redef]
    import netconfig  # type: ignore[no-redef]
    import netnames  # type: ignore[no-redef]
    import shadow  # type: ignore[no-redef]
    import wireguard  # type: ignore[no-redef]
    import usershares  # type: ignore[no-redef]
    import groupshares  # type: ignore[no-redef]
    import restoring  # type: ignore[no-redef]
    from module_loader import ModuleLoader, ModuleLoaderException  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Reconciler")


class ReconcileMode(str, Enum):
    DECLARATIVE = "declarative"
    ADDITIVE = "additive"


# firstboot.sh is copied into every installed system, and the copies made
# before the modes were merged still ask for disaster_recovery.
_MODE_ALIASES = {"disaster_recovery": ReconcileMode.DECLARATIVE}


def parse_mode(mode: str) -> ReconcileMode:
    return _MODE_ALIASES.get(mode) or ReconcileMode(mode)


@dataclass
class Change:
    path: str        # dot-notation: "system.hostname", "users.alice", "smb_shares.media"
    old_value: Any
    new_value: Any
    change_type: str  # "add" | "modify" | "remove" | "ensure"


@dataclass
class ActionResult:
    action: str
    params: dict
    target_state: str  # "applied" | "failed" | "skipped"
    error: Optional[str] = None


# Maps abstract feature IDs to (profile_category, service_name_key).
# The actual service name is read from the OS profile at runtime — never hardcoded.
SMBD_WAIT_ATTEMPTS = 10
SMBD_WAIT_SECONDS = 3

_FEATURE_SERVICE_MAP: Dict[str, tuple] = {
    "samba-cifs": ("samba", "smb_service"),
    "nfs":        ("nfs",   "nfs_service"),
}


def _normalize(state: dict) -> dict:
    """
    config.yaml keeps shares under shares.smb/nfs; the plan reads them flat.
    Administrators are `admin: true`, whatever group an older file named.
    """
    shares = state.get("shares") or {}
    admin_groups = known_admin_groups()
    return {
        **state,
        "users": [normalize_user(u, admin_groups) for u in state.get("users") or [] if isinstance(u, dict)],
        "smb_shares": state.get("smb_shares", shares.get("smb", [])),
        "nfs_shares": state.get("nfs_shares", shares.get("nfs", [])),
    }


def _export_line(share: dict) -> tuple:
    return (
        share.get("path", ""),
        share.get("allowed_hosts") or share.get("clients", ""),
        share.get("options", ""),
    )


def _in_after_order(pairs: list) -> list:
    """
    Modules in their given order, except that one whose manifest lists
    modules under "after" comes once those that are present have run: a
    module that uses another's work (the containers writing sites for the
    proxy it installs) finds it done. A loop keeps the given order.
    """
    present = {(manifest.get("id") or module.name) for manifest, module in pairs}
    pending, ordered, done = list(pairs), [], set()
    while pending:
        ready = next((i for i, (manifest, _) in enumerate(pending)
                      if not (set(manifest.get("after") or []) & present) - done), 0)
        manifest, module = pending.pop(ready)
        ordered.append((manifest, module))
        done.add((manifest.get("id") or module.name))
    return ordered


class ReconciliationEngine:
    """
    Computes and applies what separates the running system from the
    configuration.

    Two modes:
    - declarative: make the system match the configuration exactly — add,
                   change and remove. What firstboot.sh runs at every boot,
                   and what the WebUI runs when a configuration is loaded.
    - additive   : add and change only, never remove; for applying a
                   partial desired-state file from the command line.

    Order: hostname → timezone → network interfaces → pools → users → services → SSH → SMB shares
    → NFS exports → removal of users → modules (each module's own
    `modules.<id>` section, through its reconcile()). Pools come before the shares on them,
    users before the shares that name them, and users go last so no share
    still refers to them.

    What is never removed: pools and datasets (pools are only imported),
    root, system accounts, and administrators — members of the edition's
    sudo group, who can only be removed from the WebUI, so neither a boot
    nor a loaded file can leave a system nobody can log in to. A removed
    account keeps its home directory. A configuration that names no
    administrator removes nothing at all: it is more likely damaged than
    meant.
    """

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        self.engine = engine
        self.config = config
        # owner -> the share paths given to them, from the last compute_diff:
        # where a user's ID can be read back when the configuration has none.
        self._owned_paths: Dict[str, List[str]] = {}
        self.progress = None
        self._module_steps: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_desired(self, path: str) -> dict:
        """Load a desired-state YAML file and return it as a dict."""
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}

    def reconcile(
        self,
        desired_path: Optional[str] = None,
        mode: str = "declarative",
        dry_run: bool = False,
    ) -> List[ActionResult]:
        """
        Bring the system to the desired state.

        Args:
            desired_path: A desired-state YAML file; None means config.yaml.
            mode: "declarative" or "additive" ("disaster_recovery" is
                  accepted as declarative).
            dry_run: Plan only; every step comes back as "planned".
        """
        changes = self.plan(desired_path, mode)
        if dry_run:
            return [
                ActionResult(action=c.path, params={"change": c.change_type}, target_state="planned")
                for c in changes
            ]
        self._plan_progress(changes)
        results = self._apply_changes(changes)
        desired = self.load_desired(desired_path) if desired_path else self.config.to_dict()
        installing = len(changes)
        self._step_start(installing)
        installed = self._install_modules(desired.get("modules") or {})
        if any(r.action == "modules.install" and r.target_state == "applied" for r in installed):
            try:
                self.engine.reload_profile()
            except Exception as exc:
                logger.error("Profiles not loaded again after installing modules: %s", exc)
            self._plan_new_modules()
        self._step_done(installing, installed)
        results.extend(installed)
        results.extend(self._reconcile_modules(desired.get("modules") or {}, parse_mode(mode)))
        return results

    def _plan_progress(self, changes: List[Change]) -> None:
        if not self.progress:
            return
        try:
            manifests = [m for m in ModuleLoader(self.engine, self.config).discover() if m.get("id")]
        except (ModuleLoaderException, OSError):
            manifests = []
        modules = [m["id"] for m, _ in _in_after_order([(m, None) for m in manifests])]
        first = len(changes) + 1
        self._module_steps = {module_id: first + i for i, module_id in enumerate(modules)}
        self.progress.plan([restoring.step_label(c.path) for c in changes] + ["Modules to install"]
                           + [f"Module {module_id}" for module_id in modules])

    def _plan_new_modules(self) -> None:
        if not self.progress:
            return
        try:
            manifests = [m for m in ModuleLoader(self.engine, self.config).discover() if m.get("id")]
        except (ModuleLoaderException, OSError):
            return
        for module_id in [m["id"] for m, _ in _in_after_order([(m, None) for m in manifests])]:
            if module_id not in self._module_steps:
                self._module_steps[module_id] = self.progress.add(f"Module {module_id}")

    def _step_start(self, index: int) -> None:
        if self.progress:
            self.progress.start(index)

    def _step_done(self, index: int, results: List[ActionResult]) -> None:
        if self.progress:
            self.progress.done(index, [f"{r.action}: {r.error}" for r in results
                                       if r.target_state == "failed"])

    def plan(self, desired_path: Optional[str] = None, mode: str = "declarative") -> List[Change]:
        desired = self.load_desired(desired_path) if desired_path else self.config.to_dict()
        return self.compute_diff(desired, self.observe(), mode=parse_mode(mode))

    # ------------------------------------------------------------------
    # Observing the running system
    # ------------------------------------------------------------------

    def observe(self) -> dict:
        """
        The running system, in config.yaml's shape. A part that cannot be
        read is None rather than empty: nothing is removed on the strength of
        a list that could not be taken.
        """
        return {
            "system": {
                "hostname": self._read("network", "hostname_get"),
                "timezone": self._read("system", "timezone_get"),
            },
            "network": {"links": self._observe_links()},
            "storage": {"pools": self._observe_pools()},
            "users": self._observe_users(),
            "smb_shares": self._observe_smb_shares(),
            "nfs_shares": self._observe_nfs_exports(),
        }

    def _read(self, category: str, action: str, **kwargs) -> Optional[str]:
        try:
            stdout, _ = self.engine.execute(category, action, **kwargs)
        except (CommandExecutionError, CommandTranslationError):
            return None
        return stdout.strip()

    def _observe_links(self) -> Optional[List[str]]:
        """Interface names: `ip -j addr show` JSON on Linux, bare names on FreeBSD."""
        out = self._read("network", "list_interfaces")
        if out is None:
            return None
        try:
            listed = json.loads(out)
        except ValueError:
            return out.split()
        if not isinstance(listed, list):
            return None
        return [i["ifname"] for i in listed if isinstance(i, dict) and i.get("ifname")]

    def _observe_pools(self) -> Optional[List[dict]]:
        out = self._read("zfs", "pool_list")
        if out is None:
            return None
        return [{"name": line.split()[0]} for line in out.splitlines() if line.split()]

    def _observe_users(self) -> Optional[List[dict]]:
        out = self._read("users", "list")
        if out is None:
            return None
        users = []
        for line in out.splitlines():
            name = line.split(":", 1)[0].strip()
            if not name or name == "root":
                continue
            groups = self._read("users", "list_groups", username=name)
            users.append({"username": name, "groups": (groups or "").split()})
        return users

    def _observe_smb_shares(self) -> Optional[List[dict]]:
        out = self._read("samba", "list_shares")
        if out is None:
            return None
        return [{"name": line.strip()} for line in out.splitlines() if line.strip()]

    def _observe_nfs_exports(self) -> Optional[List[dict]]:
        """
        /etc/exports has no share names: an export is a line, identified by
        its path. Reading a file is not a system command, so it is read here.
        """
        try:
            path = self.engine.get_profile_value("nfs", "config_file")
            with open(path) as f:
                lines = f.read().splitlines()
        except (CommandTranslationError, OSError):
            return None
        exports = []
        for line in lines:
            line = line.strip()
            # FreeBSD's `V4:` line is the NFSv4 root, not an export.
            if not line or line.startswith("#") or line.startswith("V4:"):
                continue
            fields = line.split()
            if len(fields) == 2 and "(" in fields[1]:
                clients, _, options = fields[1].partition("(")
                exports.append({"path": fields[0], "allowed_hosts": clients,
                                "options": options.rstrip(")")})
            else:
                # FreeBSD's `path -opts -network ...`, or a line written by
                # hand: known by its path only.
                exports.append({"path": fields[0], "opaque": True})
        return exports

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def compute_diff(
        self,
        desired: dict,
        current: dict,
        mode: ReconcileMode = ReconcileMode.DECLARATIVE,
    ) -> List[Change]:
        """
        The ordered list of Changes that turns `current` (from observe())
        into `desired` (config.yaml's shape). ADDITIVE drops every removal.
        """
        desired = _normalize(desired)
        changes: List[Change] = []

        for key in ("hostname", "timezone"):
            want = desired.get("system", {}).get(key)
            have = current.get("system", {}).get(key)
            if want and want != have:
                changes.append(Change(f"system.{key}", have, want, "modify" if have else "add"))

        if current.get("network", {}).get("links"):
            wanted = [i for i in (desired.get("network") or {}).get("interfaces") or []
                      if i.get("logical_name") and (i.get("config") or i.get("type") in netconfig.VIRTUAL_TYPES
                                                    or i.get("type") == "wireguard")]
            changes.append(Change("network.interfaces", None, wanted, "ensure"))

        # A pool already imported is not necessarily in use: firstboot imports
        # every pool with -N and no cachefile to look for .freedomnas, so a
        # restored data pool is there with nothing mounted and would be left
        # out again at the next boot. "ensure" mounts it and records it.
        imported = {p["name"] for p in current.get("storage", {}).get("pools") or []}
        for pool in desired.get("storage", {}).get("pools", []):
            if pool.get("name"):
                kind = "ensure" if pool["name"] in imported else "add"
                changes.append(Change(f"storage.pools.{pool['name']}", None, pool, kind))

        for share in desired.get("smb_shares", []):
            if share.get("group") and share.get("path"):
                changes.append(Change(f"share_groups.{share['name']}", None, share, "ensure"))

        self._owned_paths = {}
        for share in desired.get("smb_shares", []) + desired.get("nfs_shares", []):
            owner, path = share.get("owner"), share.get("path")
            if owner and path and path not in self._owned_paths.setdefault(owner, []):
                self._owned_paths[owner].append(path)

        observed_users = current.get("users")
        existing = {u["username"]: u for u in observed_users or []}
        wanted_users = {u["username"]: u for u in desired.get("users", []) if u.get("username")}
        managed = self._managed_groups(desired)
        sudo_group = self._profile_value("users", "sudo_group")
        admin_extra = ([sudo_group] if sudo_group else []) + [
            g for g in self._admin_groups(wanted_users.values()) if g != sudo_group]
        managed |= set(admin_extra)
        for name, user in list(wanted_users.items()):
            if admin_extra and self._is_admin(user):
                groups = list(user.get("groups", []))
                wanted_users[name] = {**user, "groups": groups + [g for g in admin_extra if g not in groups]}
        for name, user in wanted_users.items():
            if observed_users is not None and name not in existing:
                changes.append(Change(f"users.{name}", None, user, "add"))
            elif observed_users is None:
                changes.append(Change(f"users.{name}", None, user, "ensure"))
            else:
                have = set(existing[name].get("groups", [])) & managed
                want = set(user.get("groups", [])) & managed
                if sudo_group in have - want:
                    logger.info("Keeping %s in %s: administrators lose their rights from the WebUI only",
                                name, sudo_group)
                    want.add(sudo_group)
                    user = {**user, "groups": list(user.get("groups", [])) + [sudo_group]}
                if have != want:
                    changes.append(Change(
                        f"users.{name}",
                        {"username": name, "groups": sorted(have)},
                        user,
                        "modify",
                    ))

        for feature in desired.get("installed_features", []):
            if feature.get("enabled"):
                changes.append(Change(f"features.{feature['id']}", None, feature, "ensure"))

        ssh = desired.get("system", {}).get("ssh_enabled")
        if ssh is not None:
            enabled = bool(ssh) and not self.config.setup_pending()
            changes.append(Change("system.ssh", None, enabled, "ensure"))

        if self._profile_section("power_button"):
            changes.append(Change("system.power_button", None, True, "ensure"))

        for name, enabled in ((desired.get("services") or {}).get("boot") or {}).items():
            changes.append(Change(f"services.boot.{name}", None,
                                  {"name": name, "enabled": bool(enabled)}, "ensure"))

        removals_allowed = mode == ReconcileMode.DECLARATIVE and self._names_an_admin(desired)
        if mode == ReconcileMode.DECLARATIVE and not removals_allowed:
            logger.warning("The configuration names no administrator: nothing will be removed")

        wanted_smb = {s["name"]: s for s in desired.get("smb_shares", []) if s.get("name")}
        if removals_allowed:
            for share in current.get("smb_shares") or []:
                if share["name"] not in wanted_smb:
                    changes.append(Change(f"smb_shares.{share['name']}", share, None, "remove"))
        for name, share in wanted_smb.items():
            changes.append(Change(f"smb_shares.{name}", None, share, "ensure"))

        observed_exports = current.get("nfs_shares")
        have_lines = {_export_line(e) for e in observed_exports or [] if not e.get("opaque")}
        opaque_paths = {e["path"] for e in observed_exports or [] if e.get("opaque")}
        have_paths = {line[0] for line in have_lines} | opaque_paths
        want = {self._wanted_export_line(s): s for s in desired.get("nfs_shares", []) if s.get("path")}
        want_paths = {line[0] for line in want}
        if removals_allowed:
            for export in observed_exports or []:
                stale = (export["path"] not in want_paths if export.get("opaque")
                         else _export_line(export) not in want)
                if stale:
                    changes.append(Change(f"nfs_shares.{export['path']}", export, None, "remove"))
        for line, share in want.items():
            if line in have_lines or line[0] in opaque_paths:
                continue
            if line[0] in have_paths and not removals_allowed:
                logger.warning("NFS export %s differs from the configuration; left alone", line[0])
                continue
            changes.append(Change(f"nfs_shares.{share.get('name', line[0])}", None, share, "add"))

        if removals_allowed and observed_users is not None:
            sudo_group = self._profile_value("users", "sudo_group")
            for name, user in existing.items():
                if name in wanted_users:
                    continue
                if sudo_group and sudo_group in user.get("groups", []):
                    logger.info("Keeping administrator %s: administrators are removed from the WebUI", name)
                    continue
                changes.append(Change(f"users.{name}", user, None, "remove"))

        if mode == ReconcileMode.ADDITIVE:
            changes = [c for c in changes if c.change_type != "remove"]
        return changes

    def _profile_value(self, category: str, key: str) -> str:
        try:
            return self.engine.get_profile_value(category, key) or ""
        except CommandTranslationError:
            return ""

    def _managed_groups(self, desired: dict) -> Set[str]:
        """
        The groups FreedomNAS assigns. Membership of any other group — a
        user's own primary group, `users`, anything an administrator set up
        by hand — is none of the reconciler's business.
        """
        groups = {g for u in desired.get("users", []) for g in u.get("groups", [])}
        for key in ("sudo_group", "share_group"):
            value = self._profile_value("users", key)
            if value:
                groups.add(value)
        return groups

    def _is_admin(self, user: dict) -> bool:
        return bool(normalize_user(user).get("admin"))

    def _admin_groups(self, users) -> List[str]:
        """
        The groups administrators join here (admin_groups_present): a
        configuration restored from another edition, or written before a
        feature was installed, does not name them.
        """
        if not any(self._is_admin(u) for u in users):
            return []
        try:
            return admin_groups_present(self.engine)
        except Exception as exc:
            logger.warning("Could not work out the administrators' groups: %s", exc)
            return []

    def _names_an_admin(self, desired: dict) -> bool:
        return any(self._is_admin(u) for u in desired.get("users", []))

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------

    def _apply_changes(self, changes: List[Change]) -> List[ActionResult]:
        results: List[ActionResult] = []
        usershares_ready = False
        groups_checked: Set[str] = set()
        self._renumbered = False
        for index, change in enumerate(changes):
            path, kind = change.path, change.change_type
            before = len(results)
            self._step_start(index)

            if path == "system.hostname":
                results.append(self._do_hostname(change.new_value))

            elif path == "system.timezone":
                results.append(self._do_timezone(change.new_value))

            elif path == "system.ssh":
                results.extend(self._do_ssh(change.new_value))

            elif path == "system.power_button":
                results.extend(self._do_power_button())

            elif path == "network.interfaces":
                results.extend(self._do_network(change.new_value))

            elif path.startswith("storage.pools."):
                if kind == "ensure":
                    results.extend(self._do_pool_adopt(change.new_value))
                else:
                    results.append(self._do_pool_import(change.new_value))
                if results[-1].target_state == "applied":
                    results.extend(self._do_pool_acltype(change.new_value))

            elif path.startswith("share_groups."):
                results.extend(self._do_share_group(change.new_value))

            elif path.startswith("users."):
                if kind in ("add", "ensure"):
                    results.append(self._do_user_create(change.new_value))
                elif kind == "modify":
                    results.extend(self._do_user_modify(change.new_value, change.old_value))
                elif kind == "remove":
                    results.append(self._do_user_delete(path.split(".", 1)[1]))

            elif path.startswith("features."):
                results.extend(self._do_service_enable(change.new_value["id"]))

            elif path.startswith("services.boot."):
                results.extend(self._do_service_boot(change.new_value["name"], change.new_value["enabled"]))

            if (path.startswith(("smb_shares.", "nfs_shares.")) and kind != "remove"
                    and change.new_value.get("owner") and change.new_value.get("path")
                    and change.new_value["path"] not in groups_checked):
                groups_checked.add(change.new_value["path"])
                results.extend(self._do_owner_group(change.new_value))

            if path.startswith("smb_shares."):
                if kind == "remove":
                    results.append(self._do_smb_share_delete(path.split(".", 1)[1]))
                else:
                    if not usershares_ready:
                        results.append(self._do_usershares_prepare())
                        usershares_ready = True
                    results.append(self._do_smb_share_create(change.new_value))

            elif path.startswith("nfs_shares."):
                if kind == "remove":
                    results.append(self._do_nfs_export_remove(change.old_value["path"]))
                else:
                    results.append(self._do_nfs_share_create(change.new_value))

            self._step_done(index, results[before:])

        if self._renumbered:
            for change in changes:
                if change.path.startswith("share_groups."):
                    results.extend(self._do_share_group(change.new_value))
        return results

    def _install_modules(self, sections: dict) -> List[ActionResult]:
        """
        A module the configuration has a section for but this system lacks
        (a fresh install restored from its pool, another edition) has its
        package installed from the edition's repositories, as the Plugins
        page would, so that it is loaded and reconciled right after.
        """
        wanted = {module_id for module_id, section in sections.items() if isinstance(section, dict) and section}
        try:
            present = {m.get("id") for m in ModuleLoader(self.engine, self.config).discover()}
        except (ModuleLoaderException, OSError) as exc:
            logger.error("Modules not checked: %s", exc)
            return []
        missing = sorted(wanted - present)
        if not missing:
            return []
        try:
            self.engine.execute("packages", "update_cache", timeout=3600, check=False)
            offered = module_packages(self.engine)
        except (CommandExecutionError, CommandTranslationError) as exc:
            return [ActionResult(action="modules.install", params={"modules": missing}, target_state="failed",
                                 error=str(exc))]
        results: List[ActionResult] = []
        for module_id in missing:
            params = {"module": module_id}
            package = offered.get(module_id)
            if not package:
                results.append(ActionResult(action="modules.install", params=params, target_state="skipped",
                                            error=f"the repositories of this edition have no module {module_id}"))
                continue
            params["package"] = package
            try:
                self.engine.execute("packages", "install", package_name=package, timeout=7200)
                logger.info("Module %s installed (%s)", module_id, package)
                results.append(ActionResult(action="modules.install", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Could not install module %s: %s", module_id, exc)
                results.append(ActionResult(action="modules.install", params=params, target_state="failed",
                                            error=str(exc)))
        return results

    def _reconcile_modules(self, sections: dict, mode: ReconcileMode) -> List[ActionResult]:
        try:
            pairs = ModuleLoader(self.engine, self.config).instances()
        except ModuleLoaderException as exc:
            logger.error("Modules not reconciled: %s", exc)
            return [ActionResult(action="modules", params={}, target_state="failed", error=str(exc))]
        results: List[ActionResult] = []
        for manifest, module in _in_after_order(pairs):
            module_id = manifest.get("id", module.name)
            step = self._module_steps.get(module_id)
            before = len(results)
            if step is not None:
                self._step_start(step)
            try:
                results.extend(module.reconcile(sections.get(module_id) or {}, mode.value) or [])
            except Exception as exc:
                logger.error("Module %s failed to reconcile: %s", module_id, exc)
                results.append(ActionResult(
                    action=f"modules.{module_id}", params={}, target_state="failed", error=str(exc)))
            if step is not None:
                self._step_done(step, results[before:])
        return results

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: system
    # ------------------------------------------------------------------

    def _do_network(self, interfaces: List[dict]) -> List[ActionResult]:
        """
        Physical cards get their FreedomNAS names first (netnames), then each
        configured interface its configuration, found by MAC address when the
        entry records one: a restored entry may still carry the old name.
        """
        results: List[ActionResult] = []
        renamed: Dict[str, str] = {}
        reapply = self._network_reapply()
        for name in (reapply or {}).get("forget") or []:
            self.engine.execute("network", "connection_forget", check=False, interface=name)
            logger.info("Interface %s forgotten: the network of this machine has none", name)
            results.append(ActionResult(action="network.forget", params={"interface": name},
                                        target_state="applied"))
        try:
            done, renamed = netnames.apply(self.engine, self.config)
            for line in done:
                logger.info("Network names: %s", line)
                results.append(ActionResult(action="network.names", params={"done": line},
                                            target_state="applied"))
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to name the network cards: %s", exc)
            results.append(ActionResult(action="network.names", params={}, target_state="failed",
                                        error=str(exc)))
        by_mac = {}
        try:
            stdout, _ = self.engine.execute("network", "list_physical")
            by_mac = {c["mac"]: c["name"] for c in netnames.parse_physical(stdout)}
        except (CommandExecutionError, CommandTranslationError):
            pass
        links = self._observe_links() or []
        order = {"bond": 1, "vlan": 2, "bridge": 3, "wireguard": 4}
        for entry in sorted(interfaces, key=lambda e: order.get(e.get("type"), 0)):
            if entry.get("type") == "wireguard":
                results.append(self._do_wireguard(entry))
                continue
            if entry.get("type") in netconfig.VIRTUAL_TYPES:
                results.append(self._do_virtual(entry, links, bool(reapply)))
                continue
            if entry.get("master"):
                continue
            name = (by_mac.get(str(entry.get("mac_address") or "").lower())
                    or renamed.get(entry["logical_name"], entry["logical_name"]))
            if name not in links:
                results.append(ActionResult(action="network.interface", params={"interface": name},
                                            target_state="skipped", error="not on this system"))
                continue
            results.append(self._do_interface(name, entry["config"], name in renamed.values(),
                                              bool(reapply)))
        if reapply is not None:
            self._network_reapply_done()
        return results

    def _network_marker(self):
        path = getattr(self.config, "config_path", None)
        return path.parent / shadow.NETWORK_MARKER if path else None

    def _network_reapply(self) -> Optional[dict]:
        """What the boot left when it took the network of this machine from a data pool."""
        marker = self._network_marker()
        if not marker or not marker.is_file():
            return None
        try:
            with open(marker) as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not read %s: %s", marker, exc)
            return {}

    def _network_reapply_done(self) -> None:
        try:
            self._network_marker().unlink()
        except OSError as exc:
            logger.warning("Could not remove the network marker: %s", exc)

    def _do_wireguard(self, entry: dict) -> ActionResult:
        params = {"interface": entry["logical_name"], "type": "wireguard"}
        try:
            stdout, _ = self.engine.execute("network", "wg_exists", check=False,
                                            interface=entry["logical_name"])
            if stdout.strip() == "yes":
                return ActionResult(action="network.wireguard", params=params, target_state="skipped",
                                    error="already configured")
            wireguard.apply(self.engine, entry)
        except (CommandExecutionError, CommandTranslationError, wireguard.WireGuardProblem, KeyError) as exc:
            logger.error("Failed to set up WireGuard: %s", exc)
            return ActionResult(action="network.wireguard", params=params, target_state="failed",
                                error=str(exc))
        return ActionResult(action="network.wireguard", params=params, target_state="applied")

    def _do_virtual(self, entry: dict, links: List[str], reapply: bool = False) -> ActionResult:
        name = entry["logical_name"]
        params = {"interface": name, "type": entry["type"]}
        missing = [p for p in (entry.get("ports") or []) + ([entry["parent"]] if entry.get("parent") else [])
                   if p not in links]
        if missing:
            return ActionResult(action="network.virtual", params=params, target_state="skipped",
                                error=f"{', '.join(missing)} not on this system")
        try:
            if netconfig.is_configured(self.engine, name):
                if not reapply:
                    return ActionResult(action="network.virtual", params=params, target_state="skipped",
                                        error="already configured")
                netconfig.run(self.engine, netconfig.virtual_steps(entry, create=False))
                logger.info("%s %s configured again", entry["type"], name)
                return ActionResult(action="network.virtual", params=params, target_state="applied")
            netconfig.run(self.engine, netconfig.virtual_steps(entry))
        except (CommandExecutionError, CommandTranslationError, KeyError, ValueError) as exc:
            logger.error("Failed to create %s %s: %s", entry["type"], name, exc)
            return ActionResult(action="network.virtual", params=params, target_state="failed",
                                error=str(exc))
        links.append(name)
        logger.info("%s %s created", entry["type"], name)
        return ActionResult(action="network.virtual", params=params, target_state="applied")

    def _do_interface(self, name: str, config: dict, renamed: bool = False,
                      reapply: bool = False) -> ActionResult:
        """
        Only an interface the system holds no FreedomNAS configuration for:
        after a reflash or on new hardware. A configured one is left alone, so
        a boot never takes the network down to re-apply what is already there,
        unless this boot took the network of the machine from a data pool.
        """
        params = {"interface": name}
        try:
            if netconfig.is_configured(self.engine, name) and not reapply:
                if not renamed:
                    return ActionResult(action="network.interface", params=params, target_state="skipped",
                                        error="already configured")
                self.engine.execute("network", "connection_activate", timeout=60, interface=name)
                logger.info("Interface %s activated under its new name", name)
                return ActionResult(action="network.interface", params=params, target_state="applied")
            netconfig.apply(self.engine, name, config)
        except (CommandExecutionError, CommandTranslationError, KeyError, ValueError) as exc:
            logger.error("Failed to configure interface %s: %s", name, exc)
            return ActionResult(action="network.interface", params=params, target_state="failed",
                                error=str(exc))
        logger.info("Interface %s configured", name)
        return ActionResult(action="network.interface", params=params, target_state="applied")

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

    def _do_pool_adopt(self, pool: dict) -> List[ActionResult]:
        """
        An imported pool in use: in the default cachefile, so the system's
        own import at boot brings it back mounted, and its datasets mounted.
        """
        pool_name = pool.get("name", "")
        params = {"pool_name": pool_name}
        results = []
        for action in ("pool_cachefile_default", "mount_pool_auto"):
            try:
                self.engine.execute("zfs", action, pool_name=pool_name, timeout=300)
                results.append(ActionResult(action=f"zfs.{action}", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("%s failed for pool %s: %s", action, pool_name, exc)
                results.append(ActionResult(action=f"zfs.{action}", params=params, target_state="failed", error=str(exc)))
        return results

    def _do_pool_acltype(self, pool: dict) -> List[ActionResult]:
        """
        The edition's ACL type on a pool's datasets: FreeBSD's ZFS speaks
        NFSv4 ACLs, Linux's POSIX ones, and a pool restored from the other
        edition keeps the type it was created with, which this one ignores.
        Only the property changes, not a file: the pool's root dataset and
        any dataset set apart locally get it, the rest inherit it. The
        ACLs FreedomNAS writes are the trivial ones the file mode already
        says, so the mode carries them across.
        """
        pool_name = pool.get("name", "")
        try:
            want = self.engine.get_profile_value("zfs", "pool_acltype")
            stdout, _ = self.engine.execute("zfs", "acltype_list", pool_name=pool_name, check=False)
        except (CommandExecutionError, CommandTranslationError):
            return []
        results = []
        for line in stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            name, value, source = fields[0], fields[1], fields[2]
            if value == want or (name != pool_name and source != "local"):
                continue
            params = {"dataset": name, "acltype": want}
            try:
                self.engine.execute("zfs", "dataset_set", property="acltype", value=want, dataset=name)
                logger.info("acltype of %s: %s -> %s", name, value, want)
                results.append(ActionResult(action="zfs.dataset_set", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Could not set acltype=%s on %s: %s", want, name, exc)
                results.append(ActionResult(action="zfs.dataset_set", params=params,
                                            target_state="failed", error=str(exc)))
        return results

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: users
    # ------------------------------------------------------------------

    def _user_exists(self, username: str) -> bool:
        try:
            stdout, _ = self.engine.execute(
                "users", "get_user_info", username=username, check=False
            )
        except (CommandExecutionError, CommandTranslationError):
            return False
        return bool(stdout.strip())

    def _uid_from_data(self, username: str) -> Optional[str]:
        """
        The ID a user's files carry, for a configuration that records none:
        a share given to its owner is that owner's directory, and a restored
        pool brings it with the old system's ID. Recreated under a new ID the
        user would lose those files, and whoever got the old one gain them.
        Pools are mounted before users are created.
        """
        for path in self._owned_paths.get(username, []):
            try:
                stdout, _ = self.engine.execute("directories", "owner_uid_get", path=path, check=False)
            except (CommandExecutionError, CommandTranslationError):
                continue
            uid = stdout.strip()
            if uid.isdigit() and uid != "0":
                return uid
        return None

    def _wanted_ids(self) -> tuple:
        users = [u for u in (self.config.get("users") or []) if isinstance(u, dict)]
        return ({u["uid"] for u in users if isinstance(u.get("uid"), int)},
                {u["gid"] for u in users if isinstance(u.get("gid"), int)})

    def _group_present(self, group: str) -> bool:
        try:
            stdout, _ = self.engine.execute("users", "group_exists", group=group, check=False)
        except (CommandExecutionError, CommandTranslationError):
            return True
        return bool(stdout.strip())

    def _do_user_create(self, user: dict) -> ActionResult:
        """
        A group the account belongs to that does not exist yet (docker, until
        the containers module installs Docker later in the same reconcile)
        is left out of the creation: the next reconcile adds the membership.
        """
        username = user.get("username", "")
        wanted = user.get("groups", [])
        present = [g for g in wanted if self._group_present(g)]
        if len(present) < len(wanted):
            logger.info("%s created without %s: not on this system yet", username,
                        ", ".join(g for g in wanted if g not in present))
        groups = ",".join(present)
        params = {"username": username, "groups": groups}
        if self._user_exists(username):
            return ActionResult(
                action="users.create", params=params,
                target_state="skipped", error="user already exists",
            )
        uid = str(user.get("uid") or self._uid_from_data(username) or "")
        wanted_uids, wanted_gids = self._wanted_ids()
        if uid and self._user_exists(uid):
            try:
                room = shadow.make_room(self.engine, "uid", int(uid), wanted_uids)
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Could not free uid %s for %s: %s", uid, username, exc)
                room = None
            if room is None:
                logger.warning("uid %s of %s is taken here: created with a new one, "
                               "its files keep the old owner", uid, username)
                uid = ""
            elif room:
                self._renumbered = True
        if uid:
            params["uid"] = uid
        try:
            self.engine.execute(
                "users", "create_with_uid" if uid else "create",
                username=username,
                groups=groups,
                uid=uid,
                gid=user.get("gid", ""),
                shell=user.get("shell", "/bin/bash"),
            )
            logger.info("Created user: %s%s", username, f" (uid {uid})" if uid else "")
            if isinstance(user.get("gid"), int):
                for line in shadow.take_ids(self.engine, username, gid=user["gid"], wanted_gids=wanted_gids):
                    logger.info("Realigned %s", line)
                    self._renumbered = True
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

        # Sorted: a set's order changes with every interpreter, and the log and
        # the results should not.
        for group in sorted(desired_groups - current_groups):
            params = {"username": username, "group": group}
            try:
                self.engine.execute("users", "add_to_group", username=username, group=group)
                logger.info("Added %s to group %s", username, group)
                results.append(ActionResult(action="users.add_to_group", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to add %s to group %s: %s", username, group, exc)
                results.append(ActionResult(action="users.add_to_group", params=params, target_state="failed", error=str(exc)))

        for group in sorted(current_groups - desired_groups):
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
        """
        Remove an account the configuration no longer names. Its home
        directory stays: a stale configuration must never cost anyone their
        files. It leaves Samba's database first, as it does from the WebUI.
        """
        params = {"username": username}
        try:
            self.engine.execute("samba", "check_installed")
            self.engine.execute("samba", "delete_user", username=username, check=False)
        except (CommandExecutionError, CommandTranslationError):
            pass
        try:
            self.engine.execute("users", "delete_keep_home", username=username)
            logger.info("Removed user %s, home directory kept", username)
            return ActionResult(action="users.delete_keep_home", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to remove user %s: %s", username, exc)
            return ActionResult(action="users.delete_keep_home", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: SMB shares
    # ------------------------------------------------------------------

    def _do_owner_group(self, share: dict) -> List[ActionResult]:
        """
        A share's directory given to its owner carries the edition's group
        for it (the owner's own on Linux, wheel on FreeBSD). Files restored
        from another edition carry that one's: when the directory itself
        has another group, the whole tree is given the right one, once;
        at the next boot the directory already matches and nothing runs.
        Only when the directory's owner is that user's number here: when the
        numbers differ, this system gave the name another uid, and the group
        is left alone rather than handed to whoever holds that number.
        """
        path, owner = share["path"], share["owner"]
        try:
            want = owner_group(self.engine, owner)
            stdout, _ = self.engine.execute("directories", "group_get", path=path, check=False)
        except (CommandExecutionError, CommandTranslationError):
            return []
        have = stdout.strip()
        if not have or have == want:
            return []
        params = {"path": path, "group": want}
        mismatch = self._owner_mismatch(path, owner)
        if mismatch:
            logger.warning("Group of %s left as %s: %s", path, have, mismatch)
            return [ActionResult(action="directories.group_set_recursive", params=params,
                                 target_state="skipped", error=mismatch)]
        try:
            self.engine.execute("directories", "group_set_recursive", group=want, path=path, timeout=3600)
            logger.info("Group of %s: %s -> %s", path, have, want)
            return [ActionResult(action="directories.group_set_recursive", params=params, target_state="applied")]
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Could not give %s the group %s: %s", path, want, exc)
            return [ActionResult(action="directories.group_set_recursive", params=params,
                                 target_state="failed", error=str(exc))]

    def _profile_section(self, category: str) -> dict:
        try:
            return self.engine.get_profile_section(category)
        except AttributeError:
            return {}

    def _do_power_button(self) -> List[ActionResult]:
        """
        A short press of the power button shuts the NAS down, as on any
        headless machine. Editions whose system does nothing with it by
        itself (no systemd-logind) name in `power_button` the acpid that
        listens for it and the rule FreedomNAS gives it. An install from
        before gets acpid here once (the `power-button` package category,
        which the installer puts on new systems), the rule whenever it is
        missing or different, and acpid running.
        """
        section = self._profile_section("power_button")
        results: List[ActionResult] = []
        if not Path(section["rule_dir"]).is_dir():
            try:
                pm = PackageManager(str(Path(self.engine.profile_dir) / "packages"))
                packages = pm.get_packages_for_os(section["category"], self.engine.os_type, self.engine.init_system)
                self.engine.execute("packages", "install", package_name=" ".join(packages), timeout=1800)
                results.append(ActionResult(action="power_button.install", params={"packages": packages},
                                            target_state="applied"))
            except (CommandExecutionError, CommandTranslationError, OSError, ValueError, KeyError) as exc:
                logger.error("Could not install acpid for the power button: %s", exc)
                return [ActionResult(action="power_button.install", params={}, target_state="failed",
                                     error=str(exc))]
        rule = Path(section["rule_file"])
        try:
            changed = not rule.exists() or rule.read_text() != section["rule"]
            if changed:
                rule.write_text(section["rule"])
        except OSError as exc:
            return results + [ActionResult(action="power_button.rule", params={"file": str(rule)},
                                           target_state="failed", error=str(exc))]
        service = section["service"]
        if self.config.get_service_boot().get(service) is False:
            return results
        params = {"service_name": service}
        try:
            self.engine.execute("services", "enable", service_name=service)
            stdout, _ = self.engine.execute("services", "is_running", check=False, service_name=service)
            if changed or stdout.strip() != "yes":
                self.engine.execute("services", "restart" if stdout.strip() == "yes" else "start",
                                    service_name=service)
                logger.info("Power button: %s %s", service, "rule written" if changed else "started")
                results.append(ActionResult(action="power_button.service", params=params, target_state="applied"))
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Could not start %s for the power button: %s", service, exc)
            results.append(ActionResult(action="power_button.service", params=params, target_state="failed",
                                        error=str(exc)))
        return results

    def _do_share_group(self, share: dict) -> List[ActionResult]:
        """
        A group share's group exists before the accounts that belong to it
        are created, and its directory carries that group (groupshares.restore).
        """
        params = {"group": share["group"], "path": share["path"]}
        try:
            done = groupshares.restore(self.engine, share)
            if not done:
                return []
            logger.info("Group share %s: %s", share["name"], done)
            return [ActionResult(action="groupshares.restore", params=params, target_state="applied")]
        except (CommandExecutionError, CommandTranslationError, groupshares.datasets.DatasetProblem) as exc:
            logger.error("Could not restore the group share %s: %s", share["name"], exc)
            return [ActionResult(action="groupshares.restore", params=params, target_state="failed", error=str(exc))]

    def _owner_mismatch(self, path: str, owner: str) -> Optional[str]:
        try:
            stdout, _ = self.engine.execute("directories", "owner_uid_get", path=path, check=False)
            info, _ = self.engine.execute("users", "get_user_info", username=owner, check=False)
        except (CommandExecutionError, CommandTranslationError):
            return None
        on_disk = stdout.strip()
        fields = info.strip().split(":")
        here = fields[2] if len(fields) > 2 else ""
        if on_disk.isdigit() and here.isdigit() and on_disk != here:
            return f"its owner is uid {on_disk}, {owner} is uid {here} on this system"
        return None

    def _do_usershares_prepare(self) -> ActionResult:
        """
        A restored configuration's shares reach a system no wizard has
        prepared, and `net usershare add` needs a running smbd to turn user
        names into SIDs: at boot the reconciler can be ahead of it.
        """
        try:
            usershares.prepare(self.engine)
            service = self.engine.get_profile_value("samba", "smb_service")
            stdout, _ = self.engine.execute("services", "is_running", check=False, service_name=service)
            if stdout.strip() != "yes":
                self.engine.execute("services", "start", timeout=60, service_name=service)
            return ActionResult(action="samba.usershare_prepare", params={}, target_state="applied")
        except (CommandExecutionError, CommandTranslationError, OSError) as exc:
            logger.error("Could not prepare Samba usershares: %s", exc)
            return ActionResult(action="samba.usershare_prepare", params={}, target_state="failed", error=str(exc))

    def _do_smb_share_create(self, share: dict) -> ActionResult:
        """
        `net usershare add` replaces a share of the same name, so every
        recorded share is written again: its path, comment and ACL end up
        exactly as recorded, whatever changed them in between.
        """
        name = share.get("name", "")
        path = share.get("path", "")
        comment = share.get("comment", "")
        acl = share.get("acl") or self._default_smb_acl(share)
        params = {"share_name": name, "path": path}
        try:
            for attempt in range(SMBD_WAIT_ATTEMPTS):
                try:
                    self.engine.execute(
                        "samba", "create_share",
                        share_name=name,
                        path=path,
                        comment=comment,
                        acl=acl,
                    )
                    break
                except CommandExecutionError as exc:
                    if "smbd is not running" not in str(exc) or attempt == SMBD_WAIT_ATTEMPTS - 1:
                        raise
                    time.sleep(SMBD_WAIT_SECONDS)
            logger.info("SMB share %s → %s", name, path)
            return ActionResult(action="samba.create_share", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to create SMB share %s: %s", name, exc)
            return ActionResult(action="samba.create_share", params=params, target_state="failed", error=str(exc))

    @staticmethod
    def _default_smb_acl(share: dict) -> str:
        """
        Shares recorded before the ACL was stored carry only their users. The
        WebUI gave such a share read access for everyone and full control to
        whoever created it; rebuild that, never more than it.
        """
        if share.get("read_only"):
            return "Everyone:R"
        owners = [u for u in share.get("valid_users", []) if u]
        return ",".join(["Everyone:R"] + [f"{u}:F" for u in owners])

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
    def _wanted_export_line(self, share: dict) -> tuple:
        """
        The line a recorded share is exported as: with the mapping onto its
        owner when it has one, which is in /etc/exports but not in the
        recorded options. An owner who does not exist yet (a restore creates
        the users later) cannot match a line anyway.
        """
        path, clients, options = _export_line(share)
        try:
            options = nfs_export_options(self.engine, share)
        except (CommandExecutionError, CommandTranslationError):
            pass
        return path, clients, options

    # Internal — atomic engine calls: NFS exports
    # ------------------------------------------------------------------

    def _do_nfs_share_create(self, share: dict) -> ActionResult:
        """
        The hosts are what the WebUI records as `allowed_hosts`. A share with
        none is not exported at all: exporting it to `*` would open it to
        every host that can reach the NAS.
        """
        name = share.get("name", "")
        path, clients, options = _export_line(share)
        params = {"name": name, "path": path, "clients": clients}
        if not clients:
            return ActionResult(
                action="nfs.export_add", params=params, target_state="skipped",
                error="no allowed_hosts recorded",
            )
        try:
            self.engine.execute(
                "nfs", "export_add",
                path=path,
                clients=clients,
                options=nfs_export_options(self.engine, {**share, "options": options}),
            )
            self.engine.execute("nfs", "exportfs_reload")
            logger.info("NFS export %s → %s", path, clients)
            return ActionResult(action="nfs.export_add", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to create NFS share %s: %s", name, exc)
            return ActionResult(action="nfs.export_add", params=params, target_state="failed", error=str(exc))

    def _do_nfs_export_remove(self, path: str) -> ActionResult:
        params = {"path": path}
        try:
            self.engine.execute("nfs", "export_remove", path=path)
            self.engine.execute("nfs", "exportfs_reload")
            logger.info("Removed NFS export: %s", path)
            return ActionResult(action="nfs.export_remove", params=params, target_state="applied")
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Failed to remove NFS export %s: %s", path, exc)
            return ActionResult(action="nfs.export_remove", params=params, target_state="failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal — atomic engine calls: services
    # ------------------------------------------------------------------

    def _do_ssh(self, enabled: bool) -> List[ActionResult]:
        """Turn SSH on or off as the configuration records it."""
        try:
            service_name = self.engine.get_profile_value("system", "ssh_service")
        except CommandTranslationError as exc:
            return [ActionResult(action="services.ssh", params={}, target_state="skipped", error=str(exc))]

        results: List[ActionResult] = []
        for action in (("enable", "start") if enabled else ("stop", "disable")):
            params = {"service_name": service_name}
            try:
                self.engine.execute("services", action, service_name=service_name, check=enabled)
                results.append(ActionResult(action=f"services.{action}", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to %s service %s: %s", action, service_name, exc)
                results.append(ActionResult(action=f"services.{action}", params=params, target_state="failed", error=str(exc)))
        return results

    def _do_service_boot(self, name: str, enabled: bool) -> List[ActionResult]:
        """Start a service at boot, or keep it from starting, as the WebUI chose."""
        params = {"service_name": name}
        listed = self._read("services", "list_all")
        if listed is None or name not in listed.split():
            return [ActionResult(action="services.boot", params=params, target_state="skipped",
                                 error=f"no service {name} on this system")]
        results: List[ActionResult] = []
        for action in (("enable", "start") if enabled else ("stop", "disable")):
            try:
                self.engine.execute("services", action, service_name=name, check=enabled)
                results.append(ActionResult(action=f"services.{action}", params=params, target_state="applied"))
            except (CommandExecutionError, CommandTranslationError) as exc:
                logger.error("Failed to %s service %s: %s", action, name, exc)
                results.append(ActionResult(action=f"services.{action}", params=params,
                                            target_state="failed", error=str(exc)))
        return results

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

        if self.config.get_service_boot().get(service_name) is False:
            return [ActionResult(
                action=f"services.enable.{feature_id}",
                params={"feature_id": feature_id, "service_name": service_name},
                target_state="skipped",
                error=f"{service_name} is disabled at boot",
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
