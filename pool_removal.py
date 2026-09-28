"""
Exporting and destroying a pool from the Pools tab.

Export is the reversible one: the pool leaves this system and can be
imported again, here or elsewhere. Destroy is not. Both are refused for the
pool the system runs from, for the pool serving the live configuration, and
for a pool whose datasets mount on system directories — the same rule the
Datasets tab follows, since removing the pool removes every dataset in it.

`zpool` is never given -f: a dataset in use makes ZFS refuse, and that
refusal reaches the administrator as it is.
"""

import logging
from typing import Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas import datasets, keymgmt, shadow
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore
    import datasets  # type: ignore
    import keymgmt  # type: ignore
    import shadow  # type: ignore

logger = logging.getLogger("FreedomNAS.PoolRemoval")

ACTIONS = {"export": "pool_export", "destroy": "pool_destroy"}


class RemovalProblem(Exception):
    """A pool that must not be exported or destroyed; the message is for the administrator."""


def problem(engine, pool: str, rows: Optional[List] = None, system_pool: Optional[str] = None,
            active: Optional[str] = None) -> Optional[str]:
    """Why `pool` must stay, or None."""
    system_pool = datasets.root_pool(engine) if system_pool is None else system_pool
    if pool == system_pool:
        return "it is the pool this system runs from"
    active = shadow.active_dataset(engine) if active is None else active
    if active and active.split("/", 1)[0] == pool:
        return "it holds the configuration this system is using"
    rows = datasets.listing(engine) if rows is None else rows
    return datasets.subtree_reason(rows, pool, system_pool)


def contents(engine, pool: str) -> Dict:
    """The datasets the pool holds, where they mount, and whether it is encrypted."""
    members = [(name, mp) for name, mp in datasets.listing(engine)
               if name == pool or name.startswith(pool + "/")]
    try:
        status, _ = engine.execute("zfs", "pool_keystatus", pool_name=pool, check=False)
    except (CommandExecutionError, CommandTranslationError):
        status = ""
    return {
        "datasets": [name for name, _ in members],
        "mountpoints": [mp for _, mp in members if mp.startswith("/")],
        "encrypted": status.strip() in ("available", "unavailable"),
        "keyslots": keymgmt.KeyslotStore(pool).exists(),
    }


def remove(engine, pool: str, action: str) -> Dict:
    """Export or destroy `pool`. Raises RemovalProblem when it must stay."""
    if action not in ACTIONS:
        raise RemovalProblem("The action is export or destroy")
    reason = problem(engine, pool)
    if reason:
        raise RemovalProblem(f"{pool} cannot be removed: {reason}")
    engine.execute("zfs", ACTIONS[action], pool_name=pool, timeout=300)
    logger.info("Pool %s: %s", pool, action)
    forgot_keyslots = False
    if action == "destroy":
        store = keymgmt.KeyslotStore(pool)
        if store.exists():
            try:
                store.path.unlink()
                forgot_keyslots = True
            except OSError as exc:
                logger.warning("Could not remove the keyslots of %s: %s", pool, exc)
    return {"pool": pool, "action": action, "forgot_keyslots": forgot_keyslots}
