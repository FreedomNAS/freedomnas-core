"""FreedomNAS names for physical network cards, by MAC address."""

from pathlib import Path

import pytest

import netnames
from engine import FreedomNASEngine

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")
A, B, C = "52:54:00:00:00:0a", "52:54:00:00:00:0b", "52:54:00:00:00:0c"


def cards(*pairs):
    return [{"name": n, "mac": m, "driver": n} for n, m in pairs]


def test_parse_physical_skips_what_is_not_a_card():
    text = ("eth1 52:54:00:00:00:0B eth1\n"
            "eth0 52:54:00:00:00:0a vtnet0\ndummy 00:00:00:00:00:00 dummy\nbroken\n"
            "fnas_lan2 52:54:00:00:00:0c fnas_lan2\n")
    assert netnames.parse_physical(text) == [
        {"name": "eth0", "mac": A, "driver": "vtnet0"},
        {"name": "eth1", "mac": B, "driver": "eth1"},
        {"name": "fnas_lan2", "mac": C, "driver": "fnas_lan2"}]


def test_new_cards_are_numbered_in_order():
    names, moved = netnames.plan(cards(("enp3s0", A), ("enp4s0", B)), {})
    assert names == {A: "fnas_lan1", B: "fnas_lan2"} and moved == {}


def test_recorded_cards_keep_their_names():
    names, _ = netnames.plan(cards(("eth0", A), ("eth1", B)), {B: "fnas_lan1", A: "fnas_lan2"})
    assert names == {A: "fnas_lan2", B: "fnas_lan1"}


def test_a_replaced_card_takes_over_the_missing_name():
    names, moved = netnames.plan(cards(("eth0", C), ("eth1", B)), {A: "fnas_lan1", B: "fnas_lan2"})
    assert names == {C: "fnas_lan1", B: "fnas_lan2"}
    assert moved == {"fnas_lan1": A}


def test_a_new_card_never_takes_a_name_still_recorded_for_another():
    names, _ = netnames.plan(cards(("eth0", A), ("eth1", C)), {A: "fnas_lan1", B: "fnas_lan2"})
    assert names == {A: "fnas_lan1", C: "fnas_lan2"}
    names, _ = netnames.plan(cards(("eth1", C)), {A: "fnas_lan1", B: "fnas_lan2"})
    assert names == {C: "fnas_lan1"}


class Config:
    def __init__(self, interfaces=(), mapping=None):
        self.network = {"interfaces": [dict(i) for i in interfaces], "interfaces_mapping": dict(mapping or {})}

    def get(self, key, default=None):
        return self.network["interfaces_mapping"] if key == "network.interfaces_mapping" else default

    def set(self, key, value):
        self.network["interfaces_mapping"] = value

    def get_interface(self, name):
        return next((i for i in self.network["interfaces"] if i["logical_name"] == name), None)

    def add_interface(self, entry):
        self.remove_interface(entry["logical_name"])
        self.network["interfaces"].append(entry)
        if entry.get("mac_address"):
            self.network["interfaces_mapping"][entry["mac_address"]] = entry["logical_name"]

    def remove_interface(self, name):
        self.network["interfaces"] = [i for i in self.network["interfaces"] if i["logical_name"] != name]
        self.network["interfaces_mapping"] = {m: n for m, n in self.network["interfaces_mapping"].items()
                                              if n != name}


class Engine:
    def __init__(self, physical):
        self.physical, self.calls = physical, []

    def execute(self, category, action, **kwargs):
        self.calls.append((action, kwargs))
        return (self.physical if action == "list_physical" else ""), ""


def test_apply_renames_persists_and_moves_the_configuration():
    static = {"method": "static", "ip": "192.168.1.5", "cidr": 24, "dns": ["1.1.1.1"]}
    config = Config([{"logical_name": "eth0", "config": static}])
    engine = Engine(f"eth0 {A} eth0\neth1 {B} eth1\n")
    done, renamed = netnames.apply(engine, config)
    assert renamed == {"eth0": "fnas_lan1", "eth1": "fnas_lan2"}
    actions = [(a, k) for a, k in engine.calls if a != "list_physical"]
    assert actions[0] == ("names_clear", {})
    assert actions[1:3] == [("name_persist", {"mac": A, "driver": "eth0", "new_name": "fnas_lan1"}),
                            ("name_persist", {"mac": B, "driver": "eth1", "new_name": "fnas_lan2"})]
    assert ("rename_interface", {"old_name": "eth0", "new_name": "fnas_tmp0"}) in actions
    assert ("rename_interface", {"old_name": "fnas_tmp0", "new_name": "fnas_lan1"}) in actions
    assert ("connection_rename", {"old_name": "eth0", "new_name": "fnas_lan1"}) in actions
    assert config.get_interface("eth0") is None
    assert config.get_interface("fnas_lan1") == {"logical_name": "fnas_lan1", "config": static, "mac_address": A}
    assert config.network["interfaces_mapping"] == {A: "fnas_lan1", B: "fnas_lan2"}
    assert done == [f"eth0 ({A}) is now fnas_lan1", f"eth1 ({B}) is now fnas_lan2"]


def test_apply_on_named_cards_only_rewrites_the_rules():
    config = Config(mapping={A: "fnas_lan1"})
    engine = Engine(f"fnas_lan1 {A} fnas_lan1\n")
    assert netnames.apply(engine, config) == ([], {})
    assert [a for a, _ in engine.calls] == ["list_physical", "names_clear", "name_persist"]


def test_apply_swaps_names_through_temporary_ones():
    config = Config(mapping={A: "fnas_lan2", B: "fnas_lan1"})
    engine = Engine(f"fnas_lan1 {A} x\nfnas_lan2 {B} y\n")
    netnames.apply(engine, config)
    renames = [(k["old_name"], k["new_name"]) for a, k in engine.calls if a == "rename_interface"]
    assert renames == [("fnas_lan1", "fnas_tmp0"), ("fnas_lan2", "fnas_tmp1"),
                       ("fnas_tmp0", "fnas_lan2"), ("fnas_tmp1", "fnas_lan1")]


@pytest.mark.parametrize("edition", ["debian", "freebsd", "arch", "artix", "void", "gentoo"])
def test_every_edition_has_the_naming_commands(edition):
    engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
    assert engine.get_command("network", "list_physical")
    assert engine.get_command("network", "names_clear")
    assert "fnas_lan1" in str(engine.get_command("network", "name_persist", mac=A, driver="vtnet0",
                                                 new_name="fnas_lan1"))
    assert engine.get_command("network", "rename_interface", old_name="eth0", new_name="fnas_lan1")
    assert engine.get_command("network", "connection_rename", old_name="eth0", new_name="fnas_lan1")


def test_only_card_names_count_in_the_mapping():
    names, _ = netnames.plan(cards(("fnas_bridge1", B), ("fnas_lan1", A)), {A: "fnas_lan1", B: "fnas_bridge1"})
    assert names == {A: "fnas_lan1", B: "fnas_lan2"}
