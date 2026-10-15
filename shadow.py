"""
The configuration's two kinds of `.freedomnas` dataset.

`config.yaml` describes what this system should be. Two kinds of copy of it
matter, and they are not equal:

  * The internal one, `<root pool>/.freedomnas`, mounted at
    <config_dir>/state. It belongs to this install and is the truth about
    it: who administers it, its edition, its services.

  * The external ones, `<data pool>/.freedomnas`, one on every other pool
    that carries the dataset. When the disk that holds the root pool dies,
    what survives is the data pool, and that is where the appliance's
    identity has to be waiting: a reinstall onto a blank disk restores it
    from there. Their mountpoint is <config_dir>/shared, and they are only
    ever mounted for a moment somewhere else, to be read or written: they
    never compete with the internal one for the state directory.

Every save stamps `system.saved_at`, and a copy on a data pool carries the
stamp of the save it mirrors. At boot, what belongs to the pools is taken
from the newest external copy, whichever install wrote it and however old
it is next to the internal one: shares, snapshot policies, modules, pools,
and users this install does not have. What belongs to this install — its
users' roles and IDs, its edition, its services — is never overwritten
from outside.

The network and the hostname belong to the machine, not to the install: the
cards are known by their MAC addresses. An external copy that knows a card
of this machine gives them, whichever install wrote it last, so every
edition booted on the same box comes up with the same addresses. A copy
that knows none of the cards here was written on another machine, which
may still be running with those addresses: an install that has a network
of its own keeps it. When the network is taken, the reconciler applies it
again at that boot, also to interfaces already configured.

The internal dataset carries org.freedomnas:role=internal. A pool that is
another install's root — that property on its `.freedomnas`, or a bootfs —
is left alone: never read, written, moved or kept imported.
"""

import contextlib
import copy
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import yaml

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas import netnames, paths
    from freedomnas.config import ConfigurationManager, known_admin_groups, normalize_user
    from freedomnas.package_manager import admin_groups_present
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore
    import netnames  # type: ignore
    import paths  # type: ignore
    from config import ConfigurationManager, known_admin_groups, normalize_user  # type: ignore
    from package_manager import admin_groups_present  # type: ignore

logger = logging.getLogger("FreedomNAS.Shadow")

DATASET_NAME = ".freedomnas"
CONFIG_NAME = "config.yaml"
ROLE_PROPERTY = "org.freedomnas:role"
NETWORK_MARKER = "network-reapply"

_ERRORS = (CommandExecutionError, CommandTranslationError)


def _pools(engine) -> List[str]:
    try:
        stdout, _ = engine.execute("zfs", "pool_list", check=False)
    except _ERRORS as exc:
        logger.error("Cannot list pools: %s", exc)
        return []
    return [line.split()[0] for line in stdout.splitlines() if line.split()]


def _exists(engine, dataset: str) -> bool:
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=dataset, check=False)
    except _ERRORS:
        return False
    return stdout.strip() == dataset


def root_pool(engine) -> Optional[str]:
    """The pool this system booted from."""
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path="/", check=False)
    except _ERRORS:
        return None
    name = stdout.strip().split("/", 1)[0]
    return name or None


def internal_dataset(engine) -> Optional[str]:
    pool = root_pool(engine)
    return f"{pool}/{DATASET_NAME}" if pool else None


def active_dataset(engine) -> Optional[str]:
    """The `.freedomnas` currently mounted at <config_dir>/state, if any."""
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=str(paths.state_dir(engine)), check=False)
    except _ERRORS:
        return None
    name = stdout.strip()
    return name if name.endswith(f"/{DATASET_NAME}") else None


def _property(engine, dataset: str, name: str) -> str:
    try:
        stdout, _ = engine.execute("zfs", "dataset_get_property", property=name,
                                   dataset=dataset, check=False)
    except _ERRORS:
        return ""
    return (stdout.split() or [""])[0]


def _mountpoint(engine, dataset: str) -> str:
    return _property(engine, dataset, "mountpoint")


def _bootfs(engine, pool: str) -> str:
    try:
        stdout, _ = engine.execute("zfs", "pool_get_all", pool_name=pool, check=False)
    except _ERRORS:
        return ""
    for line in stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] == "bootfs":
            return "" if fields[1] == "-" else fields[1]
    return ""


def foreign_root(engine, pool: str, root: Optional[str] = None) -> bool:
    """
    Whether `pool` is the root pool of another install: its `.freedomnas` is
    that install's truth, not a copy to read from, write to or keep imported.
    """
    root = root or root_pool(engine)
    if pool == root:
        return False
    if _bootfs(engine, pool):
        return True
    dataset = f"{pool}/{DATASET_NAME}"
    return _exists(engine, dataset) and _property(engine, dataset, ROLE_PROPERTY) == "internal"


def _set_mountpoint(engine, dataset: str, mountpoint: str) -> None:
    if _mountpoint(engine, dataset) != mountpoint:
        engine.execute("zfs", "dataset_set", property="mountpoint", value=mountpoint, dataset=dataset)


def _make_external(engine, dataset: str) -> None:
    """A data pool's copy gets a mountpoint of its own, unless it is in use."""
    if dataset == active_dataset(engine):
        return
    try:
        _set_mountpoint(engine, dataset, str(paths.shared_dir(engine)))
    except _ERRORS as exc:
        logger.warning("Could not move the mountpoint of %s: %s", dataset, exc)


def ensure_dataset(engine, pool: str) -> bool:
    """
    Give a pool its `.freedomnas` dataset. Returns True if it had to be created.

    Called when a pool is created, so that a data pool is ready to hold the
    configuration from the moment it exists.
    """
    dataset = f"{pool}/{DATASET_NAME}"
    if foreign_root(engine, pool):
        return False
    created = False
    if not _exists(engine, dataset):
        engine.execute("zfs", "create_state_dataset", pool_name=pool)
        logger.info("Created %s", dataset)
        created = True
    if pool != root_pool(engine):
        _make_external(engine, dataset)
    return created


@contextlib.contextmanager
def _mounted(engine, dataset: str) -> Iterator[Path]:
    """The dataset mounted for a moment at a temporary directory."""
    mountpoint = tempfile.mkdtemp(prefix="freedomnas-shadow.")
    try:
        engine.execute("zfs", "mount_dataset_at", dataset=dataset, mountpoint=mountpoint)
    except _ERRORS:
        Path(mountpoint).rmdir()
        raise
    try:
        yield Path(mountpoint)
    finally:
        try:
            engine.execute("zfs", "unmount_at", mountpoint=mountpoint, check=False)
        except _ERRORS as exc:
            logger.error("Left %s mounted at %s: %s", dataset, mountpoint, exc)
        try:
            Path(mountpoint).rmdir()
        except OSError:
            pass


def sync(engine, config_path: str) -> List[Dict]:
    """
    Copy the live configuration into every other pool's `.freedomnas`.

    The dataset the configuration is served from is skipped: it is the
    source. Never raises — a pool that cannot be mirrored is reported and
    the others still get their copy.
    """
    source = Path(config_path)
    if not source.is_file():
        logger.warning("No configuration at %s — nothing to mirror", source)
        return []

    active = active_dataset(engine)
    root = root_pool(engine)
    results = []
    for pool in _pools(engine):
        dataset = f"{pool}/{DATASET_NAME}"
        if dataset == active or not _exists(engine, dataset) or foreign_root(engine, pool, root):
            continue
        _make_external(engine, dataset)
        results.append(_copy_into(engine, dataset, source))
    return results


def _copy_into(engine, dataset: str, source: Path) -> Dict:
    result = {"dataset": dataset, "copied": False, "error": None}
    try:
        with _mounted(engine, dataset) as mountpoint:
            shutil.copy2(source, mountpoint / CONFIG_NAME)
        result["copied"] = True
        logger.info("Mirrored the configuration to %s", dataset)
    except _ERRORS as exc:
        result["error"] = f"could not mount: {exc}"
    except OSError as exc:
        result["error"] = str(exc)
    return result


def _read(engine, dataset: str) -> Optional[Dict]:
    try:
        with _mounted(engine, dataset) as mountpoint:
            with open(mountpoint / CONFIG_NAME) as f:
                data = yaml.safe_load(f)
    except FileNotFoundError:
        return None
    except (*_ERRORS, OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read the configuration on %s: %s", dataset, exc)
        return None
    return data if isinstance(data, dict) else None


def _load(path: Path) -> Dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _initialized(config: Dict) -> bool:
    return bool((config.get("system") or {}).get("initialized"))


def _saved_at(config: Dict) -> str:
    return str((config.get("system") or {}).get("saved_at") or "")


def _empty(directory: Path) -> None:
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def activate(engine, pool: Optional[str] = None, state: Optional[Path] = None) -> str:
    """
    Make the root pool's `.freedomnas` the live state: create it if needed,
    move what the state directory holds into it, and mount it there.
    `pool`, when it is another pool, gets its external copy.

    A system set up before the internal dataset existed served its state
    from a data pool's dataset: that one is unmounted, keeps its contents
    and becomes the external copy. Nothing is destroyed.
    """
    internal = internal_dataset(engine)
    if not internal:
        raise CommandExecutionError("The root filesystem is not on a ZFS pool")
    state = Path(state) if state else paths.state_dir(engine)
    current = active_dataset(engine)

    if current != internal:
        created = not _exists(engine, internal)
        if created:
            engine.execute("zfs", "create_state_dataset", pool_name=internal.split("/", 1)[0])
            logger.info("Created %s", internal)
        _set_mountpoint(engine, internal, str(state))
        moving = (created or current) and state.is_dir()
        if moving:
            with _mounted(engine, internal) as mountpoint:
                shutil.copytree(state, mountpoint, dirs_exist_ok=True, symlinks=True)
        if current:
            engine.execute("zfs", "unmount_dataset", dataset=current)
        if moving:
            _empty(state)
        state.mkdir(parents=True, exist_ok=True)
        try:
            engine.execute("zfs", "mount_dataset", dataset=internal)
        except _ERRORS:
            if current:
                engine.execute("zfs", "mount_dataset", dataset=current)
            raise
        if current:
            _make_external(engine, current)
            logger.info("%s is now an external copy", current)
        logger.info("The configuration now lives on %s", internal)

    if _property(engine, internal, ROLE_PROPERTY) != "internal":
        engine.execute("zfs", "dataset_set", property=ROLE_PROPERTY, value="internal", dataset=internal)

    if pool and pool != internal.split("/", 1)[0]:
        ensure_dataset(engine, pool)
    return internal


def merge_shared(local: Dict, external: Dict, admin_extra: Optional[List[str]] = None) -> Dict:
    """
    `local` with what belongs to the pool taken from `external`: shares,
    snapshot policies and modules as `external` has them, pools and users
    added. A user new to this install never arrives as an administrator.
    """
    merged = copy.deepcopy(local)
    shares = external.get("shares")
    if isinstance(shares, dict):
        merged["shares"] = copy.deepcopy(shares)

    policies = (external.get("snapshots") or {}).get("policies")
    if isinstance(policies, list):
        merged.setdefault("snapshots", {})["policies"] = copy.deepcopy(policies)

    modules = external.get("modules")
    if isinstance(modules, dict):
        merged["modules"] = {**(merged.get("modules") or {}), **copy.deepcopy(modules)}

    storage = merged.setdefault("storage", {})
    pools = list(storage.get("pools") or [])
    names = {p.get("name") for p in pools if isinstance(p, dict)}
    for entry in (external.get("storage") or {}).get("pools") or []:
        if isinstance(entry, dict) and entry.get("name") and entry["name"] not in names:
            pools.append(copy.deepcopy(entry))
            names.add(entry["name"])
    storage["pools"] = pools

    users = list(merged.get("users") or [])
    known = {u.get("username") for u in users if isinstance(u, dict)}
    admin_groups = known_admin_groups() | set(admin_extra or [])
    for user in external.get("users") or []:
        if not isinstance(user, dict) or not user.get("username") or user["username"] in known:
            continue
        entry = normalize_user(user, admin_groups)
        entry["admin"] = False
        users.append(entry)
        known.add(user["username"])
    merged["users"] = users
    return merged


def known_macs(config: Dict) -> set:
    network = config.get("network") or {}
    macs = {str(mac).lower() for mac in network.get("interfaces_mapping") or {}}
    macs |= {str(i["mac_address"]).lower() for i in network.get("interfaces") or []
             if isinstance(i, dict) and i.get("mac_address")}
    return macs


def take_network(local: Dict, external: Dict) -> Dict:
    """`local` with the network and the hostname of `external`."""
    taken = copy.deepcopy(local)
    taken["network"] = copy.deepcopy(external.get("network") or {})
    hostname = (external.get("system") or {}).get("hostname")
    if hostname:
        taken.setdefault("system", {})["hostname"] = hostname
    return taken


def _machine(config: Dict):
    return config.get("network") or {}, (config.get("system") or {}).get("hostname")


def _present_macs(engine) -> set:
    try:
        stdout, _ = engine.execute("network", "list_physical", check=False)
    except _ERRORS as exc:
        logger.warning("Could not list the network cards: %s", exc)
        return set()
    return {card["mac"] for card in netnames.parse_physical(stdout)}


def _take_network(engine, config_path: Path, externals: List) -> Optional[str]:
    """
    The network of the newest external copy that knows a card of this
    machine, when it differs from the one here. Interfaces this install had
    and that copy has not are listed for the reconciler to forget.
    """
    present = _present_macs(engine)
    source = next(((pool, data) for pool, data in externals if known_macs(data) & present), None)
    if not source:
        return None
    pool, data = source
    current = _load(config_path)
    wanted = take_network(current, data)
    if _machine(wanted) == _machine(current):
        return None
    names = {i.get("logical_name") for i in (wanted.get("network") or {}).get("interfaces") or []
             if isinstance(i, dict)}
    forget = [i["logical_name"] for i in (current.get("network") or {}).get("interfaces") or []
              if isinstance(i, dict) and i.get("logical_name") and i["logical_name"] not in names
              and i.get("type") != "wireguard"]
    _write(config_path, wanted)
    with open(config_path.parent / NETWORK_MARKER, "w") as f:
        yaml.safe_dump({"source": pool, "forget": forget}, f)
    logger.info("Took the network of this machine from %s", pool)
    return pool


def boot(engine) -> Dict:
    """
    At every boot, once the pools are imported: put the internal dataset in
    place and decide where the configuration comes from.

    action is one of
      local    — the internal configuration, as it is
      merged   — the internal configuration, with the shared sections of the
                 newest external copy
      restored — no initialized internal configuration: the newest external
                 one was copied in (a reinstall)
      none     — nothing anywhere: the setup wizard runs
    """
    if not internal_dataset(engine):
        return {"internal": None, "action": "none", "source": None, "keep": []}
    state = paths.state_dir(engine)
    internal = activate(engine, state=state)
    root = internal.split("/", 1)[0]
    config_path = state / CONFIG_NAME
    local = _load(config_path)

    externals = []
    copies = []
    for pool in _pools(engine):
        dataset = f"{pool}/{DATASET_NAME}"
        if pool == root or not _exists(engine, dataset) or foreign_root(engine, pool, root):
            continue
        copies.append(pool)
        _make_external(engine, dataset)
        data = _read(engine, dataset)
        if data and _initialized(data):
            externals.append((pool, data))
    externals.sort(key=lambda e: _saved_at(e[1]), reverse=True)

    imported = _pools(engine)
    result = {"internal": internal, "action": "none", "source": None, "keep": []}
    if _initialized(local):
        result["action"] = "local"
        if externals:
            pool, data = externals[0]
            merged = merge_shared(local, data, _admin_extra(engine))
            if merged != local:
                _write(config_path, merged)
                result.update(action="merged", source=pool)
                logger.info("Took the shared sections from %s", pool)
        network = _take_network(engine, config_path, externals)
        if network:
            result["network"] = network
    elif externals:
        pool, _ = externals[0]
        with _mounted(engine, f"{pool}/{DATASET_NAME}") as mountpoint:
            shutil.copy2(mountpoint / CONFIG_NAME, config_path)
        restored = _load(config_path)
        changed = _local_ids(engine, restored)
        if (restored.get("system") or {}).get("edition") != engine.os_type:
            restored.setdefault("system", {})["edition"] = engine.os_type
            changed = True
        if changed:
            _write(config_path, restored)
        result.update(action="restored", source=pool)
        logger.info("Restored the configuration from %s", pool)

    final = _load(config_path)
    named = {p.get("name") for p in (final.get("storage") or {}).get("pools") or []
             if isinstance(p, dict)}
    named |= {m.get("pool") for m in (final.get("modules") or {}).values()
              if isinstance(m, dict) and m.get("pool")}
    named |= set(copies)
    result["keep"] = [p for p in imported if p != root and p in named
                      and not foreign_root(engine, p, root)]
    return result


def _local_ids(engine, config: Dict) -> bool:
    """
    A restored user who already exists here keeps the IDs this system gave
    them: the configuration describes the system, not the one it came from.
    """
    changed = False
    for user in config.get("users") or []:
        if not isinstance(user, dict) or not user.get("username"):
            continue
        try:
            stdout, _ = engine.execute("users", "get_user_info", username=user["username"], check=False)
        except _ERRORS:
            continue
        fields = stdout.strip().split(":")
        if len(fields) < 4 or not fields[2].isdigit() or not fields[3].isdigit():
            continue
        uid, gid = int(fields[2]), int(fields[3])
        if user.get("uid") != uid or user.get("gid") != gid:
            logger.info("%s keeps uid %s and gid %s of this system", user["username"], uid, gid)
            user["uid"], user["gid"] = uid, gid
            changed = True
    return changed


def _admin_extra(engine) -> List[str]:
    try:
        return admin_groups_present(engine)
    except Exception as exc:
        logger.warning("Could not work out the administrators' groups: %s", exc)
        return []


def _write(path: Path, data: Dict) -> None:
    manager = ConfigurationManager(config_path=str(path))
    manager.config = manager._merge_with_defaults(data)
    manager.save()
