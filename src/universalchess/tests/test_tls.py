"""Tests for TLS certificate generation (universalchess.tls).

These guard the regression reported in the field: the server certificate was
generated for a hardcoded ``dgt.local`` name that does not match the device's
real mDNS hostname (``<hostname>.local``), producing browser name-mismatch
warnings even after the CA was installed. They also guard the follow-on
requirement that the hostname can change after install, so the certificate must
be regenerated when it no longer covers the current hostname.

mkcert is not available in CI, so the boundary that shells out to mkcert
(``tls._run_mkcert``) is replaced with a fake that writes real, parseable
certificate files honouring the requested SAN list. This keeps the SAN-coverage
logic under test against actual X.509 parsing rather than a stubbed predicate.
"""

import datetime
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import universalchess.tls as tls


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _write_cert(cert_path: Path, key_path: Path, dns_names, ip_names=()):
    """Write a self-signed cert with the given SAN entries to disk.

    Used both to fabricate a pre-existing certificate and as the payload of the
    fake mkcert, so SAN-coverage assertions run against genuine X.509 parsing.
    """
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName(n) for n in dns_names]
    import ipaddress
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ip_names]
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=825)
        )
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _install_fake_mkcert(monkeypatch):
    """Replace tls._run_mkcert with a fake that writes cert files from the SAN args.

    Returns a list that records each invocation so tests can assert whether
    generation happened and with which SAN entries.
    """
    calls = []

    def fake(cmd, env, purpose):
        calls.append({"cmd": list(cmd), "purpose": purpose, "env": dict(env)})
        cert_path = Path(cmd[cmd.index("-cert-file") + 1])
        key_path = Path(cmd[cmd.index("-key-file") + 1])
        # SAN entries are every positional arg after the flag pairs.
        san = [a for a in cmd[1:] if not a.startswith("-") and a not in (str(cert_path), str(key_path))]
        dns = [s for s in san if not s.replace(".", "").isdigit()]
        ips = [s for s in san if s.replace(".", "").isdigit()]
        _write_cert(cert_path, key_path, dns or ["localhost"], ips)
        caroot = Path(env["CAROOT"])
        _write_cert(caroot / tls.CA_CERT_FILENAME, caroot / tls.CA_KEY_FILENAME, ["Universal Chess Test CA"])
        return True

    monkeypatch.setattr(tls, "_run_mkcert", fake)
    return calls


@pytest.fixture
def fixed_hostname(monkeypatch):
    """Pin the reported hostname so SAN/coverage assertions are deterministic."""
    monkeypatch.setattr(tls.socket, "gethostname", lambda: "dgtcentaur")
    monkeypatch.setattr(tls, "get_local_ips", lambda: [])
    return "dgtcentaur"


# ---------------------------------------------------------------------------
# Hostname derivation
# ---------------------------------------------------------------------------

class TestLocalHostnames:
    def test_includes_mdns_bare_and_loopback(self, fixed_hostname):
        """SAN must contain the real ``<host>.local`` mDNS name.
        Regression: a hardcoded ``dgt.local`` omitted the real name and the
        cert failed to validate when reached as ``dgtcentaur.local``.
        """
        names = tls.local_hostnames()
        assert "dgtcentaur.local" in names
        assert "dgtcentaur" in names
        assert "localhost" in names

    def test_strips_existing_dot_local_suffix(self, monkeypatch):
        """A hostname already ending in .local must not yield ``host.local.local``.
        Regression: doubled suffix would never match the name the client uses.
        """
        monkeypatch.setattr(tls.socket, "gethostname", lambda: "dgtcentaur.local")
        names = tls.local_hostnames()
        assert "dgtcentaur.local" in names
        assert "dgtcentaur.local.local" not in names


class TestBuildSanEntries:
    def test_contains_mdns_name_and_loopback_ip(self, fixed_hostname):
        """SAN list must carry the mDNS name and 127.0.0.1 with no duplicates.
        Regression: missing loopback broke local checks; duplicates make mkcert
        emit redundant SANs.
        """
        entries = tls._build_san_entries()
        assert "dgtcentaur.local" in entries
        assert "127.0.0.1" in entries
        assert len(entries) == len(set(entries))

    def test_includes_detected_local_ips(self, monkeypatch):
        """Detected LAN IPs must be present so IP-based access also validates.
        Regression: dropping detected IPs forced clients onto name-only access.
        """
        monkeypatch.setattr(tls.socket, "gethostname", lambda: "dgtcentaur")
        monkeypatch.setattr(tls, "get_local_ips", lambda: ["192.168.1.50"])
        entries = tls._build_san_entries()
        assert "192.168.1.50" in entries


# ---------------------------------------------------------------------------
# Certificate SAN inspection
# ---------------------------------------------------------------------------

class TestCertificateDnsNames:
    def test_parses_san_dns_names(self, tmp_path):
        """SAN DNS names must be read back from a written cert.
        Regression: failure to parse SAN would make coverage checks always
        regenerate (or never), defeating rename handling.
        """
        cert = tmp_path / "server.pem"
        key = tmp_path / "server-key.pem"
        _write_cert(cert, key, ["dgtcentaur.local", "dgtcentaur", "localhost"], ["127.0.0.1"])
        names = tls.certificate_dns_names(cert)
        assert "dgtcentaur.local" in names
        assert "localhost" in names

    def test_missing_file_returns_empty(self, tmp_path):
        """A missing cert yields no names rather than raising.
        Regression: an exception here would crash the boot-time generator.
        """
        assert tls.certificate_dns_names(tmp_path / "nope.pem") == set()


class TestCoversCurrentHostname:
    def test_true_when_mdns_name_present(self, tmp_path, fixed_hostname):
        """Coverage is True only when ``<host>.local`` is in the SAN.
        Regression: this predicate gates regeneration on rename.
        """
        cert = tmp_path / "server.pem"
        key = tmp_path / "server-key.pem"
        _write_cert(cert, key, ["dgtcentaur.local", "localhost"], ["127.0.0.1"])
        assert tls.certificate_covers_current_hostname(cert) is True

    def test_false_when_mdns_name_absent(self, tmp_path, fixed_hostname):
        """A cert for a different host must report not-covered so it regenerates.
        Regression: the exact field bug -- cert for ``dgt.local`` on a host
        named ``dgtcentaur`` -- must be detected as stale.
        """
        cert = tmp_path / "server.pem"
        key = tmp_path / "server-key.pem"
        _write_cert(cert, key, ["dgt.local", "localhost"], ["127.0.0.1"])
        assert tls.certificate_covers_current_hostname(cert) is False


# ---------------------------------------------------------------------------
# ensure_certificates
# ---------------------------------------------------------------------------

class TestEnsureCertificates:
    def test_generates_when_missing(self, tmp_path, fixed_hostname, monkeypatch):
        """First run must create cert/key/CA covering the real hostname.
        Regression: nginx cannot start without these files; SAN must match host.
        """
        calls = _install_fake_mkcert(monkeypatch)
        cert, key, regenerated = tls.ensure_certificates(tmp_path)
        assert regenerated is True
        assert cert.exists() and key.exists()
        assert tls.get_ca_cert_path(tmp_path).exists()
        assert "dgtcentaur.local" in tls.certificate_dns_names(cert)
        assert len(calls) == 1

    def test_filenames_are_hostname_independent(self, tmp_path, fixed_hostname, monkeypatch):
        """Cert filenames must be stable regardless of hostname.
        Regression: embedding the hostname in the filename broke the nginx
        config path whenever the device was renamed.
        """
        _install_fake_mkcert(monkeypatch)
        cert, key, _ = tls.ensure_certificates(tmp_path)
        assert cert.name == "server.pem"
        assert key.name == "server-key.pem"

    def test_skips_when_present_and_covers_hostname(self, tmp_path, fixed_hostname, monkeypatch):
        """A valid, hostname-matching cert must not be regenerated.
        Regression: regenerating every boot would churn the CA and force
        clients to re-trust repeatedly.
        """
        ssl_dir = tmp_path / tls.SSL_DIR_NAME
        cert, key = tls.get_server_cert_paths(tmp_path)
        _write_cert(cert, key, ["dgtcentaur.local", "localhost"], ["127.0.0.1"])
        _write_cert(ssl_dir / tls.CA_CERT_FILENAME, ssl_dir / tls.CA_KEY_FILENAME, ["CA"])
        calls = _install_fake_mkcert(monkeypatch)
        _, _, regenerated = tls.ensure_certificates(tmp_path)
        assert regenerated is False
        assert calls == []

    def test_regenerates_when_hostname_changed(self, tmp_path, fixed_hostname, monkeypatch):
        """A cert that no longer covers the current hostname must regenerate.
        Regression: the device can be renamed after install; a stale cert must
        be replaced so the new ``<host>.local`` validates.
        """
        ssl_dir = tmp_path / tls.SSL_DIR_NAME
        cert, key = tls.get_server_cert_paths(tmp_path)
        _write_cert(cert, key, ["dgt.local", "localhost"], ["127.0.0.1"])
        _write_cert(ssl_dir / tls.CA_CERT_FILENAME, ssl_dir / tls.CA_KEY_FILENAME, ["CA"])
        calls = _install_fake_mkcert(monkeypatch)
        _, _, regenerated = tls.ensure_certificates(tmp_path)
        assert regenerated is True
        assert len(calls) == 1
        assert "dgtcentaur.local" in tls.certificate_dns_names(cert)

    def test_force_regenerates_even_when_valid(self, tmp_path, fixed_hostname, monkeypatch):
        """force=True must regenerate regardless of current validity.
        Regression: operators need an explicit way to rotate certs.
        """
        ssl_dir = tmp_path / tls.SSL_DIR_NAME
        cert, key = tls.get_server_cert_paths(tmp_path)
        _write_cert(cert, key, ["dgtcentaur.local", "localhost"], ["127.0.0.1"])
        _write_cert(ssl_dir / tls.CA_CERT_FILENAME, ssl_dir / tls.CA_KEY_FILENAME, ["CA"])
        calls = _install_fake_mkcert(monkeypatch)
        _, _, regenerated = tls.ensure_certificates(tmp_path, force=True)
        assert regenerated is True
        assert len(calls) == 1
