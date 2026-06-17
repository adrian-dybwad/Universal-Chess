"""TLS certificate management for Universal Chess.

Generates and manages a local Certificate Authority and server certificates
using mkcert, enabling trusted HTTPS on the local network. nginx terminates
TLS using the generated certificates; client devices install the CA root
certificate (via QR code or direct download) to avoid browser warnings.

The CA private key is stored with restrictive permissions (0600) and never
served over any endpoint.

Certificate layout under ``<config_dir>/ssl/``:

    rootCA.pem             CA certificate (served to clients)
    rootCA-key.pem         CA private key (never served, chmod 600)
    dgt.local.pem          Server certificate (used by nginx)
    dgt.local-key.pem      Server private key (chmod 600)
"""

from __future__ import annotations

import base64
import logging
import os
import socket
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SSL_DIR_NAME = "ssl"
CA_CERT_FILENAME = "rootCA.pem"
CA_KEY_FILENAME = "rootCA-key.pem"

HOSTNAME = "dgt.local"
SERVER_CERT_FILENAME = f"{HOSTNAME}.pem"
SERVER_KEY_FILENAME = f"{HOSTNAME}-key.pem"


def get_ca_cert_path(config_dir: Path) -> Path:
    """Return the path to the CA root certificate PEM file."""
    return config_dir / SSL_DIR_NAME / CA_CERT_FILENAME


def get_server_cert_paths(config_dir: Path) -> tuple:
    """Return (cert_path, key_path) for the server certificate."""
    ssl_dir = config_dir / SSL_DIR_NAME
    return ssl_dir / SERVER_CERT_FILENAME, ssl_dir / SERVER_KEY_FILENAME


def get_local_ips() -> list:
    """Return non-loopback IPv4 addresses for the server.

    Uses socket.getaddrinfo on the system hostname to discover addresses.
    Falls back to connecting to a public DNS IP (no data sent) to discover
    the default route interface address.
    """
    ips: set = set()

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                ips.add(addr)
    except (socket.gaierror, OSError):
        pass

    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                addr = s.getsockname()[0]
                if not addr.startswith("127."):
                    ips.add(addr)
            finally:
                s.close()
        except OSError:
            pass

    return sorted(ips)


def _build_san_entries() -> list:
    """Build the full SAN list: hostnames + detected local IPs.

    mkcert automatically distinguishes DNS names from IP addresses
    based on format, so IP strings are passed directly.
    """
    entries = [HOSTNAME]
    local_ips = get_local_ips()
    if local_ips:
        entries.extend(local_ips)
        logger.info("TLS: detected local IPs for SAN: %s", local_ips)
    return entries


def ensure_certificates(config_dir: Path) -> tuple:
    """Ensure TLS certificates exist, generating them with mkcert if missing.

    On first run, creates:
      - A local CA (rootCA.pem, rootCA-key.pem)
      - A server cert signed by that CA (dgt.local.pem, dgt.local-key.pem)

    Subsequent runs skip generation if all four files exist.

    Returns:
        Tuple of (server_cert_path, server_key_path).

    Raises:
        RuntimeError: If mkcert fails to generate certificates.
    """
    ssl_dir = config_dir / SSL_DIR_NAME
    ssl_dir.mkdir(parents=True, exist_ok=True)

    cert_path, key_path = get_server_cert_paths(config_dir)
    ca_cert_path = get_ca_cert_path(config_dir)
    ca_key_path = ssl_dir / CA_KEY_FILENAME

    all_exist = all(p.exists() for p in (cert_path, key_path, ca_cert_path, ca_key_path))
    if all_exist:
        logger.info("TLS: certificates already present at %s", cert_path)
        return cert_path, key_path

    logger.info("TLS: generating certificates in %s", ssl_dir)

    env = os.environ.copy()
    env["CAROOT"] = str(ssl_dir)

    # Skip `mkcert -install` -- it tries to add the CA to the local system
    # trust store, which fails on some Linux distros and is unnecessary.
    # Client devices are the ones that need to trust the CA (via the
    # download page), not the Pi itself.
    san_entries = _build_san_entries()
    _run_mkcert(
        [
            "mkcert",
            "-cert-file", str(cert_path),
            "-key-file", str(key_path),
            *san_entries,
        ],
        env=env,
        purpose="generate server cert",
    )

    for sensitive_file in (ca_key_path, key_path):
        if sensitive_file.exists():
            os.chmod(str(sensitive_file), 0o600)

    if not cert_path.exists() or not key_path.exists():
        raise RuntimeError(
            f"mkcert did not produce expected certificate files in {ssl_dir}"
        )

    logger.info("TLS: certificates generated (cert=%s, ca=%s, sans=%s)",
                cert_path, ca_cert_path, san_entries)
    return cert_path, key_path


def _run_mkcert(cmd: list, env: dict, purpose: str) -> bool:
    """Execute a mkcert command.

    Args:
        cmd: Command and arguments.
        env: Environment variables (must include CAROOT).
        purpose: Human-readable description for log messages.

    Returns:
        True if the command succeeded.

    Raises:
        RuntimeError: If mkcert is not installed or the command fails.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            logger.error("mkcert failed (%s): %s", purpose, result.stderr.strip())
            raise RuntimeError(f"mkcert failed ({purpose}): {result.stderr.strip()}")
        logger.info("mkcert OK: %s", purpose)
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "mkcert is not installed. Install it with: "
            "sudo apt install mkcert libnss3-tools"
        )


def generate_mobileconfig(ca_cert_path: Path) -> bytes:
    """Generate an Apple .mobileconfig profile containing the CA certificate.

    iOS requires certificates to be delivered as signed or unsigned
    .mobileconfig profiles. This generates an unsigned profile that,
    when opened in Safari, triggers the iOS certificate install flow.

    Args:
        ca_cert_path: Path to the CA root certificate PEM file.

    Returns:
        Bytes of the mobileconfig XML.

    Raises:
        FileNotFoundError: If the CA cert file doesn't exist.
    """
    if not ca_cert_path.exists():
        raise FileNotFoundError(f"CA certificate not found: {ca_cert_path}")

    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives.serialization import Encoding

    cert_pem = ca_cert_path.read_bytes()
    cert = load_pem_x509_certificate(cert_pem)
    cert_der = cert.public_bytes(Encoding.DER)
    cert_der_b64 = base64.b64encode(cert_der).decode("ascii")

    payload_uuid = str(uuid.uuid4()).upper()
    profile_uuid = str(uuid.uuid4()).upper()

    profile_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>UniversalChess-CA.cer</string>
            <key>PayloadContent</key>
            <data>{cert_der_b64}</data>
            <key>PayloadDescription</key>
            <string>Adds the Universal Chess CA certificate to enable trusted HTTPS.</string>
            <key>PayloadDisplayName</key>
            <string>Universal Chess CA</string>
            <key>PayloadIdentifier</key>
            <string>com.universalchess.ca.{payload_uuid}</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Install this profile to trust Universal Chess for secure HTTPS connections.</string>
    <key>PayloadDisplayName</key>
    <string>Universal Chess TLS Certificate</string>
    <key>PayloadIdentifier</key>
    <string>com.universalchess.profile.{profile_uuid}</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>"""

    return profile_xml.encode("utf-8")
