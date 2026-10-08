"""
FreedomNAS names for the physical network interfaces: fnas_lan1, fnas_lan2…,
the same on every edition, tied to each card's MAC address.

network.interfaces_mapping in config.yaml keeps MAC → name. A card whose MAC
is recorded gets its name back; when a recorded MAC is gone (a restore on
new hardware, a replaced card) its name, and whatever was configured under
it, goes to a card no name was recorded for, in the order the system lists
them. Cards still left over get the next free numbers.
"""

import re
from typing import Dict, List, Tuple

PREFIX = "fnas_lan"
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_NUMBER = re.compile(r"(\d+)")


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in _NUMBER.split(name)]


def parse_physical(text: str) -> List[Dict[str, str]]:
    """`network.list_physical`: one card per line, "name mac driver"."""
    cards = []
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        mac = fields[1].lower()
        if not _MAC.match(mac) or mac == "00:00:00:00:00:00":
            continue
        name = fields[0]
        cards.append({"name": name, "mac": mac, "driver": fields[2] if len(fields) > 2 else name})
    return sorted(cards, key=lambda c: _natural(c["name"]))


def plan(cards: List[Dict[str, str]], mapping: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    MAC → name for every card present, and the names that moved to another
    card: name → the MAC it had before.
    """
    mapping = {mac.lower(): name for mac, name in (mapping or {}).items() if name.startswith(PREFIX)}
    present = [c["mac"] for c in cards]
    names: Dict[str, str] = {}
    for mac in present:
        if mac in mapping:
            names[mac] = mapping[mac]
    orphaned = sorted((name for mac, name in mapping.items() if mac not in present), key=_natural)
    moved: Dict[str, str] = {}
    for mac in present:
        if mac in names or not orphaned:
            continue
        name = orphaned.pop(0)
        names[mac] = name
        moved[name] = next(m for m, n in mapping.items() if n == name)
    taken = set(names.values()) | set(orphaned)
    number = 1
    for mac in present:
        if mac in names:
            continue
        while f"{PREFIX}{number}" in taken:
            number += 1
        names[mac] = f"{PREFIX}{number}"
        taken.add(names[mac])
    return names, moved


def apply(engine, config) -> Tuple[List[str], Dict[str, str]]:
    """
    Name every physical card, now and at every boot, and carry each card's
    configuration over to its new name. Returns what was done, for the log,
    and old name → new name for every card renamed.
    """
    stdout, _ = engine.execute("network", "list_physical")
    cards = parse_physical(stdout)
    if not cards:
        return [], {}
    mapping = dict(config.get("network.interfaces_mapping") or {})
    names, moved = plan(cards, mapping)
    done = [f"{name} moved to the card {mac} (it was on {old})"
            for name, old in moved.items() for mac, n in names.items() if n == name]

    engine.execute("network", "names_clear")
    for card in cards:
        engine.execute("network", "name_persist", mac=card["mac"], driver=card["driver"],
                       new_name=names[card["mac"]])

    renaming = [c for c in cards if c["name"] != names[c["mac"]]]
    for number, card in enumerate(renaming):
        engine.execute("network", "rename_interface", old_name=card["name"], new_name=f"fnas_tmp{number}")
    for number, card in enumerate(renaming):
        new = names[card["mac"]]
        engine.execute("network", "rename_interface", old_name=f"fnas_tmp{number}", new_name=new)
        engine.execute("network", "connection_rename", old_name=card["name"], new_name=new)
        entry = config.get_interface(card["name"])
        if entry is not None and config.get_interface(new) is None:
            config.remove_interface(card["name"])
            entry["logical_name"] = new
            config.add_interface(entry)
        done.append(f"{card['name']} ({card['mac']}) is now {new}")

    final = {mac: name for mac, name in mapping.items()
             if mac.lower() not in names and name not in names.values() and name.startswith(PREFIX)}
    final.update(names)
    for card in cards:
        entry = config.get_interface(names[card["mac"]])
        if entry is not None and entry.get("mac_address") != card["mac"]:
            entry["mac_address"] = card["mac"]
            config.add_interface(entry)
    if final != mapping:
        config.set("network.interfaces_mapping", final)
    return done, {c["name"]: names[c["mac"]] for c in renaming}
