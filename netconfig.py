"""
How one network interface gets its addresses: IPv4 by DHCP or static, IPv6
automatic, static or off, DNS servers and search domains, MTU.

A configuration is the dict config.yaml keeps under
network.interfaces[].config. normalize() checks it and fills in what is
left out; steps() turns it into the profile's network.* actions.

A change made from the WebUI runs under a checkpoint: the profile undoes it
by itself after a timeout unless it is confirmed, so a wrong address cannot
lock the administrator out.
"""

import ipaddress
import re
from typing import Dict, List, Optional, Tuple

IPV4_METHODS = ("dhcp", "static")
IPV6_METHODS = ("auto", "static", "disabled")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_IFNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,14}$")


class NetConfigProblem(Exception):
    pass


def interface_problem(name: str) -> Optional[str]:
    if not _IFNAME.match(name or ""):
        return f"{name!r} is not an interface name"
    return None


def _words(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return [str(v).strip() for v in value if str(v).strip()]


def _address(value, version: int, what: str) -> str:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        raise NetConfigProblem(f"{what}: {value!r} is not an IPv{version} address")
    if address.version != version:
        raise NetConfigProblem(f"{what}: {value!r} is not an IPv{version} address")
    return str(address)


def _prefix(value, version: int, what: str) -> int:
    top = 32 if version == 4 else 128
    try:
        prefix = int(value)
    except (TypeError, ValueError):
        raise NetConfigProblem(f"{what}: {value!r} is not a prefix length")
    if not 1 <= prefix <= top:
        raise NetConfigProblem(f"{what}: the prefix length goes from 1 to {top}")
    return prefix


def normalize(request: Dict) -> Dict:
    method = request.get("method") or "dhcp"
    if method not in IPV4_METHODS:
        raise NetConfigProblem("method is dhcp or static")
    config: Dict = {"method": method}
    if method == "static":
        config["ip"] = _address(request.get("ip"), 4, "IP address")
        config["cidr"] = _prefix(request.get("cidr"), 4, "IPv4 prefix")
        gateway = str(request.get("gateway") or "").strip()
        config["gateway"] = _address(gateway, 4, "Gateway") if gateway else ""
        network = ipaddress.ip_interface(f"{config['ip']}/{config['cidr']}").network
        if config["gateway"] and ipaddress.ip_address(config["gateway"]) not in network:
            raise NetConfigProblem(f"Gateway {config['gateway']} is outside {network}")

    dns = []
    for server in _words(request.get("dns")):
        try:
            dns.append(str(ipaddress.ip_address(server)))
        except ValueError:
            raise NetConfigProblem(f"DNS server: {server!r} is not an IP address")
    config["dns"] = dns
    search = _words(request.get("dns_search"))
    for domain in search:
        if not _DOMAIN.match(domain):
            raise NetConfigProblem(f"Search domain: {domain!r} is not a domain name")
    config["dns_search"] = search
    if method == "static" and not dns:
        raise NetConfigProblem("A static address needs at least one DNS server")

    ipv6 = request.get("ipv6_method") or "auto"
    if ipv6 not in IPV6_METHODS:
        raise NetConfigProblem("ipv6_method is auto, static or disabled")
    config["ipv6_method"] = ipv6
    if ipv6 == "static":
        config["ipv6"] = _address(request.get("ipv6"), 6, "IPv6 address")
        config["ipv6_prefix"] = _prefix(request.get("ipv6_prefix") or 64, 6, "IPv6 prefix")
        gateway = str(request.get("ipv6_gateway") or "").strip()
        config["ipv6_gateway"] = _address(gateway, 6, "IPv6 gateway") if gateway else ""
    elif any(ipaddress.ip_address(d).version == 6 for d in dns) and ipv6 == "disabled":
        raise NetConfigProblem("IPv6 DNS servers need IPv6 to be on")

    mtu = request.get("mtu")
    if mtu in (None, "", 0, "0"):
        config["mtu"] = None
    else:
        try:
            config["mtu"] = int(mtu)
        except (TypeError, ValueError):
            raise NetConfigProblem(f"MTU: {mtu!r} is not a number")
        if not 576 <= config["mtu"] <= 9216:
            raise NetConfigProblem("MTU goes from 576 to 9216")
    return config


def steps(interface: str, config: Dict) -> List[Tuple[str, Dict]]:
    plan: List[Tuple[str, Dict]] = [("connection_ensure", {})]
    if config.get("method") == "static":
        plan.append(("ipv4_static", {"ip": config["ip"], "cidr": config["cidr"],
                                     "gateway": config.get("gateway") or ""}))
    else:
        plan.append(("ipv4_dhcp", {}))
    if config.get("ipv6_method") == "static":
        plan.append(("ipv6_static", {"ipv6": config["ipv6"],
                                     "ipv6_prefix": config.get("ipv6_prefix") or 64,
                                     "ipv6_gateway": config.get("ipv6_gateway") or ""}))
    elif config.get("ipv6_method") == "disabled":
        plan.append(("ipv6_disabled", {}))
    else:
        plan.append(("ipv6_auto", {}))
    dns = config.get("dns") or []
    if isinstance(dns, str):
        dns = dns.split()
    if dns or config.get("dns_search"):
        plan.append(("dns_set", {
            "dns": " ".join(d for d in dns if ipaddress.ip_address(d).version == 4),
            "dns6": " ".join(d for d in dns if ipaddress.ip_address(d).version == 6),
            "dns_search": " ".join(config.get("dns_search") or []),
        }))
    else:
        plan.append(("dns_auto", {}))
    if config.get("mtu"):
        plan.append(("mtu_set", {"mtu": config["mtu"]}))
    else:
        plan.append(("mtu_auto", {}))
    plan.append(("connection_activate", {}))
    return [(action, dict(kwargs, interface=interface)) for action, kwargs in plan]


def apply(engine, interface: str, config: Dict) -> None:
    for action, kwargs in steps(interface, config):
        engine.execute("network", action, timeout=60, **kwargs)


def is_configured(engine, interface: str) -> bool:
    stdout, _ = engine.execute("network", "connection_exists", check=False, interface=interface)
    return stdout.strip() == "yes"


def checkpoint_create(engine, seconds: int) -> str:
    stdout, _ = engine.execute("network", "checkpoint_create", rollback_seconds=seconds)
    checkpoint = stdout.strip()
    if not checkpoint:
        raise NetConfigProblem("The network checkpoint could not be created")
    return checkpoint


def checkpoint_confirm(engine, checkpoint: str) -> None:
    engine.execute("network", "checkpoint_destroy", checkpoint=checkpoint)


def checkpoint_rollback(engine, checkpoint: str) -> None:
    engine.execute("network", "checkpoint_rollback", checkpoint=checkpoint, timeout=60)
