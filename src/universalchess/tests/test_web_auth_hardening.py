"""Tests for web authentication and WebDAV privilege hardening.

Root cause these guard
----------------------
Three separate weaknesses in the request path, all inherited or accreted:

1. **WebDAV ``/777.txt``** -- a DGT Centaur legacy feature where uploading a file
   named ``777.txt`` made the server ``chmod 0o777`` every path listed inside it.
   Nothing in Universal-Chess uses it, and it exists only to make files
   world-writable, so it is removed rather than narrowed.

2. **Non-constant-time hash comparison** -- the crypt fallback compared the
   computed hash to the stored hash with ``==``, which short-circuits on the first
   differing byte and leaks a timing signal about the hash.

3. **A 4-character password minimum** -- this is the Linux account password, which
   grants SSH access and passwordless sudo to the root helper scripts. It is only
   ever set through the web UI (never typed on the board), so a longer minimum
   costs no board-side convenience.
"""

import os
import stat

import pytest

from universalchess.tests.webapp_fixture import load_webapp, make_test_client

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")


webapp = load_webapp()


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


class TestWebdavChmodFeatureRemoved:
    """Uploading 777.txt must never change file permissions."""

    def test_put_777_does_not_widen_permissions(self, client, authed, monkeypatch, tmp_path):
        """A 777.txt upload listing a file must leave that file's mode untouched.

        How the regression manifests: restoring the feature makes the listed file
        world-writable (0o777), so the mode assertion changes from 0o600 to 0o777.
        A world-writable file in the service user's home is a persistence foothold
        for anyone who gains even brief write access.
        """
        monkeypatch.setattr(webapp, "WEBDAV_BASE_PATH", str(tmp_path))
        victim = tmp_path / "victim.txt"
        victim.write_text("secret")
        victim.chmod(0o600)

        resp = client.put("/777.txt", data=b"/victim.txt\n")

        assert resp.status_code in (201, 204)
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o600

    def test_chmod_is_never_called_during_a_put(self, client, authed, monkeypatch, tmp_path):
        """No WebDAV PUT may invoke chmod at all.

        Broader than the mode check: catches a reintroduced chmod that targets a
        different path (or fails silently on the listed one) and so would leave the
        victim's mode coincidentally correct while still being exploitable.
        """
        monkeypatch.setattr(webapp, "WEBDAV_BASE_PATH", str(tmp_path))
        calls = []
        monkeypatch.setattr(webapp.os, "chmod", lambda *a, **k: calls.append(a))

        client.put("/777.txt", data=b"/anything.txt\n")

        assert calls == []

    def test_upload_is_still_stored(self, client, authed, monkeypatch, tmp_path):
        """Removing the chmod behaviour must not break ordinary WebDAV writes.

        How the regression manifests: an over-broad removal that dropped the PUT
        handler would silently stop file uploads, which is the feature users
        actually rely on (PGN access over a network drive).
        """
        monkeypatch.setattr(webapp, "WEBDAV_BASE_PATH", str(tmp_path))

        resp = client.put("/notes.txt", data=b"hello")

        assert resp.status_code in (201, 204)
        assert (tmp_path / "notes.txt").read_bytes() == b"hello"


def _fake_crypt(password, salt):
    """A deterministic stand-in for crypt(3).

    The real ``crypt`` module was removed from the standard library in Python 3.13,
    so it is absent both here and on the board's Python. Injecting a fake keeps the
    comparison logic (salt extraction, constant-time compare, rejection of unusable
    hashes) directly testable on any interpreter, instead of skipping these tests on
    the very platforms that run this code.

    Reproduces the contract the implementation depends on: the ``salt`` argument may
    be either a bare salt or a complete hash, because callers verify a password by
    re-hashing it with the stored hash as the salt. libcrypt parses the salt out of
    that prefix, so ``crypt(pw, full_hash) == full_hash`` when ``pw`` is correct --
    a fake that naively appended to its input would never match and would make a
    correct implementation look broken.
    """
    import hashlib

    if salt.startswith("$"):
        # "$<id>$<salt>[$<hash>]" -> keep the id and salt fields, drop any hash.
        fields = salt.split("$")
        prefix = f"${fields[1]}${fields[2]}"
    else:
        # Traditional DES: the salt is the first two characters.
        prefix = salt[:2]

    digest = hashlib.sha256((prefix + password).encode()).hexdigest()
    return f"{prefix}${digest}"


class TestCryptHashComparison:
    """The crypt fallback must compare hashes in constant time and stay correct."""

    def test_matching_password_is_accepted(self):
        """A correct password must still verify.

        Guards the hardening from breaking authentication outright: swapping in a
        constant-time compare is only safe if the accept/reject result is unchanged.
        """
        stored = _fake_crypt("correct horse", "$6$abcdefghij")

        assert webapp._crypt_hash_matches("correct horse", stored, crypt_fn=_fake_crypt) is True

    def test_wrong_password_is_rejected(self):
        """An incorrect password must not verify.

        How the regression manifests: a compare that returns True on length match,
        or that swallows an exception into success, would accept any password -- a
        total authentication bypass.
        """
        stored = _fake_crypt("correct horse", "$6$abcdefghij")

        assert webapp._crypt_hash_matches("wrong horse", stored, crypt_fn=_fake_crypt) is False

    def test_uses_constant_time_comparison(self, monkeypatch):
        """The comparison must go through hmac.compare_digest.

        Timing cannot be asserted reliably in a unit test, so this pins the
        mechanism instead. How the regression manifests: reverting to ``==`` means
        compare_digest is never called and this records no invocation.
        """
        stored = _fake_crypt("correct horse", "$6$abcdefghij")
        calls = []
        real = webapp.hmac.compare_digest

        def recording(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(webapp.hmac, "compare_digest", recording)

        webapp._crypt_hash_matches("correct horse", stored, crypt_fn=_fake_crypt)

        assert len(calls) == 1

    @pytest.mark.parametrize("bad_hash", ["", None, "*", "!", "x"])
    def test_unusable_hashes_are_rejected(self, bad_hash):
        """Locked, empty and shadow-sentinel hashes must never authenticate.

        ``*`` and ``!`` mark a disabled account, ``x`` means the real hash lives in
        /etc/shadow, and an empty hash means no password is set. How the regression
        manifests: treating any of these as a comparable hash could authenticate a
        disabled system account, potentially with an empty password.
        """
        assert webapp._crypt_hash_matches("anything", bad_hash, crypt_fn=_fake_crypt) is False

    def test_traditional_des_salt_is_two_characters(self):
        """A non-``$`` hash must be re-hashed with its 2-character DES salt.

        Old Pi images may still carry 13-character DES hashes. How the regression
        manifests: passing the whole hash as the salt (the modern path) produces a
        different digest, so a correct password stops verifying and the user is
        locked out of an older board.
        """
        stored = _fake_crypt("oldpass", "ab")
        seen = {}

        def recording_crypt(password, salt):
            seen["salt"] = salt
            return _fake_crypt(password, salt)

        assert webapp._crypt_hash_matches("oldpass", stored, crypt_fn=recording_crypt) is True
        assert seen["salt"] == "ab"

    def test_crypt_failure_is_not_success(self):
        """A raising hash function must yield False, never an exception or True.

        How the regression manifests: an unguarded call would 500 the auth path, and
        a bare ``except: pass`` around a pre-set ``True`` would authenticate anyone.
        """

        def boom(password, salt):
            raise ValueError("unsupported salt")

        stored = _fake_crypt("pw", "$6$salt")

        assert webapp._crypt_hash_matches("pw", stored, crypt_fn=boom) is False

    def test_missing_crypt_module_denies(self):
        """With no crypt implementation available the check must deny.

        On Python 3.13+ the module is gone, so this is the real code path on the
        board. How the regression manifests: returning True (or None treated as
        truthy by a caller) when hashing is unavailable would bypass the password
        check entirely.
        """
        assert webapp._crypt_hash_matches("pw", "$6$salt$hash", crypt_fn=None) is False


class TestPasswordPolicy:
    """The system password minimum must protect a sudo-capable account."""

    def test_minimum_is_at_least_six(self):
        """The configured minimum must not regress below six characters.

        How the regression manifests: dropping back to 4 allows a trivially
        brute-forced password on an account with SSH access and passwordless sudo
        to the root helpers.
        """
        assert webapp._MIN_PASSWORD_LENGTH >= 6

    def test_short_password_is_rejected(self, client, authed):
        """A password under the minimum must be refused with a clear error.

        How the regression manifests: accepting a 5-character password would let
        the UI set a credential weaker than the stated policy.
        """
        resp = client.post(
            "/api/system/change-password",
            json={"current_password": "oldpass", "new_password": "a" * 5},
            headers={"X-Forwarded-Proto": "https"},
        )

        assert resp.status_code == 400
        assert "at least" in resp.get_json()["error"]
