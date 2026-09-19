"""A self-signed certificate, so a phone will let JARVIS use its microphone.

Browsers only grant microphone access on a "secure context". `http://localhost`
counts as one, so using JARVIS on this computer over plain HTTP is fine. A phone
reaching `http://192.168.1.50:8787` does NOT count, and `getUserMedia` is
refused outright - the page loads and the microphone button simply never works,
with nothing in the interface to explain why.

Serving HTTPS fixes it. The certificate is self-signed, so the phone shows a
warning once and you tap through; after that it is a secure context and the
microphone works. The key never leaves this machine.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path

from ..core.events import log

CERT_NAME = "realtime-cert.pem"
KEY_NAME = "realtime-key.pem"
VALID_DAYS = 825


def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def ensure(directory: Path | None = None) -> tuple[Path, Path]:
    """Return (cert, key), generating them the first time. Cached after that."""
    from .. import paths

    directory = directory or paths.ROOT
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME

    if cert_path.exists() and key_path.exists():
        if not _expired(cert_path):
            return cert_path, key_path
        log.info("realtime certificate expired - making a new one")

    _generate(cert_path, key_path)
    return cert_path, key_path


def _expired(cert_path: Path) -> bool:
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        expiry = getattr(cert, "not_valid_after_utc", None)
        if expiry is None:                      # older cryptography
            expiry = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        return expiry <= datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return True


def _generate(cert_path: Path, key_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS on this machine"),
    ])

    # Every address the phone might use has to be in the certificate, or the
    # browser rejects it before you get the chance to accept the warning.
    alt_names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(socket.gethostname()),
    ]
    for address in _local_ips():
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            continue

    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    try:
        import sys
        if sys.platform != "win32":
            key_path.chmod(0o600)
    except OSError:
        pass
    log.info("generated a self-signed certificate for realtime voice")
