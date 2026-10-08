"""WireGuard: keys, the NAS's file, devices and their configuration."""

import base64
from pathlib import Path

import pytest

import wireguard
from engine import FreedomNASEngine
from wireguard import WireGuardProblem

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")


def server(**over):
    return wireguard.new_server(dict({"endpoint": "nas.example.org"}, **over), ["192.168.1.5/24"])


def test_keys_are_x25519_pairs():
    private, public = wireguard.key_pair()
    assert len(base64.b64decode(private)) == 32 == len(base64.b64decode(public))
    assert wireguard.public_key(private) == public


def test_a_server_gets_defaults_and_its_own_key():
    entry = server(dns="1.1.1.1")
    assert {k: entry[k] for k in ("logical_name", "type", "address", "listen_port", "dns", "peers")} == {
        "logical_name": "fnas_wg1", "type": "wireguard", "address": "10.10.0.1/24",
        "listen_port": 51820, "dns": ["1.1.1.1"], "peers": []}
    assert wireguard.public_key(entry["private_key"])


@pytest.mark.parametrize("over, problem", [
    ({"endpoint": ""}, "public address"),
    ({"endpoint": "bad host!"}, "public address"),
    ({"address": "10.10.0.0/24"}, "cannot be used"),
    ({"address": "192.168.1.200/24"}, "overlaps"),
    ({"address": "fd00::1/64"}, "IPv4"),
    ({"address": "10.0.0.1/8"}, "/16 to /30"),
    ({"listen_port": 80}, "1024 to 65535"),
    ({"dns": "one"}, "DNS server"),
])
def test_bad_servers(over, problem):
    with pytest.raises(WireGuardProblem, match=problem):
        server(**over)


def test_devices_get_the_next_address_and_a_configuration():
    entry = server(dns="10.10.0.1")
    peer, conf = wireguard.add_peer(entry, "Sarah phone")
    assert peer["address"] == "10.10.0.2/32" and peer["name"] == "Sarah phone"
    assert "Address = 10.10.0.2/32" in conf and "DNS = 10.10.0.1" in conf
    assert f"PublicKey = {wireguard.public_key(entry['private_key'])}" in conf
    assert "Endpoint = nas.example.org:51820" in conf and "AllowedIPs = 10.10.0.0/24" in conf
    private = conf.split("PrivateKey = ")[1].split("\n")[0]
    assert wireguard.public_key(private) == peer["public_key"]
    entry["peers"].append(peer)
    second, _ = wireguard.add_peer(entry, "laptop")
    assert second["address"] == "10.10.0.3/32"
    with pytest.raises(WireGuardProblem, match="exists already"):
        wireguard.add_peer(entry, "Sarah phone")
    with pytest.raises(WireGuardProblem, match="device name"):
        wireguard.add_peer(entry, "bad/name")


def test_a_full_network_is_reported():
    entry = server(address="10.10.0.1/30")
    entry["peers"].append(wireguard.add_peer(entry, "one")[0])
    with pytest.raises(WireGuardProblem, match="No address left"):
        wireguard.add_peer(entry, "two")


def test_the_nas_file_lists_every_device_and_never_their_private_keys():
    entry = server()
    peer, client = wireguard.add_peer(entry, "phone")
    entry["peers"].append(peer)
    text = wireguard.conf(entry)
    assert text.startswith(f"[Interface]\nPrivateKey = {entry['private_key']}\nAddress = 10.10.0.1/24\nListenPort = 51820\n")
    assert f"PublicKey = {peer['public_key']}\nAllowedIPs = 10.10.0.2/32" in text
    assert client.split("PrivateKey = ")[1].split("\n")[0] not in text
    entry["peers"][0]["public_key"] = "x\nPostUp = rm -rf /"
    with pytest.raises(WireGuardProblem):
        wireguard.conf(entry)


def test_remove_peer():
    entry = server()
    entry["peers"].append(wireguard.add_peer(entry, "phone")[0])
    assert wireguard.remove_peer(entry, "phone")["peers"] == []
    with pytest.raises(WireGuardProblem):
        wireguard.remove_peer(entry, "laptop")


def test_parse_dump():
    text = ("priv\tpub\t51820\toff\n"
            "PEERKEY=\t(none)\t203.0.113.5:4444\t10.10.0.2/32\t1791460000\t1200\t3400\t25\n"
            "OTHER=\t(none)\t(none)\t10.10.0.3/32\t0\t0\t0\t25\n")
    assert wireguard.parse_dump(text) == {
        "PEERKEY=": {"endpoint": "203.0.113.5:4444", "latest_handshake": 1791460000, "rx": 1200, "tx": 3400},
        "OTHER=": {"endpoint": "", "latest_handshake": 0, "rx": 0, "tx": 0}}


@pytest.mark.parametrize("edition", ["debian", "freebsd", "arch", "artix", "void", "gentoo"])
def test_every_edition_has_the_wireguard_commands(edition):
    engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
    for action in ("wg_write", "wg_apply", "wg_exists", "wg_remove", "wg_dump"):
        assert "fnas_wg1" in str(engine.get_command("network", action, interface="fnas_wg1"))
    assert engine.get_command("network", "qr_svg")


@pytest.mark.parametrize("edition", ["debian", "freebsd", "void"])
def test_wg_write_is_one_shell_command_because_stdin_commands_never_get_a_shell(edition):
    import shlex
    engine = FreedomNASEngine.for_target(edition, profile_dir=PROFILES_DIR)
    argv = shlex.split(engine.get_command("network", "wg_write", interface="fnas_wg1"))
    assert argv[:2] == ["sh", "-c"] and argv[-1] == "fnas_wg1" and len(argv) == 5
