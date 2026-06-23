"""TLS certificate management for Universal Chess.

Generates and manages a local Certificate Authority and a server certificate
using mkcert, enabling trusted HTTPS on the local network. nginx terminates
TLS using the generated certificate; client devices install the CA root
certificate (via QR code or direct download) to avoid browser warnings.

The CA private key is stored with restrictive permissions (0600) and never
served over any endpoint.

Hostname handling
-----------------
The server certificate's Subject Alternative Names are derived from the
device's *actual* hostname at generation time (``<hostname>.local`` plus the
bare hostname, ``localhost`` and the loopback/LAN IPs) -- never a hardcoded
name. This guards a field regression where the cert was issued for a fixed
``dgt.local`` while the device advertised a different mDNS name (e.g.
``dgtcentaur.local``), producing browser name-mismatch warnings even after the
CA was trusted.

Because the hostname can change after install (the install-time rename only
takes effect on the following boot, and an operator may rename the device
later), :func:`ensure_certificates` regenerates the certificate whenever the
existing one no longer covers the current ``<hostname>.local`` name. The boot
oneshot and an ``/etc/hostname`` path unit invoke this so a rename refreshes
the certificate immediately.

Certificate layout under ``<config_dir>/ssl/``:

    rootCA.pem             CA certificate (served to clients)
    rootCA-key.pem         CA private key (never served, chmod 600)
    server.pem             Server certificate (used by nginx)
    server-key.pem         Server private key (chmod 600)

The server cert/key filenames are intentionally hostname-independent so the
nginx configuration never has to change when the device is renamed.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import socket
import subprocess  # nosec B404 -- used only to invoke mkcert with a fixed argv
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SSL_DIR_NAME = "ssl"
CA_CERT_FILENAME = "rootCA.pem"
CA_KEY_FILENAME = "rootCA-key.pem"

# Hostname-independent so the nginx config and CA layout survive a rename.
SERVER_CERT_FILENAME = "server.pem"
SERVER_KEY_FILENAME = "server-key.pem"

MDNS_SUFFIX = ".local"
_DEFAULT_CONFIG_DIR = "/opt/universalchess/config"
_MKCERT_TIMEOUT_SECONDS = 30


def get_ca_cert_path(config_dir: Path) -> Path:
    """Return the path to the CA root certificate PEM file."""
    return config_dir / SSL_DIR_NAME / CA_CERT_FILENAME


def get_server_cert_paths(config_dir: Path) -> tuple:
    """Return ``(cert_path, key_path)`` for the server certificate."""
    ssl_dir = config_dir / SSL_DIR_NAME
    return ssl_dir / SERVER_CERT_FILENAME, ssl_dir / SERVER_KEY_FILENAME


def current_mdns_name() -> str:
    """Return the device's primary mDNS name (``<short-hostname>.local``).

    The short hostname is the first label of :func:`socket.gethostname`, so a
    value already carrying a domain or ``.local`` suffix does not produce a
    doubled suffix. This is the name browsers use to reach the device on the
    LAN and the one a valid server certificate must cover.
    """
    raw = (socket.gethostname() or "").strip()
    short = raw.split(".")[0] if raw else ""
    if not short:
        short = "localhost"
    return f"{short}{MDNS_SUFFIX}"


def local_hostnames() -> list:
    """Return the DNS names the server certificate should cover.

    Ordered, de-duplicated: the mDNS name first (the canonical access name),
    then the bare short hostname, then ``localhost``.
    """
    mdns = current_mdns_name()
    short = mdns[: -len(MDNS_SUFFIX)]
    names: list = []
    for name in (mdns, short, "localhost"):
        if name and name not in names:
            names.append(name)
    return names


def get_local_ips() -> list:
    """Return non-loopback IPv4 addresses for the server.

    Uses ``socket.getaddrinfo`` on the system hostname to discover addresses.
    Falls back to opening a UDP socket toward a public IP (no data is sent) to
    learn the default-route interface address.
    """
    ips: set = set()

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                ips.add(addr)
    except (socket.gaierror, OSError) as exc:
        # Best-effort: a host with no resolvable address still gets a valid
        # cert from the hostname/loopback SANs, so this is informational only.
        logger.debug("TLS: hostname address lookup failed: %s", exc)

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
        except OSError as exc:
            # No default route (e.g. offline at boot). The cert still covers the
            # hostname and loopback; LAN-IP SANs are simply omitted.
            logger.debug("TLS: default-route IP discovery failed: %s", exc)

    return sorted(ips)


def _build_san_entries() -> list:
    """Build the full SAN list: hostnames + loopback + detected local IPs.

    mkcert distinguishes DNS names from IP addresses by their format, so IP
    strings are passed through directly. ``127.0.0.1`` is always included so
    loopback access (and local health checks) validate.
    """
    entries: list = list(local_hostnames())
    for ip in ["127.0.0.1", *get_local_ips()]:
        if ip not in entries:
            entries.append(ip)
    return entries


def certificate_dns_names(cert_path: Path) -> set:
    """Return the set of SAN DNS names in ``cert_path``.

    Returns an empty set when the file is missing or cannot be parsed, so
    callers can treat "no readable cert" the same as "does not cover the
    hostname" and regenerate rather than crash on a corrupt/absent file.
    """
    if not cert_path.exists():
        return set()
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        return set(san.get_values_for_type(x509.DNSName))
    except Exception:  # noqa: BLE001 -- any parse failure means "treat as stale"
        logger.warning("TLS: could not read SAN from %s; treating as stale", cert_path)
        return set()


def certificate_covers_current_hostname(cert_path: Path) -> bool:
    """Return whether ``cert_path`` covers the current ``<hostname>.local``.

    This is the predicate that gates regeneration on rename: when the device
    name changes the existing certificate stops covering the new mDNS name and
    must be reissued.
    """
    return current_mdns_name() in certificate_dns_names(cert_path)


def ensure_certificates(config_dir: Path, *, force: bool = False) -> tuple:
    """Ensure a hostname-matching TLS certificate exists.

    Generates (or regenerates) the local CA and server certificate with mkcert
    when any of the four files is missing, when the existing server cert no
    longer covers the current ``<hostname>.local`` name, or when ``force`` is
    set. Otherwise it leaves the existing certificate untouched -- regenerating
    a valid cert on every boot would rotate the CA and force every client to
    re-trust it.

    Args:
        config_dir: Base config directory; certs live under ``<config_dir>/ssl``.
        force: Regenerate unconditionally (operator-initiated rotation).

    Returns:
        ``(server_cert_path, server_key_path, regenerated)`` where
        ``regenerated`` is True iff mkcert was invoked this call.

    Raises:
        RuntimeError: If mkcert is unavailable or fails to produce the files.
    """
    config_dir = Path(config_dir)
    ssl_dir = config_dir / SSL_DIR_NAME
    ssl_dir.mkdir(parents=True, exist_ok=True)

    cert_path, key_path = get_server_cert_paths(config_dir)
    ca_cert_path = get_ca_cert_path(config_dir)
    ca_key_path = ssl_dir / CA_KEY_FILENAME

    all_exist = all(
        p.exists() for p in (cert_path, key_path, ca_cert_path, ca_key_path)
    )
    if not force and all_exist and certificate_covers_current_hostname(cert_path):
        logger.info("TLS: certificate at %s already covers %s",
                    cert_path, current_mdns_name())
        return cert_path, key_path, False

    san_entries = _build_san_entries()
    logger.info("TLS: generating certificate in %s for SANs %s", ssl_dir, san_entries)

    env = os.environ.copy()
    env["CAROOT"] = str(ssl_dir)

    # `mkcert -install` is intentionally skipped: it tries to add the CA to the
    # Pi's own system trust store, which fails on some distros and is pointless
    # here -- it is the *client* devices that must trust the CA (via the
    # download page), not the Pi.
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

    logger.info("TLS: certificate generated (cert=%s, ca=%s, sans=%s)",
                cert_path, ca_cert_path, san_entries)
    return cert_path, key_path, True


def _run_mkcert(cmd: list, env: dict, purpose: str) -> bool:
    """Execute a mkcert command.

    Args:
        cmd: Command and arguments.
        env: Environment variables (must include ``CAROOT``).
        purpose: Human-readable description for log messages.

    Returns:
        True if the command succeeded.

    Raises:
        RuntimeError: If mkcert is not installed or the command fails.
    """
    try:
        # No shell and a fixed argv list: cmd[0] is the literal "mkcert" and the
        # remaining args are file paths plus SAN entries derived from the
        # system hostname/IPs -- not attacker-controlled free text -- so there
        # is no shell-injection surface. mkcert is a declared package dependency.
        result = subprocess.run(  # noqa: S603  # nosec B603 -- controlled argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=_MKCERT_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "mkcert is not installed. Install it with: "
            "sudo apt install mkcert libnss3-tools"
        ) from exc

    if result.returncode != 0:
        logger.error("mkcert failed (%s): %s", purpose, result.stderr.strip())
        raise RuntimeError(f"mkcert failed ({purpose}): {result.stderr.strip()}")
    logger.info("mkcert OK: %s", purpose)
    return True


def generate_mobileconfig(ca_cert_path: Path) -> bytes:
    """Generate an Apple ``.mobileconfig`` profile containing the CA certificate.

    iOS requires certificates to be delivered as signed or unsigned
    ``.mobileconfig`` profiles. This produces an unsigned profile that, when
    opened in Safari, triggers the iOS certificate install flow.

    Args:
        ca_cert_path: Path to the CA root certificate PEM file.

    Returns:
        Bytes of the mobileconfig XML.

    Raises:
        FileNotFoundError: If the CA cert file does not exist.
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


def main(argv=None) -> int:
    """CLI entrypoint: ensure certificates exist for the current hostname.

    Invoked by the install script and the boot/rename systemd units as
    ``python -m universalchess.tls [config_dir] [--force]``. Exits non-zero on
    failure so a unit ordered before nginx surfaces the problem instead of
    letting nginx start without a usable certificate.
    """
    parser = argparse.ArgumentParser(
        prog="universalchess.tls",
        description="Ensure a hostname-matching TLS certificate exists.",
    )
    parser.add_argument(
        "config_dir",
        nargs="?",
        default=os.environ.get("DGTCM_CONFIG_DIR", _DEFAULT_CONFIG_DIR),
        help="Config directory containing the ssl/ subdirectory.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even when the existing certificate is valid.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        cert_path, _key_path, regenerated = ensure_certificates(
            Path(args.config_dir), force=args.force
        )
    except RuntimeError as exc:
        logger.error("TLS: %s", exc)
        return 1

    if regenerated:
        print(f"::: TLS certificate generated for {current_mdns_name()} at {cert_path}")
    else:
        print(f"::: TLS certificate already valid for {current_mdns_name()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
