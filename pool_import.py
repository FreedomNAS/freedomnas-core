"""
Bringing in a pool that already exists: another FreedomNAS's data pool, or a
pool some other system made.

The pool is imported twice. inspect() imports it read-only, unmounted and
under a temporary name, reads what it holds and exports it again: nothing on
the pool changes, not even the host that last used it, so the administrator
can look and walk away. import_pool() then imports it for real, by numeric
id, so that it is the pool that was inspected.

A pool that would mount over this system — another operating system's root,
a dataset on /usr or /etc — is refused outright: ZFS would mount it over the
running system's directories, now and at every boot. A dataset that only
inherits an unfortunate mountpoint is a conflict instead, which choosing
another mountpoint for the pool resolves.
"""

import base64
import binascii
import logging
import os
import posixpath
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas import keymgmt, shadow
except ImportError:  # running from the repository, not from the package
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore
    import keymgmt  # type: ignore
    import shadow  # type: ignore

logger = logging.getLogger("FreedomNAS.PoolImport")

SYSTEM_DIRS = (
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/opt", "/proc", "/root", "/run", "/sbin", "/sys", "/tmp",
    "/usr", "/var",
)
UNMOUNTED = ("none", "legacy")
IMPORTABLE_STATES = ("ONLINE", "DEGRADED")
ADMIN_GROUPS = ("sudo", "wheel")


class ImportProblem(Exception):
    """A pool that cannot be imported as asked; the message is for the administrator."""


def parse_importable(text: str) -> List[Dict]:
    """The pools `zpool import` offers, one dict per `pool:` block."""
    pools: List[Dict] = []
    current: Optional[Dict] = None
    key = None
    for line in text.splitlines():
        stripped = line.strip()
        head, sep, rest = stripped.partition(":")
        if sep and head in ("pool", "id", "state", "status", "action", "config", "see", "comment"):
            key = head
            if key == "pool":
                current = {"name": rest.strip(), "id": "", "state": "", "status": "", "action": ""}
                pools.append(current)
            elif current is not None and key in ("id", "state", "status", "action"):
                current[key] = rest.strip()
        elif current is not None and key in ("status", "action") and stripped:
            current[key] = f"{current[key]} {stripped}".strip()
    for pool in pools:
        pool["foreign"] = ("another system" in pool["status"]
                           or "'-f'" in pool["action"])
        pool["importable"] = pool["state"] in IMPORTABLE_STATES and pool["id"].isdigit()
    return pools


def list_importable(engine) -> List[Dict]:
    stdout, _ = engine.execute("zfs", "pool_importable", check=False, timeout=120)
    return parse_importable(stdout)


def parse_tree(text: str, pool: str) -> List[Dict]:
    """
    `zfs get -rHp` rows as one dict per dataset, parents first, each named
    relative to the pool ("" for the pool's own dataset).
    """
    datasets: Dict[str, Dict] = {}
    order: List[str] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, prop, value, source = parts[:4]
        if name != pool and not name.startswith(pool + "/"):
            continue
        rel = name[len(pool) + 1:] if name != pool else ""
        if rel not in datasets:
            datasets[rel] = {"rel": rel}
            order.append(rel)
        datasets[rel][prop] = value
        if prop == "mountpoint":
            datasets[rel]["mountpoint_explicit"] = source not in ("default", "-") and not source.startswith("inherited")
    return [datasets[rel] for rel in order]


def project_mountpoints(datasets: List[Dict], pool_name: str,
                        root_mountpoint: Optional[str] = None) -> Dict[str, str]:
    """
    Where every dataset would mount once imported as `pool_name`, with the
    pool's own dataset moved to `root_mountpoint` if one is given.
    """
    result: Dict[str, str] = {}
    for ds in datasets:
        rel = ds["rel"]
        value = ds.get("mountpoint", "none")
        if rel == "":
            if root_mountpoint:
                mp = root_mountpoint
            elif ds.get("mountpoint_explicit"):
                mp = value
            else:
                mp = "/" + pool_name
        elif ds.get("mountpoint_explicit"):
            mp = value
        else:
            parent = result.get(posixpath.dirname(rel), "none")
            mp = parent if parent in UNMOUNTED else posixpath.join(parent, posixpath.basename(rel))
        result[rel] = mp
    return result


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + "/")


def _system_path(path: str) -> bool:
    return path == "/" or any(_under(path, d) for d in SYSTEM_DIRS)


def _occupied(path: str) -> Optional[str]:
    if os.path.ismount(path):
        return "something is already mounted there"
    try:
        if os.path.isdir(path) and os.listdir(path):
            return "the directory is not empty"
    except OSError:
        return None
    if os.path.exists(path) and not os.path.isdir(path):
        return "a file is in the way"
    return None


def os_root_reasons(datasets: List[Dict]) -> List[str]:
    """Why this pool is another system's root, or [] if it is not."""
    reasons = []
    for ds in datasets:
        rel = ds["rel"]
        if rel == shadow.DATASET_NAME:
            continue
        mp = ds.get("mountpoint", "none")
        canmount = ds.get("canmount", "on")
        if mp == "/" and (rel != "" or canmount != "off"):
            reasons.append(f"{rel or '(pool)'} mounts on /")
        elif ds.get("mountpoint_explicit") and canmount != "off" and _system_path(mp):
            reasons.append(f"{rel or '(pool)'} mounts on {mp}")
    return reasons


def mount_conflicts(datasets: List[Dict], mountpoints: Dict[str, str],
                    occupied: Optional[Callable[[str], Optional[str]]] = None) -> List[Dict]:
    occupied = occupied or _occupied
    conflicts = []
    for ds in datasets:
        rel = ds["rel"]
        mp = mountpoints.get(rel, "none")
        if ds.get("canmount", "on") != "on" or mp in UNMOUNTED:
            continue
        if not mp.startswith("/"):
            reason = "not an absolute path"
        elif _system_path(mp):
            reason = "a system directory"
        else:
            reason = occupied(mp)
        if reason:
            conflicts.append({"dataset": rel, "mountpoint": mp, "reason": reason})
    return conflicts


def is_system_path(path: str) -> bool:
    return _system_path(path)


def occupied(path: str) -> Optional[str]:
    """Why a dataset must not be mounted at `path`, or None."""
    return _occupied(path)


def mountpoint_problem(path: str) -> Optional[str]:
    if not path.startswith("/") or path != posixpath.normpath(path):
        return "The mountpoint must be an absolute path, like /mnt/tank"
    if any(c.isspace() or not c.isprintable() for c in path):
        return "The mountpoint may not contain spaces or control characters"
    if _system_path(path):
        return f"{path} is a system directory"
    return None


def encryption_roots(datasets: List[Dict]) -> List[Dict]:
    roots = []
    for ds in datasets:
        if ds.get("encryption", "off") in ("off", "-"):
            continue
        root = ds.get("encryptionroot", "-")
        if root != "-" and _rel_of(root) == ds["rel"]:
            roots.append({
                "dataset": ds["rel"],
                "keyformat": ds.get("keyformat", ""),
                "unlocked": ds.get("keystatus") == "available",
            })
    return roots


def _rel_of(name: str) -> str:
    return name.split("/", 1)[1] if "/" in name else ""


def _full(pool: str, rel: str) -> str:
    return f"{pool}/{rel}" if rel else pool


def _load_keys(engine, pool: str, roots: List[Dict], passphrases: Dict[str, str],
               keyslot_unlock: Optional[Dict] = None) -> Dict[str, Dict]:
    """
    Unlock what can be unlocked. Each root gets `error` and, when the error
    must stop an import, `blocking`. Returns the keyslots that opened a root,
    by root.
    """
    opened: Dict[str, Dict] = {}
    for root in roots:
        root.pop("error", None)
        root.pop("blocking", None)
        full = _full(pool, root["dataset"])
        if root["keyformat"] != "passphrase":
            slots = keymgmt.pool_keyslots(engine, full) if root["keyformat"] else None
            root["unlock_methods"] = _unlock_methods(slots)
            if root["unlocked"]:
                continue
            if not slots:
                root["error"] = (f"its key is {root['keyformat'] or 'not a passphrase'}: "
                                 "it cannot be unlocked here and will stay locked")
                continue
            raw_key, problem = _open_keyslot(engine, slots, keyslot_unlock)
            if raw_key is None:
                root["error"], root["blocking"] = problem, True
                continue
            try:
                keymgmt.load_raw_key(engine, full, raw_key)
            except (CommandExecutionError, CommandTranslationError) as exc:
                root["error"], root["blocking"] = f"the key did not load: {exc}", True
                continue
            root["unlocked"] = True
            opened[root["dataset"]] = slots
            continue
        if root["unlocked"]:
            continue
        passphrase = passphrases.get(root["dataset"], "")
        if not passphrase:
            root["error"], root["blocking"] = "passphrase required", True
            continue
        if any(c in passphrase for c in "\n\r\0"):
            root["error"], root["blocking"] = "the passphrase may not contain line breaks", True
            continue
        try:
            engine.execute_with_stdin("zfs", "load_key_prompt", passphrase + "\n", dataset=full)
            root["unlocked"] = True
        except (CommandExecutionError, CommandTranslationError) as exc:
            logger.info("Wrong key for %s: %s", root["dataset"], exc)
            root["error"], root["blocking"] = "wrong passphrase", True
    return opened


def _unlock_methods(slots: Optional[Dict]) -> List[str]:
    if not slots:
        return []
    methods = []
    if "usb" in slots["slots"]:
        methods += ["usb", "file"]
    if "passphrase" in slots["slots"]:
        methods.append("passphrase")
    return methods


def _open_keyslot(engine, slots: Dict, unlock: Optional[Dict]) -> Tuple[Optional[bytes], str]:
    """The raw key from one of the pool's keyslots, or None and why not."""
    method = (unlock or {}).get("method")
    usb_slot = slots["slots"].get("usb")
    passphrase_slot = slots["slots"].get("passphrase")
    if method == "usb" and usb_slot:
        raw_key = keymgmt.PoolUnlocker(engine).find_usb_key(usb_slot)
        return (raw_key, "") if raw_key else (None, "no USB key plugged into this system opens it")
    if method == "file" and usb_slot:
        try:
            secret = base64.b64decode(unlock.get("key") or "", validate=True)
        except (binascii.Error, ValueError):
            return None, "the freedomnas.key file could not be read"
        try:
            return keymgmt.unwrap_key(usb_slot, secret), ""
        except keymgmt.SlotUnavailableError:
            return None, "this freedomnas.key does not open it"
    if method == "passphrase" and passphrase_slot:
        try:
            return keymgmt.unwrap_key(passphrase_slot, (unlock.get("passphrase") or "").encode("utf-8")), ""
        except keymgmt.SlotUnavailableError:
            return None, "wrong Master Passphrase"
    return None, "unlock it with its USB key or its Master Passphrase"


def _tree(engine, pool: str) -> List[Dict]:
    stdout, _ = engine.execute("zfs", "dataset_tree", pool_name=pool, timeout=60)
    return parse_tree(stdout, pool)


def _read_config(engine, pool: str) -> Tuple[Optional[Dict], Optional[str]]:
    dataset = f"{pool}/{shadow.DATASET_NAME}"
    mountpoint = tempfile.mkdtemp(prefix="freedomnas-import.")
    try:
        engine.execute("zfs", "mount_dataset_at", dataset=dataset, mountpoint=mountpoint)
    except (CommandExecutionError, CommandTranslationError) as exc:
        Path(mountpoint).rmdir()
        return None, f"could not mount {shadow.DATASET_NAME}: {exc}"
    try:
        with open(Path(mountpoint) / shadow.CONFIG_NAME) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return None, "its config.yaml is not a mapping"
        return data, None
    except FileNotFoundError:
        return None, None
    except (OSError, yaml.YAMLError) as exc:
        return None, f"could not read its config.yaml: {exc}"
    finally:
        engine.execute("zfs", "unmount_at", mountpoint=mountpoint, check=False)
        try:
            Path(mountpoint).rmdir()
        except OSError:
            pass


def _translate(path: str, old: Dict[str, str], new: Dict[str, str]) -> Optional[str]:
    best = None
    for rel, mp in old.items():
        if mp in UNMOUNTED or not mp.startswith("/"):
            continue
        if _under(path, mp) and (best is None or len(mp) > len(old[best])):
            best = rel
    if best is None or new.get(best, "none") in UNMOUNTED:
        return None
    rest = path[len(old[best]):]
    return new[best] + rest


def _acl_users(acl: str) -> List[str]:
    users = []
    for entry in (acl or "").split(","):
        name = entry.rsplit(":", 1)[0].strip()
        if name and name.lower() != "everyone" and name not in users:
            users.append(name)
    return users


def foreign_shares(config: Dict, old: Dict[str, str], new: Dict[str, str]) -> Dict[str, List[Dict]]:
    shares = config.get("shares") or {}
    result: Dict[str, List[Dict]] = {"smb": [], "nfs": []}
    for kind in ("smb", "nfs"):
        for share in shares.get(kind) or []:
            if not isinstance(share, dict) or not share.get("name") or not share.get("path"):
                continue
            path = _translate(str(share["path"]), old, new)
            if path is None:
                continue
            entry = dict(share)
            entry["path"] = path
            entry["original_path"] = share["path"]
            if kind == "smb":
                wanted = _acl_users(share.get("acl", ""))
                for user in share.get("valid_users") or []:
                    if user not in wanted:
                        wanted.append(user)
                entry["users"] = wanted
            result[kind].append(entry)
    return result


def foreign_users(config: Dict) -> List[Dict]:
    users = []
    for user in config.get("users") or []:
        if isinstance(user, dict) and user.get("username"):
            users.append({
                "username": user["username"],
                "uid": user.get("uid"),
                "groups": list(user.get("groups") or []),
                "shell": user.get("shell") or "/bin/bash",
                "admin": any(g in ADMIN_GROUPS for g in user.get("groups") or []),
            })
    return users


def _find(engine, pool_id: str) -> Dict:
    for pool in list_importable(engine):
        if pool["id"] == str(pool_id):
            if not pool["importable"]:
                raise ImportProblem(f"{pool['name']} is {pool['state'] or 'not importable'}: {pool['status']}")
            return pool
    raise ImportProblem(f"No importable pool has the id {pool_id}")


def _survey(engine, pool: str, final_name: str, root_mountpoint: Optional[str],
            passphrases: Dict[str, str], occupied, keyslot_unlock: Optional[Dict] = None) -> Dict:
    datasets = _tree(engine, pool)
    roots = encryption_roots(datasets)
    _load_keys(engine, pool, roots, passphrases, keyslot_unlock)
    if any(r["unlocked"] for r in roots):
        datasets = _tree(engine, pool)
    projected = project_mountpoints(datasets, final_name, root_mountpoint)
    root = datasets[0] if datasets else {}
    return {
        "datasets": [{
            "dataset": ds["rel"],
            "mountpoint": projected.get(ds["rel"], "none"),
            "canmount": ds.get("canmount", "on"),
            "used": int(ds["used"]) if str(ds.get("used", "")).isdigit() else None,
            "encrypted": ds.get("encryption", "off") not in ("off", "-"),
            "locked": ds.get("keystatus") == "unavailable",
        } for ds in datasets],
        "refused": os_root_reasons(datasets),
        "conflicts": mount_conflicts(datasets, projected, occupied),
        "encryption_roots": roots,
        "root_mountpoint": projected.get("", "none"),
        "freedomnas": any(ds["rel"] == shadow.DATASET_NAME for ds in datasets),
        "_datasets": datasets,
        "_projected": projected,
    }


def inspect(engine, pool_id: str, name: Optional[str] = None, force: bool = False,
            root_mountpoint: Optional[str] = None, passphrases: Optional[Dict[str, str]] = None,
            occupied: Optional[Callable[[str], Optional[str]]] = None,
            keyslot_unlock: Optional[Dict] = None) -> Dict:
    """
    Look at an importable pool without changing it. Raises ImportProblem
    when it cannot even be looked at.
    """
    found = _find(engine, pool_id)
    if found["foreign"] and not force:
        raise ImportProblem(
            f"{found['name']} was last used by another system: confirm that it is not "
            "in use there any more to import it")
    final_name = name or found["name"]
    if root_mountpoint:
        problem = mountpoint_problem(root_mountpoint)
        if problem:
            raise ImportProblem(problem)
    temp = f"fnasimport{os.getpid()}"
    try:
        engine.execute("zfs", "pool_import_inspect", force="-f" if force else "",
                       pool_id=str(pool_id), pool_name=temp, timeout=120)
    except (CommandExecutionError, CommandTranslationError) as exc:
        raise ImportProblem(f"Could not open {found['name']}: {exc}")
    try:
        report = _survey(engine, temp, final_name, root_mountpoint, passphrases or {}, occupied,
                         keyslot_unlock)
        original = project_mountpoints(report["_datasets"], found["name"])
        report["current_root_mountpoint"] = original.get("", "none")
        config, config_error = (None, None)
        state = next((d for d in report["datasets"] if d["dataset"] == shadow.DATASET_NAME), None)
        report["config_locked"] = bool(state and state["locked"])
        if report["freedomnas"] and not report["config_locked"]:
            config, config_error = _read_config(engine, temp)
        report["config_error"] = config_error
        if config is not None:
            report["shares"] = foreign_shares(config, original, report["_projected"])
            report["users"] = foreign_users(config)
        else:
            report["shares"] = {"smb": [], "nfs": []}
            report["users"] = []
    finally:
        engine.execute("zfs", "pool_export", pool_name=temp, check=False, timeout=120)
    report["pool"] = {**found, "final_name": final_name}
    for key in ("_datasets", "_projected"):
        report.pop(key, None)
    return report


def import_pool(engine, pool_id: str, name: str, force: bool = False,
                root_mountpoint: Optional[str] = None,
                passphrases: Optional[Dict[str, str]] = None,
                keyslot_unlock: Optional[Dict] = None) -> Dict:
    """
    Import the pool for good under `name`, unlock it, move its mountpoint if
    asked and mount it. On any failure the pool is exported again and
    ImportProblem raised.
    """
    try:
        engine.execute("zfs", "pool_import_id", force="-f" if force else "",
                       pool_id=str(pool_id), pool_name=name, timeout=300)
    except (CommandExecutionError, CommandTranslationError) as exc:
        raise ImportProblem(f"Could not import the pool: {exc}")
    moved_from = None
    try:
        datasets = _tree(engine, name)
        refused = os_root_reasons(datasets)
        if refused:
            raise ImportProblem("It holds another system's root: " + "; ".join(refused))
        roots = encryption_roots(datasets)
        opened = _load_keys(engine, name, roots, passphrases or {}, keyslot_unlock)
        wrong = [r for r in roots if r.get("blocking")]
        if wrong:
            raise ImportProblem(f"{wrong[0]['dataset'] or name}: {wrong[0]['error']}")
        current = project_mountpoints(datasets, name).get("", "none")
        if root_mountpoint and root_mountpoint != current:
            root = datasets[0]
            moved_from = root.get("mountpoint") if root.get("mountpoint_explicit") else ""
            engine.execute("zfs", "dataset_set", property="mountpoint",
                           value=root_mountpoint, dataset=name)
        engine.execute("zfs", "mount_pool_auto", pool_name=name, timeout=300)
        kept = _keep_foreign_config(engine, name, datasets)
    except (CommandExecutionError, CommandTranslationError, ImportProblem) as exc:
        if moved_from is not None:
            _restore_mountpoint(engine, name, moved_from)
        engine.execute("zfs", "pool_export", pool_name=name, check=False, timeout=120)
        if isinstance(exc, ImportProblem):
            raise
        raise ImportProblem(f"Could not bring {name} in: {exc}")
    adopted = False
    if "" in opened:
        try:
            adopted = keymgmt.adopt_keyslots(name, opened[""])
        except OSError as exc:
            logger.warning("Could not keep the keyslots of %s here: %s", name, exc)
    return {"pool": name, "encryption_roots": roots, "kept_config": kept,
            "adopted_keyslots": adopted}


def _restore_mountpoint(engine, pool: str, was: str) -> None:
    try:
        if was:
            engine.execute("zfs", "dataset_set", property="mountpoint", value=was, dataset=pool)
        else:
            engine.execute("zfs", "dataset_inherit", property="mountpoint", dataset=pool)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.error("Could not give %s its mountpoint back: %s", pool, exc)


def _keep_foreign_config(engine, pool: str, datasets: List[Dict]) -> Optional[str]:
    """
    Keep the imported pool's own configuration beside the copy this system
    is about to mirror over it, so what the other system knew is not lost.
    """
    if not any(ds["rel"] == shadow.DATASET_NAME for ds in datasets):
        return None
    dataset = f"{pool}/{shadow.DATASET_NAME}"
    mountpoint = tempfile.mkdtemp(prefix="freedomnas-import.")
    try:
        engine.execute("zfs", "mount_dataset_at", dataset=dataset, mountpoint=mountpoint)
    except (CommandExecutionError, CommandTranslationError) as exc:
        logger.warning("Could not keep the configuration of %s: %s", dataset, exc)
        Path(mountpoint).rmdir()
        return None
    try:
        source = Path(mountpoint) / shadow.CONFIG_NAME
        if not source.is_file():
            return None
        target = Path(mountpoint) / f"{shadow.CONFIG_NAME}.imported"
        shutil.copy2(source, target)
        return f"{dataset}:{target.name}"
    except OSError as exc:
        logger.warning("Could not keep the configuration of %s: %s", dataset, exc)
        return None
    finally:
        engine.execute("zfs", "unmount_at", mountpoint=mountpoint, check=False)
        try:
            Path(mountpoint).rmdir()
        except OSError:
            pass
