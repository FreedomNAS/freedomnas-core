"""
The WebUI's TLS material.

HTTP Basic authentication sends the administrator's password on every single
request, so serving the WebUI over plain HTTP puts it on the wire, in
reversible base64, dozens of times a session. Nothing can prove who the
appliance is without a CA the browser knows, so FreedomNAS keeps its own:

  ca.pem / ca-key.pem   this NAS's own certificate authority, made once and
                        never replaced. Importing ca.pem into a browser or a
                        phone once makes every certificate below trusted.
  cert.pem / key.pem    the server certificate the WebUI (and apps that serve
                        HTTPS with it) present, signed by the CA, followed by
                        the CA itself.

The server certificate names the hostname (also as <hostname>.local, and as
<hostname>.home.arpa with every name below it: the local domain of RFC 8375,
where the reverse proxy serves the apps by name) and every address the NAS has, and
is issued again at boot whenever one of them is missing from it, whenever it
was not signed by this CA (installs from before the CA had a self-signed one)
and before it expires. Clients that trust the CA never notice.
"""

import datetime
import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    from freedomnas import paths
except ImportError:  # running from the repository, not from the package
    import paths  # type: ignore[no-redef]


def cert_file() -> Path:
    return paths.tls_dir() / "cert.pem"


def key_file() -> Path:
    return paths.tls_dir() / "key.pem"


def ca_file() -> Path:
    return paths.tls_dir() / "ca.pem"


def ca_key_file() -> Path:
    return paths.tls_dir() / "ca-key.pem"


_CA_YEARS = 20
_SERVER_DAYS = 800
_RENEW_DAYS = 90


LOCAL_DOMAIN = "home.arpa"


def local_domain(hostname: str = "") -> str:
    """The domain the apps are named under on the LAN: <hostname>.home.arpa."""
    return f"{hostname or socket.gethostname() or 'freedomnas'}.{LOCAL_DOMAIN}"


def _names(hostname: str, addresses: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    dns = ["localhost"]
    if hostname and hostname not in dns:
        dns[:0] = [hostname, f"{hostname}.local", f"{hostname}.{LOCAL_DOMAIN}", f"*.{hostname}.{LOCAL_DOMAIN}"]
    ips = ["127.0.0.1", "::1"]
    for address in addresses:
        if address not in ips:
            ips.append(address)
    return dns, ips


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)


def addresses(engine) -> List[str]:
    """The NAS's own addresses, from the network profile's interface listing."""
    stdout = ""
    for action in ("list_interfaces_json", "list_interfaces"):
        if action in engine.list_actions("network"):
            try:
                stdout, _ = engine.execute("network", action)
            except Exception:
                return []
            break
    try:
        interfaces = json.loads(stdout or "[]")
    except ValueError:
        return []
    found: List[str] = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        for info in interface.get("addr_info") or []:
            local = str(info.get("local") or "")
            if _usable(local) and local not in found:
                found.append(local)
    return found


def fingerprint(cert_path: Optional[Path] = None) -> str:
    """The SHA-256 fingerprint, in the colon-separated form browsers show."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate((cert_path or cert_file()).read_bytes())
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def ca_fingerprint() -> str:
    return fingerprint(ca_file())


def describe() -> dict:
    """What the WebUI shows: both fingerprints, the names and addresses covered, the expiry."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_file().read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {
        "ca_fingerprint": ca_fingerprint() if ca_file().exists() else None,
        "fingerprint": fingerprint(),
        "names": san.get_values_for_type(x509.DNSName),
        "addresses": [str(ip) for ip in san.get_values_for_type(x509.IPAddress)],
        "expires": cert.not_valid_after_utc.isoformat(),
    }


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _private_bytes(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _ensure_ca(hostname: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    if ca_file().exists() and ca_key_file().exists():
        cert = x509.load_pem_x509_certificate(ca_file().read_bytes())
        key = serialization.load_pem_private_key(ca_key_file().read_bytes(), password=None)
        return cert, key

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"FreedomNAS {hostname} CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FreedomNAS"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * _CA_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(ca_key_file(), _private_bytes(key), 0o600)
    _write(ca_file(), cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert, key


def _current(ca_cert, dns_names: List[str], ip_addresses: List[str], check_addresses: bool = True) -> bool:
    from cryptography import x509

    if not (cert_file().exists() and key_file().exists()):
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_file().read_bytes())
        cert.verify_directly_issued_by(ca_cert)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:
        return False
    have_dns = set(san.get_values_for_type(x509.DNSName))
    have_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    wanted_ips = {str(ipaddress.ip_address(a)) for a in ip_addresses}
    left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    return (set(dns_names) <= have_dns and (wanted_ips <= have_ips or not check_addresses)
            and left > datetime.timedelta(days=_RENEW_DAYS))


def ensure_certificate(hostname: str = "", addresses: Iterable[str] = (), check_addresses: bool = True) -> bool:
    """
    Make sure the CA exists and the server certificate covers `hostname` and
    `addresses`; returns True if the server certificate was (re)issued. With
    `check_addresses` off, missing addresses alone do not reissue it.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    hostname = hostname or socket.gethostname() or "freedomnas"
    dns_names, ip_addresses = _names(hostname, addresses)

    tls_dir = paths.tls_dir()
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)
    ca_cert, ca_key = _ensure_ca(hostname)
    if _current(ca_cert, dns_names, ip_addresses, check_addresses):
        return False

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FreedomNAS"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(min(now + datetime.timedelta(days=_SERVER_DAYS), ca_cert.not_valid_after_utc))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in dns_names]
                + [x509.IPAddress(ipaddress.ip_address(a)) for a in ip_addresses]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write(key_file(), _private_bytes(key), 0o600)
    _write(cert_file(), certificate.public_bytes(serialization.Encoding.PEM)
           + ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return True
