"""Interface configuration: checking, the profile steps, and every edition's commands."""

import shlex
from pathlib import Path

import pytest

import netconfig
from engine import FreedomNASEngine
from netconfig import NetConfigProblem

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")
EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]
FULL = {"method": "static", "ip": "192.168.1.50", "cidr": "24", "gateway": "192.168.1.1",
        "dns": "1.1.1.1, 2606:4700::1111", "dns_search": "lan home.arpa",
        "ipv6_method": "static", "ipv6": "fd00::50", "ipv6_prefix": 64, "ipv6_gateway": "fd00::1",
        "mtu": "9000"}


def test_defaults_are_dhcp_and_automatic():
    assert netconfig.normalize({}) == {"method": "dhcp", "dns": [], "dns_search": [],
                                       "ipv6_method": "auto", "mtu": None}


def test_a_full_configuration_is_normalized():
    config = netconfig.normalize(FULL)
    assert config["cidr"] == 24 and config["mtu"] == 9000
    assert config["dns"] == ["1.1.1.1", "2606:4700::1111"]
    assert config["dns_search"] == ["lan", "home.arpa"]


@pytest.mark.parametrize("change, problem", [
    ({"method": "bootp"}, "dhcp or static"),
    ({"ip": "192.168.1.300"}, "IPv4 address"),
    ({"ip": "fd00::5"}, "IPv4 address"),
    ({"cidr": 33}, "1 to 32"),
    ({"gateway": "10.0.0.1"}, "outside"),
    ({"dns": ""}, "DNS server"),
    ({"dns": "one.one"}, "not an IP"),
    ({"dns_search": "bad_domain!"}, "domain name"),
    ({"ipv6_method": "slaac"}, "auto, static or disabled"),
    ({"ipv6": "192.168.1.5"}, "IPv6 address"),
    ({"ipv6_prefix": 129}, "1 to 128"),
    ({"mtu": 100}, "576 to 9216"),
    ({"mtu": "big"}, "not a number"),
])
def test_bad_values_are_refused(change, problem):
    with pytest.raises(NetConfigProblem, match=problem):
        netconfig.normalize(dict(FULL, **change))


def test_ipv6_dns_needs_ipv6():
    with pytest.raises(NetConfigProblem, match="IPv6 DNS"):
        netconfig.normalize({"dns": "2606:4700::1111", "ipv6_method": "disabled"})


@pytest.mark.parametrize("name", ["eth0", "enp3s0", "bond0.10", "fnas_lan"])
def test_interface_names(name):
    assert netconfig.interface_problem(name) is None


@pytest.mark.parametrize("name", ["", "-x", "x;reboot", "a b", "waytoolonginterfacename"])
def test_bad_interface_names(name):
    assert netconfig.interface_problem(name)


def test_steps_for_dhcp():
    actions = [a for a, _ in netconfig.steps("eth0", netconfig.normalize({}))]
    assert actions == ["connection_ensure", "ipv4_dhcp", "ipv6_auto", "dns_auto",
                       "mtu_auto", "connection_activate"]


def test_steps_for_a_full_configuration():
    steps = dict(netconfig.steps("eth0", netconfig.normalize(FULL)))
    assert steps["dns_set"] == {"interface": "eth0", "dns": "1.1.1.1", "dns6": "2606:4700::1111",
                                "dns_search": "lan home.arpa"}
    assert steps["ipv6_static"]["ipv6_prefix"] == 64
    assert steps["mtu_set"]["mtu"] == 9000


def test_a_configuration_saved_by_the_old_static_route_still_applies():
    old = {"method": "static", "ip": "192.168.1.50", "cidr": 24, "gateway": "192.168.1.1",
           "dns": ["1.1.1.1"]}
    actions = [a for a, _ in netconfig.steps("eth0", old)]
    assert actions == ["connection_ensure", "ipv4_static", "ipv6_auto", "dns_set",
                       "mtu_auto", "connection_activate"]


@pytest.mark.parametrize("edition", EDITIONS)
@pytest.mark.parametrize("request_", [{}, FULL, dict(FULL, method="dhcp", ipv6_method="disabled",
                                                       dns="1.1.1.1", mtu=None)])
def test_every_edition_has_every_step(edition, request_):
    engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
    for action, kwargs in netconfig.steps("eth0", netconfig.normalize(request_)):
        assert engine.get_command("network", action, **kwargs)
    for action, kwargs in (("connection_exists", {"interface": "eth0"}),
                           ("checkpoint_create", {"rollback_seconds": 90}),
                           ("checkpoint_destroy", {"checkpoint": "/x/1"}),
                           ("checkpoint_rollback", {"checkpoint": "/x/1"})):
        assert engine.get_command("network", action, **kwargs)


def test_networkmanager_static_step():
    engine = FreedomNASEngine.for_target("void", profile_dir=PROFILES_DIR)
    command = engine.get_command("network", "ipv4_static", interface="eth0", ip="192.168.1.50",
                                 cidr=24, gateway="")
    assert shlex.split(command) == [
        "nmcli", "connection", "modify", "fnas-eth0", "ipv4.method", "manual",
        "ipv4.addresses", "192.168.1.50/24", "ipv4.gateway", ""]


def test_freebsd_dns_step_keeps_both_families_in_one_value():
    engine = FreedomNASEngine.for_target("freebsd", profile_dir=PROFILES_DIR)
    command = engine.get_command("network", "dns_set", interface="em0", dns="1.1.1.1 9.9.9.9",
                                 dns6="2606:4700::1111", dns_search="lan")
    assert shlex.split(command) == [
        "sysrc", "-f", "/etc/resolvconf.conf",
        "name_servers=1.1.1.1 9.9.9.9 2606:4700::1111", "search_domains=lan"]


class FakeEngine:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def execute(self, category, action, timeout=30, check=True, **kwargs):
        self.calls.append((action, kwargs))
        return self.outputs.get(action, ""), ""


def test_checkpoint_create_returns_what_the_profile_prints():
    engine = FakeEngine({"checkpoint_create": "/org/freedesktop/NetworkManager/Checkpoint/3\n"})
    assert netconfig.checkpoint_create(engine, 90) == "/org/freedesktop/NetworkManager/Checkpoint/3"
    assert engine.calls == [("checkpoint_create", {"rollback_seconds": 90})]


def test_no_checkpoint_is_a_problem():
    with pytest.raises(NetConfigProblem):
        netconfig.checkpoint_create(FakeEngine(), 90)


def test_is_configured():
    assert netconfig.is_configured(FakeEngine({"connection_exists": "yes\n"}), "eth0")
    assert not netconfig.is_configured(FakeEngine(), "eth0")


PRESENT = {"fnas_lan1": "52:54:00:00:00:0a", "fnas_lan2": "52:54:00:00:00:0b", "fnas_lan3": "52:54:00:00:00:0c"}


def test_a_bridge_takes_the_mac_of_its_first_port():
    entry = netconfig.normalize_virtual({"type": "bridge", "ports": "fnas_lan2, fnas_lan1"}, [], PRESENT)
    assert entry == {"type": "bridge", "ports": ["fnas_lan2", "fnas_lan1"], "mac_address": PRESENT["fnas_lan2"],
                     "stp": False, "logical_name": "fnas_bridge1", "config": netconfig.normalize({})}


def test_names_count_up_past_the_ones_taken():
    taken = [{"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan3"]},
             {"logical_name": "fnas_lan3", "master": "fnas_bridge1"}]
    entry = netconfig.normalize_virtual({"type": "bridge", "ports": ["fnas_lan1"]}, taken, PRESENT)
    assert entry["logical_name"] == "fnas_bridge2"
    bond = netconfig.normalize_virtual({"type": "bond", "ports": ["fnas_lan1", "fnas_lan2"], "mode": "failover"},
                                       taken, PRESENT)
    assert (bond["logical_name"], bond["mode"]) == ("fnas_bond1", "failover")
    vlan = netconfig.normalize_virtual({"type": "vlan", "parent": "fnas_lan1", "vlan_id": "10"}, taken, PRESENT)
    assert (vlan["logical_name"], vlan["parent"], vlan["vlan_id"]) == ("fnas_vlan10", "fnas_lan1", 10)
    assert "mac_address" not in vlan


@pytest.mark.parametrize("request_, problem", [
    ({"type": "tunnel"}, "bridge, bond or vlan"),
    ({"type": "bridge", "ports": []}, "at least one port"),
    ({"type": "bridge", "ports": ["fnas_lan9"]}, "No interface"),
    ({"type": "bridge", "ports": ["fnas_lan3"]}, "port of fnas_bridge1"),
    ({"type": "bridge", "ports": ["fnas_lan1", "fnas_lan1"]}, "twice"),
    ({"type": "bridge", "ports": ["fnas_bridge1"]}, "network cards"),
    ({"type": "bond", "ports": ["fnas_lan1"]}, "at least two"),
    ({"type": "bond", "ports": ["fnas_lan1", "fnas_lan2"], "mode": "rr"}, "lacp or failover"),
    ({"type": "vlan", "parent": "fnas_lan1", "vlan_id": 5000}, "1 to 4094"),
    ({"type": "vlan", "parent": "fnas_lan1", "vlan_id": "x"}, "number"),
    ({"type": "vlan", "parent": "fnas_lan3", "vlan_id": 5}, "port of"),
    ({"type": "vlan", "parent": "fnas_lan1", "vlan_id": 7}, "exists already"),
])
def test_bad_virtual_requests(request_, problem):
    taken = [{"logical_name": "fnas_bridge1", "type": "bridge", "ports": ["fnas_lan3"]},
             {"logical_name": "fnas_lan3", "master": "fnas_bridge1"},
             {"logical_name": "fnas_vlan7", "type": "vlan", "parent": "fnas_lan2", "vlan_id": 7}]
    present = dict(PRESENT, fnas_bridge1="52:54:00:00:00:0c")
    with pytest.raises(NetConfigProblem, match=problem):
        netconfig.normalize_virtual(request_, taken, present)


def test_bridge_steps_create_address_attach_and_activate():
    entry = netconfig.normalize_virtual({"type": "bridge", "ports": ["fnas_lan1", "fnas_lan2"], "stp": True,
                                         "config": {"method": "static", "ip": "10.0.0.5", "cidr": 24,
                                                    "dns": "10.0.0.1"}}, [], PRESENT)
    actions = [a for a, _ in netconfig.virtual_steps(entry)]
    assert actions == ["bridge_create", "connection_ensure", "ipv4_static", "ipv6_auto", "dns_set", "mtu_auto",
                       "bridge_stp_on", "bridge_port_add", "bridge_port_add", "mac_set",
                       "port_activate", "port_activate", "virtual_activate"]


@pytest.mark.parametrize("edition", EDITIONS)
def test_every_edition_has_the_virtual_steps(edition):
    engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
    entries = [
        netconfig.normalize_virtual({"type": "bridge", "ports": ["fnas_lan1"]}, [], PRESENT),
        netconfig.normalize_virtual({"type": "bond", "ports": ["fnas_lan1", "fnas_lan2"]}, [], PRESENT),
        netconfig.normalize_virtual({"type": "bond", "ports": ["fnas_lan1", "fnas_lan2"], "mode": "failover"},
                                    [], PRESENT),
        netconfig.normalize_virtual({"type": "vlan", "parent": "fnas_lan1", "vlan_id": 10}, [], PRESENT),
    ]
    entries[0]["stp"] = True
    for entry in entries:
        for action, kwargs in netconfig.virtual_steps(entry) + netconfig.virtual_remove_steps(entry):
            assert engine.get_command("network", action, **kwargs)
