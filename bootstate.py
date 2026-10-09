"""
Automatic rollback after a kernel update that does not boot.

The work happens in the initramfs (dracut module 92freedomnas-fallback) and
in the zz-dracut-uki hook, which keep their state in boot.state on the ESP.
From here the system confirms that it booted, turns the rollback on or off,
reports what happened, and clears the record once the administrator has
seen it. Editions without a UKI (FreeBSD) have none of these commands.
"""

import logging
import re
from typing import Dict, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore

logger = logging.getLogger("FreedomNAS.BootState")

CONFIG_KEY = "system.autorollback"
_FAILED_DATASET = re.compile(r"^[A-Za-z0-9_.:/-]+-failed-\d{8}-\d{6}$")


def autorollback_enabled(config) -> bool:
    return config.get(CONFIG_KEY, True) is not False


def _flag(enabled: bool) -> str:
    return "yes" if enabled else "no"


def confirm_boot(engine, config) -> bool:
    """End a kernel's trial: called once the system has finished booting."""
    try:
        engine.execute("bootloader", "boot_ok", autorollback=_flag(autorollback_enabled(config)))
    except CommandTranslationError:
        return False
    except CommandExecutionError as exc:
        logger.warning("could not confirm the boot: %s", exc)
        return False
    return True


def read_state(engine) -> Optional[Dict[str, str]]:
    """boot.state as a dict, or None where the edition has no UKI."""
    try:
        out, _ = engine.execute("bootloader", "boot_state")
    except CommandTranslationError:
        return None
    except CommandExecutionError as exc:
        logger.warning("could not read boot.state: %s", exc)
        return {}
    state = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            state[key.strip()] = value.strip()
    return state


def summary(engine, config) -> Dict[str, object]:
    state = read_state(engine)
    result = {
        "supported": state is not None,
        "autorollback": autorollback_enabled(config),
        "state": (state or {}).get("state", "ok"),
        "rollback": None,
    }
    if state and state.get("date") and state.get("failed-kernel"):
        dataset = state.get("failed-dataset", "")
        result["rollback"] = {
            "date": state["date"],
            "failed_kernel": state["failed-kernel"],
            "restored_kernel": state.get("restored-kernel", ""),
            "reason": state.get("reason", ""),
            "snapshot": state.get("snapshot", ""),
            "system_restored": state.get("fs-rollback") == "yes",
            "failed_dataset": dataset if _FAILED_DATASET.match(dataset) else "",
        }
    return result


def set_autorollback(engine, config, enabled: bool) -> None:
    """Dual write: boot.state for the initramfs, config.yaml for a restore."""
    engine.execute("bootloader", "autorollback_set", autorollback=_flag(enabled))
    config.set(CONFIG_KEY, enabled)


def forget(engine, destroy_failed: bool) -> Optional[str]:
    """
    Clear the record of a rollback, and with destroy_failed also the failed
    root dataset it kept. Returns the dataset destroyed, if any.
    """
    destroyed = None
    if destroy_failed:
        dataset = (read_state(engine) or {}).get("failed-dataset", "")
        if dataset and _FAILED_DATASET.match(dataset):
            engine.execute("zfs", "dataset_destroy_recursive", dataset=dataset)
            destroyed = dataset
    engine.execute("bootloader", "rollback_forget")
    return destroyed
