"""
Shares used by a group of accounts.

A group share is a dataset's own directory that belongs to a Unix group, not
to one account: root owns it, the group may read and write everything in it,
and what anyone creates there stays the group's (`directories.acl_group_set`,
an ACL every new file and directory inherits). Samba lets in the members of
the group (`Unix Group\\<group>` in the usershare ACL), so who belongs to the
group is who uses the share, and changing that needs no change to the share.

Membership is recorded on the users, in their `groups`, like any other group
FreedomNAS gives: a restore recreates the group first and then the accounts in
it. A share marked `everyone` takes in every account created afterwards too.

An app that serves the files (Jellyfin reads them, Nextcloud writes them) is
given its own entry in the same ACL, by its account's name or number, so it
needs to be in no group.
"""

import re
from typing import Dict, List, Optional, Set

try:
    from freedomnas import datasets, usershares
except ImportError:  # running from the repository, not from the package
    import datasets  # type: ignore
    import usershares  # type: ignore

GROUP_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")


class GroupShareProblem(Exception):
    """A request that cannot be carried out; the message is for the administrator."""


def share_groups(config) -> Set[str]:
    return {s["group"] for s in config.get_smb_shares() if s.get("group")}


def everyone_groups(config) -> List[str]:
    """The groups a new account joins: those of the shares marked `everyone`."""
    return sorted({s["group"] for s in config.get_smb_shares() if s.get("group") and s.get("everyone")})


def members_of(config, group: str) -> List[str]:
    return [u["username"] for u in config.get_users()
            if u.get("username") and group in (u.get("groups") or [])]


def group_exists(engine, group: str) -> bool:
    stdout, _ = engine.execute("users", "group_exists", group=group, check=False)
    return bool(stdout.strip())


def check_new_group(engine, config, group: str) -> None:
    """A group a new share may use: one of FreedomNAS's shares, or a name nobody has."""
    if not GROUP_NAME.match(group):
        raise GroupShareProblem(
            "The group name takes lowercase letters, digits, - and _, starts with a letter "
            "and is at most 31 characters long")
    if group in share_groups(config):
        return
    if config.get_user(group) is not None or group_exists(engine, group):
        raise GroupShareProblem(f"There is already a group or an account named {group} on this system: "
                                "choose another name")


def ensure_group(engine, group: str) -> None:
    if not group_exists(engine, group):
        engine.execute("users", "group_create", group=group)


def check_accounts(config, names) -> None:
    unknown = sorted(set(names) - {u.get("username") for u in config.get_users()})
    if unknown:
        raise GroupShareProblem(f"There is no account named {', '.join(unknown)}")


def set_members(engine, config, group: str, wanted) -> List[str]:
    """Make exactly the accounts in `wanted` members of `group`. Returns them."""
    check_accounts(config, wanted)
    users = {u["username"]: u for u in config.get_users() if u.get("username")}
    wanted = set(wanted)
    for name in sorted(users):
        user = users[name]
        groups = list(user.get("groups") or [])
        if name in wanted and group not in groups:
            engine.execute("users", "add_to_group", group=group, username=name)
            config.add_user({**user, "groups": groups + [group]})
        elif name not in wanted and group in groups:
            engine.execute("users", "remove_from_group", group=group, username=name)
            config.add_user({**user, "groups": [g for g in groups if g != group]})
    for share in [s for s in config.get_smb_shares() if s.get("group") == group and s.get("apps")]:
        _apply_apps(engine, config, share["name"])
    return sorted(wanted)


def give_to_group(engine, path: str, group: str) -> str:
    """
    Make the directory `path` mounts a dataset on, and everything in it, the
    group's. Returns the dataset.
    """
    name = datasets.dataset_mounted_at(engine, path)
    if not name:
        raise datasets.DatasetProblem(
            f"{path} is not where a dataset of a data pool is mounted: a group share needs a dataset of its own")
    for prop, value in engine.get_profile_value("directories", "group_share_properties").items():
        engine.execute("zfs", "dataset_set", property=prop, value=value, dataset=name)
    engine.execute("directories", "owner_set", owner="root", path=path)
    engine.execute("directories", "group_set_recursive", group=group, path=path, timeout=3600)
    engine.execute("directories", "mode_set",
                   mode=engine.get_profile_value("directories", "group_share_mode"), path=path)
    engine.execute("directories", "acl_group_set", group=group, path=path, timeout=3600)
    return name


def take_back(engine, path: str, group: str) -> None:
    """
    The directory of a share that is gone: root's alone. Its files leave the
    group too, which is about to be deleted and whose number another group
    may get later.
    """
    engine.execute("directories", "acl_group_remove", group=group, path=path, timeout=3600, check=False)
    engine.execute("directories", "group_set_recursive", group=datasets.owner_group(engine, "root"),
                   path=path, timeout=3600, check=False)
    engine.execute("directories", "mode_set", mode=datasets.OWNER_MODE, path=path, check=False)


def drop_group(engine, config, group: str) -> None:
    """A group no share uses any more leaves the accounts and the system."""
    for user in config.get_users():
        if group in (user.get("groups") or []):
            config.add_user({**user, "groups": [g for g in user["groups"] if g != group]})
    engine.execute("users", "group_delete", group=group, check=False)


def create(engine, config, name: str, path: str, group: str, members, everyone: bool = False,
           read_only: bool = False, comment: Optional[str] = None) -> List[str]:
    """
    Make the SMB share `name` of `path` for `group`, with `members` in it
    (every account when `everyone`), and record it. Returns the members.
    """
    wanted = [u["username"] for u in config.get_users() if u.get("username")] if everyone else list(members)
    check_new_group(engine, config, group)
    check_accounts(config, wanted)
    ensure_group(engine, group)
    give_to_group(engine, path, group)
    joined = set_members(engine, config, group, set(wanted) | set(members_of(config, group)))
    usershares.group_settings(engine)
    acl = f"Unix Group\\{group}:{'R' if read_only else 'F'}"
    engine.execute("samba", "create_share", share_name=name, path=path, comment=comment or name, acl=acl)
    engine.execute("samba", "reload_config")
    config.add_smb_share({"name": name, "path": path, "comment": comment or name, "acl": acl,
                          "read_only": read_only, "group": group, "everyone": everyone, "guest_ok": False})
    return joined


def forget(engine, config, share: Dict) -> None:
    """After a group share left the configuration: its directory and, unused, its group."""
    group = share.get("group")
    if not group:
        return
    take_back(engine, share["path"], group)
    if group not in share_groups(config):
        drop_group(engine, config, group)


def grant(engine, path: str, account: str, write: bool) -> None:
    engine.execute("directories", "acl_user_write" if write else "acl_user_read",
                   user=account, path=path, timeout=3600)


def revoke(engine, path: str, account: str, write: bool) -> None:
    engine.execute("directories", "acl_user_write_remove" if write else "acl_user_read_remove",
                   user=account, path=path, timeout=3600, check=False)


def share_at(config, path: str) -> Optional[Dict]:
    return next((s for s in config.get_smb_shares() if s.get("group") and s.get("path") == path), None)


def add_app(engine, config, path: str, app: str, account: str, write: bool) -> bool:
    """
    Let the app `app`, running as `account` (a name or a number), read the
    group share of `path`, or write it too, and record it so a restore gives
    it back. False when `path` is no group share.
    """
    share = share_at(config, path)
    if share is None:
        return False
    apps = [a for a in share.get("apps") or [] if a.get("app") != app]
    config.add_smb_share({**share, "apps": apps + [{"app": app, "account": account, "write": write}]})
    _apply_apps(engine, config, share["name"])
    return True


def _member_account(config, group: str, account: str) -> bool:
    return any(group in (u.get("groups") or []) and account in (u.get("username"), str(u.get("uid", "")))
               for u in config.get_users())


def _apply_apps(engine, config, share_name: str) -> None:
    """
    Give each app of the share its ACL entry. An entry for one account
    stands before the group's for that account, so an app running as a
    member of the group (Jellyfin as the NAS's own user) gets the member's
    read and write: a read-only entry would take writing away from the
    person too. The app's mount stays read only where the recipe says so.
    What was granted is recorded ("granted"), for a restore and a removal.
    """
    share = next(s for s in config.get_smb_shares() if s.get("name") == share_name)
    entries = []
    for entry in share.get("apps") or []:
        write = bool(entry.get("write")) or _member_account(config, share["group"], entry["account"])
        if entry.get("granted") == "write" and not write:
            revoke(engine, share["path"], entry["account"], True)
        grant(engine, share["path"], entry["account"], write)
        entries.append({**entry, "granted": "write" if write else "read"})
    config.add_smb_share({**share, "apps": entries})


def _granted_write(entry: Dict) -> bool:
    return entry.get("granted", "write" if entry.get("write") else "read") == "write"


def remove_app(engine, config, app: str) -> None:
    """The app is gone: its entries leave the shares' ACLs, unless another app shares them."""
    for share in list(config.get_smb_shares()):
        entries = share.get("apps") or []
        mine = [a for a in entries if a.get("app") == app]
        if not mine:
            continue
        rest = [a for a in entries if a.get("app") != app]
        for entry in mine:
            if not any((a["account"], _granted_write(a)) == (entry["account"], _granted_write(entry))
                       for a in rest):
                revoke(engine, share["path"], entry["account"], _granted_write(entry))
        config.add_smb_share({**share, "apps": rest})


def restore(engine, share: Dict) -> Optional[str]:
    """
    Bring a recorded group share's directory back after a restore: the
    group is created here with a number of its own, so a directory whose
    group is not that name (it shows the old number), or that lost the
    group's ACL entry (another edition's kind of ACL: FreeBSD's NFSv4 ones
    mean nothing to Linux), is given to it again, with its apps. The old number's entries leave the ACL: whatever group
    gets that number later must not find the files open to it. Returns
    what was done, or None when it already matched.
    """
    group, path = share["group"], share["path"]
    ensure_group(engine, group)
    stdout, _ = engine.execute("directories", "group_get", path=path, check=False)
    old = stdout.strip()
    acl, _ = engine.execute("directories", "acl_get", path=path, check=False)
    if old == group and f"group:{group}:" in acl:
        return None
    if old and old != group:
        engine.execute("directories", "acl_group_remove", group=old, path=path, timeout=3600, check=False)
    give_to_group(engine, path, group)
    for entry in share.get("apps") or []:
        grant(engine, path, entry["account"], _granted_write(entry))
    return f"{path} given to the group {group}"
