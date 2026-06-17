"""Tests for TLS CA certificate endpoints.

Guards the /ca-install page and /ca.pem download endpoint, ensuring the CA
certificate is served in PEM, DER, and mobileconfig formats. These endpoints
are served over HTTP (unauthenticated) so clients can bootstrap trust before
HTTPS is available.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp
finally:
    Image.open = _orig_image_open


@pytest.fixture
def client():
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


@pytest.fixture
def ca_cert_dir(tmp_path):
    """Create a temporary CA cert for endpoint tests."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    ssl_dir = tmp_path / "ssl"
    ssl_dir.mkdir()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "dgt.local"),
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

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (ssl_dir / "rootCA.pem").write_bytes(cert_pem)

    return tmp_path


class TestCaInstallPage:
    """Tests for GET /ca-install."""

    def test_returns_html(self, client):
        """The CA install page must return HTML so browsers render it.
        Regression: wrong content type would show raw text.
        """
        resp = client.get("/ca-install")
        assert resp.status_code == 200
        assert b"Install Certificate" in resp.data

    def test_contains_platform_tabs(self, client):
        """The page must include platform-specific install instructions.
        Regression: missing tabs would leave users unable to install the CA.
        """
        resp = client.get("/ca-install")
        assert b"platform-ios" in resp.data
        assert b"platform-android" in resp.data
        assert b"platform-mac" in resp.data
        assert b"platform-win" in resp.data
        assert b"platform-linux" in resp.data

    def test_contains_universal_chess_branding(self, client):
        """Page must reference Universal Chess, not bouncer or wifikey.
        Regression: wrong branding confuses users.
        """
        resp = client.get("/ca-install")
        assert b"Universal Chess" in resp.data


class TestCaDownload:
    """Tests for GET /ca.pem."""

    def test_pem_format_default(self, client, ca_cert_dir, monkeypatch):
        """Default download must return PEM format with correct MIME type.
        Regression: wrong MIME type may prevent browsers from downloading.
        """
        monkeypatch.setattr("universalchess.web.app.CONFIG_DIR", str(ca_cert_dir))
        resp = client.get("/ca.pem")
        assert resp.status_code == 200
        assert b"BEGIN CERTIFICATE" in resp.data
        assert resp.content_type == "application/x-pem-file"

    def test_der_format(self, client, ca_cert_dir, monkeypatch):
        """DER format download must return binary cert with correct MIME type.
        Regression: wrong format would prevent Android from recognizing the cert.
        """
        monkeypatch.setattr("universalchess.web.app.CONFIG_DIR", str(ca_cert_dir))
        resp = client.get("/ca.pem?format=der")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-x509-ca-cert"
        assert b"BEGIN CERTIFICATE" not in resp.data

    def test_mobileconfig_format(self, client, ca_cert_dir, monkeypatch):
        """Mobileconfig download must return Apple profile XML.
        Regression: wrong format would prevent iOS from installing the CA.
        """
        monkeypatch.setattr("universalchess.web.app.CONFIG_DIR", str(ca_cert_dir))
        resp = client.get("/ca.pem?format=mobileconfig")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-apple-aspen-config"
        assert b"com.universalchess" in resp.data

    def test_404_when_no_cert(self, client, tmp_path, monkeypatch):
        """When no CA cert exists (TLS not configured), return 404.
        Regression: serving garbage or crashing would confuse users.
        """
        monkeypatch.setattr("universalchess.web.app.CONFIG_DIR", str(tmp_path))
        resp = client.get("/ca.pem")
        assert resp.status_code == 404

    def test_qr_format(self, client, ca_cert_dir, monkeypatch):
        """QR code generation should return SVG (if segno is available).
        Regression: broken QR would prevent mobile users from downloading CA.
        """
        monkeypatch.setattr("universalchess.web.app.CONFIG_DIR", str(ca_cert_dir))
        try:
            import segno  # noqa: F401
        except ImportError:
            pytest.skip("segno not installed")
        resp = client.get("/ca.pem?qr=1")
        assert resp.status_code == 200
        assert resp.content_type == "image/svg+xml"
