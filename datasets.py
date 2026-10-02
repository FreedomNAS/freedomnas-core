"""
Creating, moving, renaming and destroying datasets from the Datasets tab.

The pool is the record of its datasets: nothing here is written to
config.yaml, which only names what depends on a dataset — a share by its
path, a snapshot policy by its name. Those are the caller's to follow.

Two kinds of dataset are never touched: those of the pool the running
system boots from, where a moved mountpoint can leave the system unable to
start, and `.freedomnas`, whose mountpoint is the state directory by design.
"""

import posixpath
import re
from typing import Dict, List, Optional, Tuple

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas import pool_import, shadow
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore
    import pool_import  # type: ignore
    import shadow  # type: ignore

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
MOUNTPOINT_MODES = ("inherit", "path", "none")


class DatasetProblem(Exception):
    """A request that cannot be carried out; the message is for the administrator."""


def root_pool(engine) -> str:
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path="/", check=False)
    except (CommandExecutionError, CommandTranslationError):
        return ""
    return stdout.strip().split("/", 1)[0]


def protected_reason(name: str, system_pool: str, mountpoint: str = "") -> Optional[str]:
    if system_pool and (name == system_pool or name.startswith(system_pool + "/")):
        return "it belongs to the pool this system runs from"
    if shadow.DATASET_NAME in name.split("/"):
        return "it holds the FreedomNAS configuration"
    if mountpoint == "legacy":
        return "it is mounted through /etc/fstab"
    if mountpoint.startswith("/") and pool_import.is_system_path(mountpoint):
        return f"it mounts on {mountpoint}, a system directory"
    return None


def subtree_reason(rows: List[Tuple[str, str]], name: str, system_pool: str) -> Optional[str]:
    """
    Why `name` must not be moved, renamed or destroyed: its own reason, or
    that of a dataset inside it, which those changes would take along.
    """
    for other, mountpoint in rows:
        if other != name and shadow.DATASET_NAME in other.split("/"):
            continue
        if other == name or other.startswith(name + "/"):
            reason = protected_reason(other, system_pool, mountpoint)
            if reason:
                return reason if other == name else f"{other} inside it: {reason}"
    return None


def listing(engine) -> List[Tuple[str, str]]:
    """(name, mountpoint) of every dataset."""
    stdout, _ = engine.execute("zfs", "dataset_list")
    rows = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            rows.append((parts[0], parts[4]))
    return rows


def exists(engine, name: str) -> bool:
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=name, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return False
    return stdout.strip() == name


def mountpoint_of(engine, name: str) -> Tuple[str, str]:
    """(value, source) of a dataset's mountpoint."""
    stdout, _ = engine.execute("zfs", "dataset_get_property", property="mountpoint", dataset=name)
    parts = stdout.strip().split("\t")
    return parts[0], parts[1] if len(parts) > 1 else ""


def _check_name(name: str) -> None:
    parts = name.split("/")
    if len(parts) < 2 or not all(_COMPONENT.match(p) for p in parts):
        raise DatasetProblem(
            "A dataset name is pool/name: each part starts with a letter or digit and "
            "holds letters, digits, _ . : -")
    if len(name) > 255:
        raise DatasetProblem("A dataset name is at most 255 characters")


def _check_writable(engine, name: str) -> None:
    reason = subtree_reason(listing(engine), name, root_pool(engine))
    if reason:
        raise DatasetProblem(f"{name} cannot be changed here: {reason}")


def _check_target(path: str) -> None:
    problem = pool_import.mountpoint_problem(path)
    if problem:
        raise DatasetProblem(problem)
    occupied = pool_import.occupied(path)
    if occupied:
        raise DatasetProblem(f"{path}: {occupied}")


def create(engine, parent: str, name: str, mode: str = "inherit",
           mountpoint: str = "", properties: Optional[Dict[str, str]] = None) -> str:
    """
    Create parent/name. `properties` must already be validated against the
    edition's catalogue. Returns the new dataset's name.
    """
    full = f"{parent}/{name}"
    if "/" in name:
        raise DatasetProblem("The name is one part: choose the dataset it goes in as its parent")
    reason = protected_reason(full, root_pool(engine))
    if reason:
        raise DatasetProblem(f"{full} cannot be created here: {reason}")
    _check_name(full)
    if not exists(engine, parent):
        raise DatasetProblem(f"{parent} does not exist")
    if exists(engine, full):
        raise DatasetProblem(f"{full} already exists")
    if mode not in MOUNTPOINT_MODES:
        raise DatasetProblem("The mountpoint is inherit, path or none")

    options = []
    if mode == "path":
        _check_target(mountpoint)
        options.append(f"mountpoint={mountpoint}")
    elif mode == "none":
        options.append("mountpoint=none")
    else:
        parent_mp, _ = mountpoint_of(engine, parent)
        if parent_mp.startswith("/"):
            _check_target(posixpath.join(parent_mp, name))
    for prop, value in (properties or {}).items():
        options.append(f"{prop}={value}")
    if any(c.isspace() for o in options for c in o):
        raise DatasetProblem("Property values may not contain spaces")

    engine.execute("zfs", "dataset_create_with",
                   options=" ".join(f"-o {o}" for o in options), dataset=full, timeout=120)
    return full


def set_mountpoint(engine, name: str, mode: str, mountpoint: str = "") -> Dict[str, str]:
    """Move a dataset. Returns its mountpoint before and after."""
    _check_writable(engine, name)
    if mode not in MOUNTPOINT_MODES:
        raise DatasetProblem("The mountpoint is inherit, path or none")
    old, _ = mountpoint_of(engine, name)
    if mode == "path":
        if mountpoint == old:
            return {"old": old, "new": old}
        _check_target(mountpoint)
        engine.execute("zfs", "dataset_set", property="mountpoint", value=mountpoint,
                       dataset=name, timeout=120)
    elif mode == "none":
        engine.execute("zfs", "dataset_set", property="mountpoint", value="none",
                       dataset=name, timeout=120)
    else:
        if "/" not in name:
            raise DatasetProblem("A pool's own dataset has no parent to inherit from")
        parent_mp, _ = mountpoint_of(engine, name.rsplit("/", 1)[0])
        target = posixpath.join(parent_mp, name.rsplit("/", 1)[1]) if parent_mp.startswith("/") else ""
        if target and target != old:
            _check_target(target)
        engine.execute("zfs", "dataset_inherit", property="mountpoint", dataset=name, timeout=120)
    new, _ = mountpoint_of(engine, name)
    return {"old": old, "new": new}


def rename(engine, name: str, new_name: str) -> Dict[str, str]:
    """Rename within the same pool. Returns its mountpoint before and after."""
    _check_writable(engine, name)
    _check_name(new_name)
    reason = protected_reason(new_name, root_pool(engine))
    if reason:
        raise DatasetProblem(f"{new_name} cannot be used: {reason}")
    if "/" not in name:
        raise DatasetProblem("A pool's own dataset is renamed by importing the pool under another name")
    if new_name.split("/", 1)[0] != name.split("/", 1)[0]:
        raise DatasetProblem("A dataset stays in its pool: the new name must start with the same pool")
    if new_name == name or new_name.startswith(name + "/"):
        raise DatasetProblem("A dataset cannot move inside itself")
    if exists(engine, new_name):
        raise DatasetProblem(f"{new_name} already exists")
    if not exists(engine, new_name.rsplit("/", 1)[0]):
        raise DatasetProblem(f"{new_name.rsplit('/', 1)[0]} does not exist")
    old, source = mountpoint_of(engine, name)
    if source not in ("local", "received") and old.startswith("/"):
        parent_mp, _ = mountpoint_of(engine, new_name.rsplit("/", 1)[0])
        if parent_mp.startswith("/"):
            _check_target(posixpath.join(parent_mp, new_name.rsplit("/", 1)[1]))
    engine.execute("zfs", "dataset_rename", dataset=name, new_name=new_name, timeout=120)
    new, _ = mountpoint_of(engine, new_name)
    return {"old": old, "new": new}


def dependents(engine, name: str) -> Dict[str, List[str]]:
    """The child datasets and the snapshots that destroying `name` takes along."""
    children, snapshots = [], []
    stdout, _ = engine.execute("zfs", "dataset_list")
    for line in stdout.splitlines():
        other = line.split("\t")[0]
        if other.startswith(name + "/"):
            children.append(other)
    stdout, _ = engine.execute("zfs", "snapshot_list")
    for line in stdout.splitlines():
        snap = line.split("\t")[0]
        if snap.startswith(name + "@") or snap.startswith(name + "/"):
            snapshots.append(snap)
    return {"children": children, "snapshots": snapshots}


def destroy(engine, name: str, recursive: bool = False) -> None:
    _check_writable(engine, name)
    if "/" not in name:
        raise DatasetProblem("A pool's own dataset goes with the pool, not from here")
    if not recursive:
        found = dependents(engine, name)
        if found["children"] or found["snapshots"]:
            raise DatasetProblem(
                f"{name} has {len(found['children'])} child dataset(s) and "
                f"{len(found['snapshots'])} snapshot(s): tick that they go too")
    engine.execute("zfs", "dataset_destroy_recursive" if recursive else "dataset_destroy",
                   dataset=name, timeout=300)


# ---------------------------------------------------------------------------
# Owner
#
# A new dataset's directory belongs to root, mode 755: shared over SMB or NFS
# it can be read and never written. Handing it to one account — owner, mode
# 700 — makes it that account's alone, for the shares too, since Samba and
# NFS reach it as that user. Only the directory a dataset mounts on is ever
# handed over, never what is inside it nor any other directory.
# ---------------------------------------------------------------------------

OWNER_MODE = "0700"


def dataset_mounted_at(engine, path: str) -> Optional[str]:
    """
    The dataset whose own mountpoint is `path` and that may change hands, or
    None: a subdirectory, a system directory or a protected dataset.
    """
    path = posixpath.normpath(path) if path.startswith("/") else ""
    if not path:
        return None
    try:
        stdout, _ = engine.execute("zfs", "dataset_at_path", path=path, check=False)
    except (CommandExecutionError, CommandTranslationError):
        return None
    name = stdout.strip()
    if not name or "\n" in name:
        return None
    try:
        mountpoint, _ = mountpoint_of(engine, name)
    except (CommandExecutionError, CommandTranslationError):
        return None
    if mountpoint != path or protected_reason(name, root_pool(engine), mountpoint):
        return None
    return name


def owner_of(engine, path: str) -> str:
    stdout, _ = engine.execute("directories", "owner_get", path=path)
    return stdout.strip()


def give_to(engine, path: str, owner: str) -> str:
    """Make the directory `path` mounts a dataset on `owner`'s alone. Returns the dataset."""
    name = dataset_mounted_at(engine, path)
    if not name:
        raise DatasetProblem(
            f"{path} is not where a dataset of a data pool is mounted: its owner stays as it is")
    try:
        engine.execute("users", "get_user_info", username=owner)
    except CommandExecutionError:
        raise DatasetProblem(f"There is no account named {owner}")
    engine.execute("directories", "owner_set", owner=owner, path=path)
    engine.execute("directories", "mode_set", mode=OWNER_MODE, path=path)
    return name


def nfs_export_options(engine, share: dict) -> str:
    """
    The options an NFS share is exported with. NFS has no login: the server
    takes the client's word for the user ID, and an ID that differs between
    the client and the NAS is someone else. A share whose directory was
    given to its owner (`map_to_owner`) maps every client user onto that
    owner (`nfs.owner_options`), and the hosts allowed to mount it decide
    who that is. The mapping is added when the export is written, never
    recorded in `options`: the IDs are this system's, and a restore or a
    moved dataset writes the export again.
    """
    options = share.get("options") or "rw,sync,no_subtree_check"
    owner = share.get("owner")
    if not share.get("map_to_owner") or not owner:
        return options
    stdout, _ = engine.execute("users", "get_user_info", username=owner)
    fields = stdout.strip().split(":")
    if len(fields) < 4 or not fields[2].isdigit() or not fields[3].isdigit():
        raise CommandExecutionError(f"Cannot read the user and group ID of {owner}")
    template = engine.get_profile_value("nfs", "owner_options")
    return options + template.format(owner=owner, uid=fields[2], gid=fields[3])
