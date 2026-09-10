"""
The WebUI's TLS material.

HTTP Basic authentication sends the administrator's password on every single
request, so serving the WebUI over plain HTTP puts it on the wire, in
reversible base64, dozens of times a session. A self-signed certificate does
not prove who the appliance is — nothing can, without a CA the browser knows —
but it does stop the password from being readable by anyone on the network.

Generated once at first boot, into /etc/freedomnas/tls. The fingerprint is
printed so it can be compared against what the browser shows the first time.
"""

import datetime
import ipaddress
import socket
from pathlib import Path
from typing import List, Tuple

TLS_DIR = Path("/etc/freedomnas/tls")
CERT_FILE = TLS_DIR / "cert.pem"
KEY_FILE = TLS_DIR / "key.pem"

_VALID_YEARS = 10


def _names(hostname: str) -> Tuple[List[str], List[str]]:
    dns = ["localhost"]
    if hostname and hostname not in dns:
        dns.insert(0, hostname)
    return dns, ["127.0.0.1", "::1"]


def fingerprint(cert_path: Path = CERT_FILE) -> str:
    """The SHA-256 fingerprint, in the colon-separated form browsers show."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def ensure_certificate(hostname: str = "") -> bool:
    """
    Create the certificate and key if they are not there already.

    Returns True if something was generated. Existing files are never
    replaced: regenerating on every boot would retrain the admin to click
    through a changing certificate, which is the opposite of the point.
    """
    if CERT_FILE.exists() and KEY_FILE.exists():
        return False

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    hostname = hostname or socket.gethostname() or "freedomnas"
    dns_names, ip_addresses = _names(hostname)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FreedomNAS"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * _VALID_YEARS))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in dns_names]
                + [x509.IPAddress(ipaddress.ip_address(a)) for a in ip_addresses]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    TLS_DIR.mkdir(parents=True, exist_ok=True)
    TLS_DIR.chmod(0o700)
    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    KEY_FILE.chmod(0o600)
    CERT_FILE.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    CERT_FILE.chmod(0o644)
    return True
