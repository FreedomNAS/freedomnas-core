"""The CA and the server certificate it signs for the NAS's names and addresses."""

import datetime
import json
import stat

import pytest
from cryptography import x509

import paths
import tls


@pytest.fixture(autouse=True)
def tls_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "tls_dir", lambda engine=None: tmp_path / "tls")
    return tmp_path / "tls"


def server_cert():
    return x509.load_pem_x509_certificate(tls.cert_file().read_bytes())


def san(cert):
    value = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(value.get_values_for_type(x509.DNSName)), {str(ip) for ip in value.get_values_for_type(x509.IPAddress)}


def test_the_server_certificate_names_the_host_and_its_addresses():
    assert tls.ensure_certificate("nas", ["192.168.1.200", "fd00::2"])
    dns, ips = san(server_cert())
    assert dns == {"nas", "nas.local", "nas.home.arpa", "*.nas.home.arpa", "localhost"}
    assert ips == {"127.0.0.1", "::1", "192.168.1.200", "fd00::2"}


def test_the_ca_signs_it_and_follows_it_in_the_file():
    tls.ensure_certificate("nas", [])
    ca = x509.load_pem_x509_certificate(tls.ca_file().read_bytes())
    server_cert().verify_directly_issued_by(ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not server_cert().extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    chain = x509.load_pem_x509_certificates(tls.cert_file().read_bytes())
    assert [c.subject for c in chain] == [server_cert().subject, ca.subject]


def test_keys_are_private(tls_dir):
    tls.ensure_certificate("nas", [])
    assert stat.S_IMODE(tls.key_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(tls.ca_key_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700


def test_nothing_changes_while_names_and_addresses_are_covered():
    tls.ensure_certificate("nas", ["192.168.1.200"])
    before = tls.cert_file().read_bytes()
    assert not tls.ensure_certificate("nas", ["192.168.1.200"])
    assert not tls.ensure_certificate("nas", [])
    assert tls.cert_file().read_bytes() == before


def test_a_new_address_or_hostname_reissues_it_with_the_same_ca():
    tls.ensure_certificate("nas", ["192.168.1.200"])
    ca = tls.ca_file().read_bytes()
    assert tls.ensure_certificate("nas", ["192.168.1.201"])
    assert "192.168.1.201" in san(server_cert())[1]
    assert tls.ensure_certificate("box", ["192.168.1.201"])
    assert "box" in san(server_cert())[0]
    assert tls.ca_file().read_bytes() == ca


def test_a_certificate_not_signed_by_this_ca_is_replaced():
    tls.ensure_certificate("nas", [])
    tls.ca_file().unlink()
    tls.ca_key_file().unlink()
    assert tls.ensure_certificate("nas", [])
    ca = x509.load_pem_x509_certificate(tls.ca_file().read_bytes())
    server_cert().verify_directly_issued_by(ca)


def test_it_is_renewed_before_it_expires(monkeypatch):
    tls.ensure_certificate("nas", [])
    monkeypatch.setattr(tls, "_RENEW_DAYS", tls._SERVER_DAYS + 1)
    assert tls.ensure_certificate("nas", [])


def test_server_certificates_stay_within_what_phones_accept():
    tls.ensure_certificate("nas", [])
    cert = server_cert()
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= datetime.timedelta(days=825)
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in usage


class Engine:
    def __init__(self, actions, stdout):
        self.actions, self.stdout, self.calls = actions, stdout, []

    def list_actions(self, category):
        return self.actions

    def execute(self, category, action, **kw):
        self.calls.append((category, action))
        return self.stdout, ""


def test_addresses_skip_loopback_and_link_local():
    listing = json.dumps([
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}, {"local": "::1"}]},
        {"ifname": "fnas_bridge1", "addr_info": [
            {"local": "192.168.1.200"}, {"local": "fe80::1"}, {"local": "fd00::2"}]},
        {"ifname": "fnasjails", "addr_info": [{"local": "10.231.0.1"}, {"local": "169.254.1.1"}]},
    ])
    assert tls.addresses(Engine(["list_interfaces"], listing)) == ["192.168.1.200", "fd00::2", "10.231.0.1"]


def test_addresses_prefer_the_json_listing_where_the_profile_has_one():
    engine = Engine(["list_interfaces", "list_interfaces_json"], "[]")
    tls.addresses(engine)
    assert engine.calls == [("network", "list_interfaces_json")]


def test_addresses_are_empty_when_the_listing_is_not_json():
    assert tls.addresses(Engine(["list_interfaces"], "em0 re0")) == []


def test_describe_tells_what_the_webui_shows():
    tls.ensure_certificate("nas", ["192.168.1.200"])
    info = tls.describe()
    assert info["ca_fingerprint"] == tls.ca_fingerprint() != info["fingerprint"]
    assert info["names"] == ["nas", "nas.local", "nas.home.arpa", "*.nas.home.arpa", "localhost"]
    assert info["addresses"] == ["127.0.0.1", "::1", "192.168.1.200"]
    assert info["expires"].startswith(str(server_cert().not_valid_after_utc.year))


def test_names_only_leaves_a_new_address_for_later():
    tls.ensure_certificate("nas", ["192.168.1.200"])
    assert not tls.ensure_certificate("nas", ["192.168.1.200", "10.231.0.1"], check_addresses=False)
    assert tls.ensure_certificate("box", ["192.168.1.200", "10.231.0.1"], check_addresses=False)
    assert "10.231.0.1" in san(server_cert())[1]
