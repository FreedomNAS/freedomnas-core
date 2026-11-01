"""
Boot environments: whole copies of the system that the boot loader can start.

FreeBSD keeps its root in <pool>/ROOT/<name> and `bectl` clones it. Where the
profile says how (`updates.be_update`), an update is installed into a new
copy (`update-<date>`) while the running system stays as it is: the copy
boots once on trial at the next start (`bectl activate -t`), the first boot
that gets as far as `boot-ok` makes it the default, and a start that never
gets there (a panic, a hang the administrator powers off) leaves the previous
system the default, so the start after it is back on the old one, which
records that the update was rolled back. With the automatic rollback turned
off the copy becomes the default at once. Where an update still runs on the
live system, the running system is kept first as `pre-update-<date>`.

A copy the administrator picks under System, or any made before this code,
may carry the fixed `vfs.root.mountfrom` FreedomNAS's installer used to
write into /boot/loader.conf: it would start that copy's kernel on the old
root, so the line is taken out before a copy is activated, and at every boot
from the running one.

Only the copies made here are ever destroyed by themselves: the newest KEEP
stay. Those made by freebsd-update or by hand are left to the administrator.
Editions without boot environments have none of these commands.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

try:
    from freedomnas import paths
except ImportError:  # running from the repository, not from the package
    import paths  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.BootEnv")

PREFIX = "pre-update-"
UPDATE_PREFIX = "update-"
KEEP = 5
STATE_NAME = "bootenv.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class BootEnvProblem(Exception):
    """A request that cannot be carried out; the message is for the administrator."""


def supported(engine) -> bool:
    try:
        engine.get_command("bootloader", "be_list")
    except CommandTranslationError:
        return False
    return True


def parse_list(text: str) -> List[Dict]:
    """`bectl list -H`: name, active flags (N now, R on reboot, T next boot only), mountpoint, space, created."""
    environments = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 5 or not fields[0]:
            continue
        flags = fields[1]
        environments.append({
            "name": fields[0],
            "running": "N" in flags,
            "next_boot": "R" in flags or "T" in flags,
            "once": "T" in flags,
            "space": fields[3],
            "created": fields[4],
            "ours": fields[0].startswith((PREFIX, UPDATE_PREFIX)),
        })
    return environments


def list_all(engine) -> List[Dict]:
    stdout, _ = engine.execute("bootloader", "be_list")
    return parse_list(stdout)


def _find(engine, name: str) -> Dict:
    if not _NAME.match(name or ""):
        raise BootEnvProblem(f"Not a boot environment name: {name!r}")
    for environment in list_all(engine):
        if environment["name"] == name:
            return environment
    raise BootEnvProblem(f"No boot environment named {name}")


def updates_inside(engine) -> bool:
    try:
        engine.get_command("updates", "be_update", root="/")
    except CommandTranslationError:
        return False
    return supported(engine)


def create_before_update(engine, stamp: str) -> Optional[str]:
    """Keep the running system as pre-update-<stamp>, then prune the oldest of ours."""
    if not supported(engine) or updates_inside(engine):
        return None
    name = PREFIX + stamp
    engine.execute("bootloader", "be_create", be=name)
    logger.info("Boot environment %s created", name)
    prune(engine)
    return name


def prune(engine, keep: int = KEEP) -> List[str]:
    failed = (load_state(engine).get("rollback") or {}).get("failed")
    ours = [e for e in list_all(engine) if e["ours"]]
    spare = sorted(e["name"] for e in ours if not e["running"] and not e["next_boot"] and e["name"] != failed)
    newest = set(sorted(e["name"] for e in ours)[-keep:])
    destroyed = []
    for name in spare:
        if name in newest:
            continue
        try:
            engine.execute("bootloader", "be_destroy", be=name)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.warning("Could not destroy the boot environment %s: %s", name, exc)
            continue
        logger.info("Boot environment %s destroyed (more than %d kept)", name, keep)
        destroyed.append(name)
    return destroyed


def activate(engine, name: str) -> None:
    """Boot this environment from the next start on."""
    environment = _find(engine, name)
    if environment["next_boot"] and not environment["once"]:
        return
    if not environment["running"]:
        root = mount(engine, name)
        try:
            engine.execute("bootloader", "be_fix_loader", root=root)
        finally:
            umount(engine, name)
    engine.execute("bootloader", "be_activate", be=name)
    state = load_state(engine)
    if (state.get("trial") or {}).get("be") not in (None, name):
        state.pop("trial", None)
        save_state(engine, state)


def destroy(engine, name: str) -> None:
    environment = _find(engine, name)
    if environment["running"]:
        raise BootEnvProblem(f"{name} is the system running now")
    if environment["next_boot"]:
        raise BootEnvProblem(f"{name} is the system the next start boots: make another one the default first")
    engine.execute("bootloader", "be_destroy", be=name)


# ---------------------------------------------------------------------------
# Updates installed into a new boot environment
# ---------------------------------------------------------------------------

def state_path(engine) -> Path:
    return paths.state_dir(engine) / STATE_NAME


def load_state(engine) -> Dict:
    try:
        data = json.loads(state_path(engine).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(engine, state: Dict) -> None:
    path = state_path(engine)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def running(engine) -> Optional[str]:
    return next((e["name"] for e in list_all(engine) if e["running"]), None)


def mount(engine, name: str) -> str:
    root = engine.get_profile_value("bootloader", "be_mountpoint")
    engine.execute("bootloader", "be_mount", be=name, root=root)
    return root


def umount(engine, name: str) -> None:
    try:
        engine.execute("bootloader", "be_umount", be=name, check=False)
    except CommandTranslationError:
        pass


def fingerprint(engine, root: str) -> str:
    stdout, _ = engine.execute("bootloader", "be_fingerprint", root=root, timeout=300)
    return stdout


def begin_update(engine, stamp: str) -> Dict:
    """
    A new copy of the running system, mounted and ready for the update
    commands. A copy of an earlier update that was never started is dropped
    first: it was made from the same system and this one replaces it.
    """
    state = load_state(engine)
    pending = (state.get("trial") or {}).get("be")
    if pending and pending != running(engine):
        try:
            _find(engine, pending)
            engine.execute("bootloader", "be_destroy", be=pending)
        except (BootEnvProblem, CommandExecutionError) as exc:
            logger.warning("Could not drop the update %s that was never started: %s", pending, exc)
        state.pop("trial", None)
        save_state(engine, state)
    name = UPDATE_PREFIX + stamp
    engine.execute("bootloader", "be_create", be=name)
    try:
        root = mount(engine, name)
        engine.execute("bootloader", "be_enter", root=root)
        before = fingerprint(engine, root)
    except (CommandExecutionError, CommandTranslationError):
        leave(engine, name, engine.get_profile_value("bootloader", "be_mountpoint"))
        engine.execute("bootloader", "be_destroy", be=name, check=False)
        raise
    return {"be": name, "root": root, "before": before}


def update_commands(engine, root: str) -> List[str]:
    commands = engine.get_command("updates", "be_update", root=root)
    return commands if isinstance(commands, list) else [commands]


def leave(engine, name: str, root: str) -> None:
    try:
        engine.execute("bootloader", "be_leave", root=root, check=False)
    except CommandTranslationError:
        pass
    umount(engine, name)


def finish_update(engine, config, update: Dict, succeeded: bool) -> Dict:
    """
    Close the copy. Returns {"result": "failed" | "unchanged" | "trial" |
    "default", "be": name}: a failed or empty update leaves no copy behind.
    """
    name, root = update["be"], update["root"]
    after = ""
    if succeeded:
        try:
            after = fingerprint(engine, root)
            engine.execute("bootloader", "be_fix_loader", root=root)
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.error("Could not check the updated copy %s: %s", name, exc)
            succeeded = False
    leave(engine, name, root)
    if not succeeded or after == update["before"]:
        engine.execute("bootloader", "be_destroy", be=name, check=False)
        return {"result": "failed" if not succeeded else "unchanged", "be": name}
    try:
        from freedomnas.bootstate import autorollback_enabled
    except ImportError:
        from bootstate import autorollback_enabled  # type: ignore
    state = load_state(engine)
    if autorollback_enabled(config):
        engine.execute("bootloader", "be_activate_once", be=name)
        state["trial"] = {"be": name, "from": running(engine),
                          "date": datetime.now().strftime("%Y%m%d-%H%M%S")}
        result = "trial"
    else:
        engine.execute("bootloader", "be_activate", be=name)
        state.pop("trial", None)
        result = "default"
    save_state(engine, state)
    prune(engine)
    return {"result": result, "be": name}


def pending_trial(engine) -> Optional[str]:
    trial = (load_state(engine).get("trial") or {}).get("be")
    return trial if trial and trial != running(engine) else None


def confirm_boot(engine) -> Optional[str]:
    """
    Called once the system has finished starting. The copy on trial is now
    the default if this start is it; if this start is another system, the
    copy did not get this far and the update is recorded as rolled back.
    Returns "confirmed", "rolled-back" or None.
    """
    if not supported(engine):
        return None
    try:
        engine.execute("bootloader", "be_fix_loader", root="/")
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Could not check /boot/loader.conf: %s", exc)
    state = load_state(engine)
    trial = state.get("trial")
    if not trial:
        return None
    now = running(engine)
    if now == trial["be"]:
        engine.execute("bootloader", "be_activate", be=now)
        outcome = "confirmed"
        logger.info("The updated system %s started: it is the default now", now)
    else:
        state["rollback"] = {"date": trial.get("date", ""), "failed": trial["be"], "restored": now or ""}
        outcome = "rolled-back"
        logger.warning("The updated system %s did not finish starting: back on %s", trial["be"], now)
    state.pop("trial", None)
    save_state(engine, state)
    return outcome


def rollback_notice(engine) -> Optional[Dict]:
    if not supported(engine):
        return None
    return load_state(engine).get("rollback")


def forget(engine, destroy_failed: bool) -> Optional[str]:
    state = load_state(engine)
    record = state.pop("rollback", None) or {}
    save_state(engine, state)
    failed = record.get("failed")
    if destroy_failed and failed:
        destroy(engine, failed)
        return failed
    return None
