"""
WireGuard VPN: one interface on the NAS (fnas_wg1) and the devices allowed
to connect to it, each with its own key pair.

The entry lives in network.interfaces with type "wireguard". Its file is
written in the wg-quick format, which every edition reads: NetworkManager
imports it, FreeBSD's wg-quick brings it up. Keys are X25519, made here.
A device's private key is handed out once, in its configuration, and never
stored on the NAS.
"""

import base64
import ipaddress
import re
from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

NAME = "fnas_wg1"
_PEER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


class WireGuardProblem(Exception):
    pass


def key_pair() -> Tuple[str, str]:
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode(), base64.b64encode(public).decode()


def public_key(private: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private))
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                          serialization.PublicFormat.Raw)).decode()


def new_server(request: Dict, taken_networks: List[str]) -> Dict:
    try:
        address = ipaddress.ip_interface(str(request.get("address") or "10.10.0.1/24").strip())
    except ValueError:
        raise WireGuardProblem("The VPN address is like 10.10.0.1/24")
    if address.version != 4 or not 16 <= address.network.prefixlen <= 30:
        raise WireGuardProblem("The VPN address is IPv4, with a prefix from /16 to /30")
    if address.ip in (address.network.network_address, address.network.broadcast_address):
        raise WireGuardProblem(f"{address.ip} cannot be used as the NAS's VPN address")
    for other in taken_networks:
        if address.network.overlaps(ipaddress.ip_network(other, strict=False)):
            raise WireGuardProblem(f"{address.network} overlaps {other}, already used on this NAS")
    try:
        port = int(request.get("listen_port") or 51820)
    except (TypeError, ValueError):
        raise WireGuardProblem("The port is a number")
    if not 1024 <= port <= 65535:
        raise WireGuardProblem("The port goes from 1024 to 65535")
    endpoint = str(request.get("endpoint") or "").strip()
    if not endpoint or not _HOST.match(endpoint):
        raise WireGuardProblem("The public address is the name or IP your devices reach the NAS at "
                               "from outside (your router forwards the port to the NAS)")
    dns = []
    for server in str(request.get("dns") or "").replace(",", " ").split():
        try:
            dns.append(str(ipaddress.ip_address(server)))
        except ValueError:
            raise WireGuardProblem(f"DNS server: {server!r} is not an IP address")
    private, _ = key_pair()
    return {"logical_name": NAME, "type": "wireguard", "address": str(address),
            "listen_port": port, "endpoint": endpoint, "dns": dns,
            "private_key": private, "peers": []}


def add_peer(entry: Dict, name: str) -> Tuple[Dict, str]:
    """A new device: its peer entry for the NAS, and its own configuration (shown once)."""
    name = (name or "").strip()
    if not _PEER_NAME.match(name):
        raise WireGuardProblem("A device name has letters, digits, spaces, '.', '_' or '-' (up to 32)")
    if any(p["name"] == name for p in entry.get("peers") or []):
        raise WireGuardProblem(f"A device named {name!r} exists already")
    server = ipaddress.ip_interface(entry["address"])
    used = {server.ip} | {ipaddress.ip_interface(p["address"]).ip for p in entry.get("peers") or []}
    free = next((ip for ip in server.network.hosts() if ip not in used), None)
    if free is None:
        raise WireGuardProblem(f"No address left in {server.network}")
    private, public = key_pair()
    peer = {"name": name, "public_key": public, "address": f"{free}/32"}
    lines = ["[Interface]", f"PrivateKey = {private}", f"Address = {free}/32"]
    if entry.get("dns"):
        lines.append(f"DNS = {', '.join(entry['dns'])}")
    lines += ["", "[Peer]", f"PublicKey = {public_key(entry['private_key'])}",
              f"Endpoint = {entry['endpoint']}:{entry['listen_port']}",
              f"AllowedIPs = {server.network}", "PersistentKeepalive = 25", ""]
    return peer, "\n".join(lines)


def remove_peer(entry: Dict, name: str) -> Dict:
    peers = [p for p in entry.get("peers") or [] if p["name"] != name]
    if len(peers) == len(entry.get("peers") or []):
        raise WireGuardProblem(f"No device named {name!r}")
    return dict(entry, peers=peers)


def conf(entry: Dict) -> str:
    """The NAS's side, in the wg-quick format."""
    lines = ["[Interface]", f"PrivateKey = {entry['private_key']}",
             f"Address = {entry['address']}", f"ListenPort = {entry['listen_port']}"]
    for peer in entry.get("peers") or []:
        if not _KEY.match(peer.get("public_key", "")):
            raise WireGuardProblem(f"The key of {peer.get('name')!r} is not a WireGuard key")
        lines += ["", f"# {peer['name']}", "[Peer]", f"PublicKey = {peer['public_key']}",
                  f"AllowedIPs = {peer['address']}"]
    return "\n".join(lines) + "\n"


def parse_dump(text: str) -> Dict[str, Dict]:
    """`wg show <if> dump`: public key → last handshake (epoch seconds), received, sent."""
    peers: Dict[str, Dict] = {}
    for line in (text or "").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) >= 7:
            peers[fields[0]] = {"endpoint": "" if fields[2] == "(none)" else fields[2],
                                "latest_handshake": int(fields[4] or 0),
                                "rx": int(fields[5] or 0), "tx": int(fields[6] or 0)}
    return peers


def write(engine, entry: Dict) -> None:
    """Write the file through the profile (it holds the private key: 0600)."""
    engine.execute_with_stdin("network", "wg_write", conf(entry), interface=entry["logical_name"])


def apply(engine, entry: Dict) -> None:
    write(engine, entry)
    engine.execute("network", "wg_apply", timeout=60, interface=entry["logical_name"])


def remove(engine, name: str) -> None:
    engine.execute("network", "wg_remove", timeout=60, interface=name)


def status(engine, name: str) -> Dict[str, Dict]:
    stdout, _ = engine.execute("network", "wg_dump", check=False, interface=name)
    return parse_dump(stdout)


def find(interfaces: List[Dict]) -> Optional[Dict]:
    return next((i for i in interfaces if i.get("type") == "wireguard"), None)
