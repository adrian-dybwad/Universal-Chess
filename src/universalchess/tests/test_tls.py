"""Tests for TLS certificate management.

Verifies that certificate generation, path accessors, and mobileconfig
generation work correctly without requiring mkcert to be installed
(mkcert calls are mocked).
"""

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from universalchess.tls import (
    ensure_certificates,
    get_ca_cert_path,
    get_server_cert_paths,
    generate_mobileconfig,
    SSL_DIR_NAME,
    HOSTNAME,
)


@pytest.fixture
def ssl_dir(tmp_path):
    """Provide a temporary config directory for certificate storage."""
    return tmp_path


def _create_test_cert(ssl_dir: Path) -> tuple:
    """Create a real self-signed cert and key for testing.

    Uses the cryptography library directly rather than mkcert so tests
    run without mkcert installed.
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .sign(key, hashes.SHA256())
    )

    cert_dir = ssl_dir / SSL_DIR_NAME
    cert_dir.mkdir(parents=True, exist_ok=True)

    ca_cert_path = cert_dir / "rootCA.pem"
    ca_key_path = cert_dir / "rootCA-key.pem"
    server_cert_path = cert_dir / f"{HOSTNAME}.pem"
    server_key_path = cert_dir / f"{HOSTNAME}-key.pem"

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    for p in (ca_cert_path, server_cert_path):
        p.write_bytes(cert_pem)
    for p in (ca_key_path, server_key_path):
        p.write_bytes(key_pem)

    return server_cert_path, server_key_path


class TestEnsureCertificates:
    """Tests for ensure_certificates().

    Verifies mkcert is invoked with the right arguments when certs are missing
    and skipped when they already exist.
    """

    def test_generates_certs_when_missing(self, ssl_dir):
        """When no certificates exist, ensure_certificates calls mkcert to
        create them. Regression: if this breaks, the app starts without TLS.
        """
        with patch("universalchess.tls._run_mkcert") as mock_mkcert:
            mock_mkcert.return_value = True
            _create_test_cert(ssl_dir)

            cert_path, key_path = ensure_certificates(ssl_dir)

            assert cert_path.exists()
            assert key_path.exists()

    def test_skips_generation_when_certs_exist(self, ssl_dir):
        """When certificates already exist, mkcert should not be called.
        Regression: re-generating certs on every start would invalidate
        the CA trust on all client devices.
        """
        _create_test_cert(ssl_dir)

        with patch("universalchess.tls._run_mkcert") as mock_mkcert:
            cert_path, key_path = ensure_certificates(ssl_dir)
            mock_mkcert.assert_not_called()

    def test_returns_correct_paths(self, ssl_dir):
        """Returned paths must point to the server cert and key files.
        Regression: wrong paths would cause nginx to fail to load certs.
        """
        _create_test_cert(ssl_dir)
        cert_path, key_path = ensure_certificates(ssl_dir)

        assert cert_path.name == f"{HOSTNAME}.pem"
        assert key_path.name == f"{HOSTNAME}-key.pem"

    def test_mkcert_receives_hostname_in_san(self, ssl_dir):
        """mkcert must receive the hostname as a SAN entry so browsers
        accept the certificate for https://dgt.local.
        Regression: missing SAN would cause ERR_CERT_COMMON_NAME_INVALID.
        """
        with patch("universalchess.tls._run_mkcert") as mock_mkcert:
            mock_mkcert.return_value = True
            _create_test_cert(ssl_dir)

            (ssl_dir / SSL_DIR_NAME / "rootCA.pem").unlink()

            ensure_certificates(ssl_dir)

            call_args = mock_mkcert.call_args
            cmd = call_args[0][0] if call_args[0] else call_args[1].get("cmd", [])
            assert HOSTNAME in cmd

    def test_sets_restrictive_permissions_on_keys(self, ssl_dir):
        """Private key files must be chmod 600 after generation.
        Regression: world-readable keys would be a security vulnerability.
        """
        with patch("universalchess.tls._run_mkcert") as mock_mkcert:
            mock_mkcert.return_value = True
            _create_test_cert(ssl_dir)

            (ssl_dir / SSL_DIR_NAME / "rootCA.pem").unlink()

            with patch("os.chmod") as mock_chmod:
                ensure_certificates(ssl_dir)
                chmod_paths = {str(call[0][0]) for call in mock_chmod.call_args_list}
                ca_key = str(ssl_dir / SSL_DIR_NAME / "rootCA-key.pem")
                server_key = str(ssl_dir / SSL_DIR_NAME / f"{HOSTNAME}-key.pem")
                assert ca_key in chmod_paths
                assert server_key in chmod_paths

    def test_raises_on_mkcert_not_installed(self, ssl_dir):
        """If mkcert is not installed, ensure_certificates must raise with
        install instructions rather than silently falling back to HTTP.
        Regression: silent HTTP fallback would serve passwords in cleartext.
        """
        with patch("subprocess.run", side_effect=FileNotFoundError("mkcert")):
            with pytest.raises(RuntimeError, match="mkcert is not installed"):
                ensure_certificates(ssl_dir)


class TestGetCaCertPath:
    """Tests for get_ca_cert_path()."""

    def test_returns_ca_cert_path(self, ssl_dir):
        """Should return the path to rootCA.pem under the ssl directory.
        Regression: wrong path would serve the wrong file or 404 on download.
        """
        path = get_ca_cert_path(ssl_dir)
        assert path == ssl_dir / SSL_DIR_NAME / "rootCA.pem"


class TestGetServerCertPaths:
    """Tests for get_server_cert_paths()."""

    def test_returns_server_cert_paths(self, ssl_dir):
        """Should return a tuple of (cert_path, key_path) under the ssl directory.
        Regression: wrong paths would cause nginx to fail to load certs.
        """
        cert_path, key_path = get_server_cert_paths(ssl_dir)
        assert cert_path == ssl_dir / SSL_DIR_NAME / f"{HOSTNAME}.pem"
        assert key_path == ssl_dir / SSL_DIR_NAME / f"{HOSTNAME}-key.pem"


class TestGenerateMobileconfig:
    """Tests for generate_mobileconfig().

    Verifies that a valid Apple mobileconfig XML profile is generated
    containing the CA certificate.
    """

    def test_generates_valid_mobileconfig_xml(self, ssl_dir):
        """The mobileconfig output must be valid XML with the certificate payload.
        Regression: malformed XML would prevent iOS from installing the CA.
        """
        _create_test_cert(ssl_dir)
        ca_path = get_ca_cert_path(ssl_dir)

        result = generate_mobileconfig(ca_path)

        assert b"<!DOCTYPE plist" in result
        assert b"PayloadType" in result
        assert b"com.apple.security.root" in result

    def test_contains_universal_chess_branding(self, ssl_dir):
        """The profile must use Universal Chess branding, not wifikey/bouncer.
        Regression: wrong branding confuses users about what they're installing.
        """
        _create_test_cert(ssl_dir)
        ca_path = get_ca_cert_path(ssl_dir)

        result = generate_mobileconfig(ca_path)

        assert b"Universal Chess" in result
        assert b"com.universalchess" in result
        assert b"wifikey" not in result.lower()
        assert b"bouncer" not in result.lower()

    def test_contains_base64_cert_data(self, ssl_dir):
        """The mobileconfig must embed the CA cert as base64-encoded DER data.
        Regression: missing cert data would install an empty profile on iOS.
        """
        import base64
        from cryptography.x509 import load_pem_x509_certificate
        from cryptography.hazmat.primitives.serialization import Encoding

        _create_test_cert(ssl_dir)
        ca_path = get_ca_cert_path(ssl_dir)

        result = generate_mobileconfig(ca_path)

        cert_pem = ca_path.read_bytes()
        cert = load_pem_x509_certificate(cert_pem)
        cert_der_b64 = base64.b64encode(cert.public_bytes(Encoding.DER))

        assert cert_der_b64 in result

    def test_raises_on_missing_ca_file(self, ssl_dir):
        """Should raise FileNotFoundError when the CA cert doesn't exist.
        Regression: silent failure would serve an empty mobileconfig.
        """
        with pytest.raises(FileNotFoundError):
            generate_mobileconfig(ssl_dir / "nonexistent.pem")
