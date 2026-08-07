"""Tests for the password change API endpoint.

Guards that:
  - Password change requires HTTPS (rejects HTTP via X-Forwarded-Proto)
  - Password change requires authentication
  - Input validation (empty, too short, missing fields)
  - chpasswd is called correctly on success
"""

import base64
import importlib
import sys
from unittest.mock import patch, MagicMock

import pytest

from universalchess.tests.webapp_fixture import make_test_client

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
    return make_test_client(webapp)


# Shared dummy credentials. Named rather than repeated inline so the assertions
# below reference the same values as the requests that produce them: a test that
# hardcoded "testuser:newpassword" in an assertion would keep passing if the
# helper's defaults changed, asserting against a stale expectation.
TEST_USERNAME = "testuser"
CURRENT_PASSWORD = "oldpass"
NEW_PASSWORD = "newpassword"


@pytest.fixture
def authed(monkeypatch):
    """Force verify_webdav_authentication to succeed."""
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, TEST_USERNAME))


def _auth_header(username, password):
    """Build a Basic auth header. Credentials are required, never defaulted.

    Taking them explicitly keeps each test's identity visible at the call site,
    so a test asserting on the username cannot silently diverge from the one the
    request actually sent.
    """
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _default_auth_header():
    """The Basic auth header for the standard test account."""
    return _auth_header(TEST_USERNAME, CURRENT_PASSWORD)


def _change_password_request(client, current_password=CURRENT_PASSWORD,
                             new_password=NEW_PASSWORD, extra_headers=None):
    headers = {
        **_default_auth_header(),
        "X-Forwarded-Proto": "https",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/api/system/change-password",
        json={"current_password": current_password, "new_password": new_password},
        headers=headers,
    )


class TestHttpsEnforcement:
    """Password change must only work over HTTPS."""

    def test_rejects_http(self, client, authed):
        """Plain HTTP requests must be rejected with 403.
        Regression: allowing HTTP would send passwords in cleartext.
        """
        resp = client.post(
            "/api/system/change-password",
            json={"current_password": CURRENT_PASSWORD, "new_password": NEW_PASSWORD},
            headers={**_default_auth_header(), "X-Forwarded-Proto": "http"},
        )
        assert resp.status_code == 403
        assert b"HTTPS" in resp.data

    def test_rejects_missing_proto_header(self, client, authed):
        """When X-Forwarded-Proto is absent (direct access), default is HTTP.
        Regression: missing header bypass would expose passwords.
        """
        resp = client.post(
            "/api/system/change-password",
            json={"current_password": CURRENT_PASSWORD, "new_password": NEW_PASSWORD},
            headers=_default_auth_header(),
        )
        assert resp.status_code == 403


class TestAuthentication:
    """Password change requires valid authentication."""

    def test_rejects_unauthenticated(self, client):
        """Unauthenticated requests must return 401.
        Regression: unauthenticated password change is a critical vulnerability.
        """
        resp = client.post(
            "/api/system/change-password",
            json={"current_password": CURRENT_PASSWORD, "new_password": NEW_PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert resp.status_code == 401


class TestInputValidation:
    """Password change validates input before calling chpasswd."""

    def test_rejects_empty_current_password(self, client, authed):
        """Empty current password must be rejected.
        Regression: empty current_password could bypass verification.
        """
        resp = _change_password_request(client, current_password="")
        assert resp.status_code == 400
        assert b"Current password" in resp.data

    def test_rejects_empty_new_password(self, client, authed):
        """Empty new password must be rejected.
        Regression: empty password would lock the user out.
        """
        resp = _change_password_request(client, new_password="")
        assert resp.status_code == 400
        assert b"New password" in resp.data

    def test_rejects_too_short_password(self, client, authed):
        """Passwords below minimum length must be rejected.
        Regression: very short passwords are easily guessable.
        """
        resp = _change_password_request(client, new_password="ab")
        assert resp.status_code == 400
        assert b"at least" in resp.data


class TestChpasswdRecordInjection:
    """Reject inputs that could break out of the single chpasswd stdin record."""

    @pytest.mark.parametrize(
        "payload",
        [
            f"{NEW_PASSWORD}\nroot:pwned",    # newline = chpasswd record separator
            f"{NEW_PASSWORD}\rroot:pwned",    # CR is also treated as a line break
            f"{NEW_PASSWORD}\x00root:pwned",  # NUL can truncate/confuse parsing
        ],
    )
    def test_rejects_record_separator_in_new_password(self, client, authed, payload):
        """A newline/CR/NUL in new_password must be rejected before chpasswd runs.

        Why: chpasswd reads newline-delimited "user:password" records from stdin
        and offers no escaping. Interpolating new_password lets an authenticated
        caller append a second record (e.g. "root:pwned") and change another
        account's password - privilege escalation.
        How a regression shows: without the guard the request reaches chpasswd
        (mock_run called) and returns 200 instead of 400.
        """
        with patch("universalchess.web.app.subprocess.run") as mock_run:
            resp = _change_password_request(client, new_password=payload)
            assert resp.status_code == 400
            mock_run.assert_not_called()

    def test_rejects_record_separator_in_username(self, client, authed):
        """A newline in the basic-auth username must also be rejected.

        Why: the username is interpolated into the same chpasswd record, so a
        newline in it is the same injection vector even with a clean password.
        The username is taken from the Authorization header (not the authed
        fixture's return), so a crafted header reaches the chpasswd call.
        How a regression shows: without the guard chpasswd is invoked with a
        two-line payload and the endpoint returns 200 instead of 401.
        """
        crafted = base64.b64encode(
            f"{TEST_USERNAME}\nroot:{CURRENT_PASSWORD}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {crafted}",
            "X-Forwarded-Proto": "https",
            "Content-Type": "application/json",
        }
        with patch("universalchess.web.app.subprocess.run") as mock_run:
            resp = client.post(
                "/api/system/change-password",
                json={"current_password": CURRENT_PASSWORD, "new_password": NEW_PASSWORD},
                headers=headers,
            )
            assert resp.status_code == 401
            mock_run.assert_not_called()


class TestPasswordChange:
    """Successful password change calls chpasswd correctly."""

    def test_calls_chpasswd_with_correct_input(self, client, authed):
        """chpasswd must receive 'username:newpassword' on stdin.
        Regression: wrong format would silently fail to change the password.
        """
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("universalchess.web.app.subprocess.run", return_value=mock_result) as mock_run:
            resp = _change_password_request(client)

            assert resp.status_code == 200
            assert resp.get_json()["success"] is True

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[1]["input"] == f"{TEST_USERNAME}:{NEW_PASSWORD}"
            assert "chpasswd" in call_kwargs[0][0]

    def test_returns_500_on_chpasswd_failure(self, client, authed):
        """If chpasswd fails, return 500 with error message.
        Regression: silent failure would leave the user thinking the
        password was changed when it wasn't.
        """
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "PAM: Authentication failure"

        with patch("universalchess.web.app.subprocess.run", return_value=mock_result):
            resp = _change_password_request(client)
            assert resp.status_code == 500
            assert b"Failed" in resp.data
