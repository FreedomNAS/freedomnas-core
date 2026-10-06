"""
Delegated ZFS permissions (`zfs allow` / `zfs unallow`): which users and
groups may run which zfs subcommands on a dataset without being root.

Who and which permissions reach the profile's command as multi-args, where
the engine does not refuse a leading '-', so everything is checked here.
"""

import re
from typing import Dict, List

_PERM = re.compile(r"^@?[a-z][a-z0-9_:-]*$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.$@-]*$")
_SET = re.compile(r"^@[a-z][a-z0-9_]*$")
SCOPES = {"both": "", "local": "-l", "descendent": "-d"}
SECTION_SCOPE = {"Local permissions": "local", "Descendent permissions": "descendent",
                 "Local+Descendent permissions": "both"}


class DelegationProblem(Exception):
    pass


def perm_list(perms) -> str:
    if isinstance(perms, str):
        perms = [p.strip() for p in perms.split(",")]
    perms = [str(p).strip() for p in perms or [] if str(p).strip()]
    for perm in perms:
        if not _PERM.match(perm):
            raise DelegationProblem(f"{perm!r} is not a permission, property or @set")
    return ",".join(perms)


def _names(names) -> str:
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",")]
    names = [str(n).strip() for n in names or [] if str(n).strip()]
    if not names:
        raise DelegationProblem("Name at least one user or group")
    for name in names:
        if not _PRINCIPAL.match(name) or len(name) > 64:
            raise DelegationProblem(f"{name!r} is not a user or group name")
    return ",".join(names)


def arguments(request: Dict, revoking: bool = False) -> Dict[str, str]:
    """The allow_scope, allow_who and perms words for zfs.allow / zfs.unallow."""
    who = request.get("who") or "user"
    scope = request.get("scope") or "both"
    if scope not in SCOPES:
        raise DelegationProblem("scope is both, local or descendent")
    if who in ("user", "group"):
        who_words = f"{'-u' if who == 'user' else '-g'} {_names(request.get('names'))}"
    elif who == "everyone":
        who_words = "-e"
    elif who == "create":
        who_words, scope = "-c", "both"
    elif who == "set":
        name = str(request.get("set") or "")
        if not _SET.match(name):
            raise DelegationProblem("A permission set is named @name")
        who_words, scope = f"-s {name}", "both"
    else:
        raise DelegationProblem("who is user, group, everyone, create or set")
    perms = perm_list(request.get("perms"))
    if not perms and not revoking:
        raise DelegationProblem("Choose at least one permission")
    return {"allow_scope": SCOPES[scope], "allow_who": who_words, "perms": perms}


def parse(text: str) -> List[Dict]:
    """`zfs allow <dataset>` output, one entry per line under each section."""
    found: List[Dict] = []
    section = ""
    for line in (text or "").splitlines():
        if line.startswith("----"):
            continue
        if not line.startswith(("\t", " ")):
            section = line.strip().rstrip(":")
            continue
        body = line.strip()
        if not body:
            continue
        if section == "Permission sets":
            name, _, perms = body.partition(" ")
            found.append({"section": section, "who": "set", "name": name,
                          "scope": "both", "perms": perms.split(",")})
        elif section == "Create time permissions":
            found.append({"section": section, "who": "create", "name": "",
                          "scope": "both", "perms": body.split(",")})
        else:
            parts = body.split(" ", 2)
            scope = SECTION_SCOPE.get(section, "both")
            if parts[0] == "everyone" and len(parts) >= 2:
                found.append({"section": section, "who": "everyone", "name": "",
                              "scope": scope, "perms": parts[1].split(",")})
            elif len(parts) == 3 and parts[0] in ("user", "group"):
                found.append({"section": section, "who": parts[0], "name": parts[1],
                              "scope": scope, "perms": parts[2].split(",")})
    return found
