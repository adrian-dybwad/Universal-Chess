"""Tests for over-the-air update integrity verification.

Root cause these guard
----------------------
The updater downloaded a ``.deb`` from a GitHub release with ``wget`` and handed it
straight to the pinned root helper, which installs it with apt. Integrity rested
entirely on TLS: there was no signature and no checksum check, so anything that
could serve a substituted asset (a compromised release, a stolen token, a broken
TLS path) got arbitrary code execution as root on every board that updated.

CI has published a ``SHA256SUMS.txt`` asset alongside the ``.deb`` for every
release and nightly all along, but nothing on the device ever read it -- the
checksum existed purely for humans doing manual installs.

Verification is now mandatory and **fails closed**: a missing checksum asset, a
missing entry for the asset, or a mismatch all abort the update and delete the
downloaded file, because installing an unverified root package is worse than not
updating. The download must also never be recorded as a pending install unless it
verified, or the next boot would install the rejected file.
"""

from unittest.mock import MagicMock, patch

import pytest

import universalchess.services.update_service as us
from universalchess.services.update_service import ReleaseInfo, UpdateService

# Contents of the .deb the fake release serves, and its real SHA-256. The digest is
# computed rather than hardcoded so the fixture cannot drift out of sync with it.
DEB_BYTES = b"pretend debian package payload"
ASSET_NAME = "universal-chess_9.9.9_all.deb"


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def service(tmp_path, monkeypatch):
    """An UpdateService with every on-disk path redirected into tmp_path."""
    monkeypatch.setattr(us, "STATE_FILE", tmp_path / "update-state.json")
    monkeypatch.setattr(us, "VERSION_FILE", tmp_path / "VERSION")
    monkeypatch.setattr(us, "PENDING_DEB_DIR", tmp_path / "pending-updates")
    (tmp_path / "VERSION").write_text("1.0.0")

    with patch.object(us.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0", stderr="")
        return UpdateService()


def _release(
    checksums_url="https://example.invalid/SHA256SUMS.txt",
    signature_url="https://example.invalid/SHA256SUMS.txt.asc",
):
    return ReleaseInfo(
        tag="v9.9.9",
        version="9.9.9",
        name="9.9.9",
        published_at="",
        is_prerelease=False,
        is_nightly=False,
        download_url=f"https://example.invalid/{ASSET_NAME}",
        download_size=len(DEB_BYTES),
        download_name=ASSET_NAME,
        checksums_url=checksums_url,
        signature_url=signature_url,
    )


@pytest.fixture
def fake_download(monkeypatch):
    """Make the wget call write DEB_BYTES instead of touching the network.

    Returns a mutable dict so a test can override the bytes written (to simulate a
    tampered asset) without re-stubbing.
    """
    written = {"payload": DEB_BYTES}

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "wget":
            target = argv[argv.index("-O") + 1]
            with open(target, "wb") as handle:
                handle.write(written["payload"])
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(us.subprocess, "run", fake_run)
    return written


@pytest.fixture
def fake_checksums(monkeypatch):
    """Stub the checksum fetch. Returns a dict whose "text" a test can change."""
    served = {"text": f"{_sha256(DEB_BYTES)}  {ASSET_NAME}\n"}
    monkeypatch.setattr(
        UpdateService, "_fetch_checksums", lambda self, url: served["text"]
    )
    return served


class TestSignedManifestStaging:
    """The root install helper re-verifies the download against its own keyring.

    It reads the manifest and detached signature from the pending-updates
    directory, so the download step has to put them there and must not stage a
    package the helper is guaranteed to refuse.
    """

    def test_staged_deb_uses_the_published_asset_name(
        self, service, fake_download, fake_checksums, monkeypatch
    ):
        """The staged filename must be the asset name from the release.

        Why this test exists: the signed manifest lists assets under their
        published names, and the root helper looks up the local file's basename in
        it. The download used to rename the file to
        ``universal-chess_<version>_all.deb``, which happens to equal the asset
        name for a tagged release but not for a nightly, where the version string
        is the whole tag. Any divergence makes the helper's lookup miss and refuse
        every install -- on nightlies only, so it would pass a stable-release test.

        How a regression manifests: reinstating the rename breaks OTA on the
        nightly channel while stable keeps working.
        """
        nightly_asset = "universal-chess_2.0.0-nightly_all.deb"
        release = _release()
        release.download_name = nightly_asset
        release.download_url = f"https://example.invalid/{nightly_asset}"
        fake_checksums["text"] = f"{_sha256(DEB_BYTES)}  {nightly_asset}\n"

        result = service.download_update(release)

        assert result is not None, "a signed nightly release must be accepted"
        assert result.name == nightly_asset
        assert (us.PENDING_DEB_DIR / nightly_asset).exists()

    def test_manifest_and_signature_are_staged_beside_the_deb(
        self, service, fake_download, fake_checksums
    ):
        """Both verification inputs must land in the pending-updates directory.

        Why this test exists: the root helper refuses to install when either file
        is missing (it cannot verify, so it must not proceed). If the download does
        not stage them, every install is refused and the board reports an update it
        can never apply.

        How a regression manifests: downloads still succeed and the UI offers the
        update, but installing always fails with "missing verification input".
        """
        assert service.download_update(_release()) is not None

        manifest = us.PENDING_DEB_DIR / us.CHECKSUMS_ASSET_NAME
        signature = us.PENDING_DEB_DIR / us.CHECKSUMS_SIGNATURE_ASSET_NAME
        assert manifest.exists(), "signed manifest was not staged"
        assert signature.exists(), "manifest signature was not staged"
        assert manifest.read_text() == fake_checksums["text"]

    def test_unsigned_release_is_refused_and_leaves_nothing_staged(
        self, service, fake_download, fake_checksums
    ):
        """A release without a signature must be rejected, not staged unsigned.

        Why this test exists: the helper will refuse an unsigned release anyway, so
        staging it would leave a downloaded .deb and a pending-update marker for an
        install that can never succeed -- the board would retry it on every boot.
        Rejecting at download time keeps the failure visible and self-clearing.

        How a regression manifests: the .deb remains in pending-updates/ and the
        board loops on an uninstallable update.
        """
        assert service.download_update(_release(signature_url=None)) is None
        assert list(us.PENDING_DEB_DIR.glob("*.deb")) == []


class TestChecksumParsing:
    """The SHA256SUMS.txt parser must read the exact format sha256sum emits."""

    def test_parses_matching_filename(self):
        """A standard two-space sha256sum line must yield its digest.

        How the regression manifests: a parser that mis-splits returns None for a
        valid file, so every update is rejected and boards stop updating entirely.
        """
        text = f"{'a' * 64}  other.deb\n{'b' * 64}  {ASSET_NAME}\n"

        assert us.parse_sha256sums(text, ASSET_NAME) == "b" * 64

    def test_parses_binary_mode_marker(self):
        """sha256sum's binary-mode ``*`` prefix on the filename must be handled.

        ``sha256sum -b`` writes "<hash> *<name>". How the regression manifests: the
        leading ``*`` becomes part of the compared filename, no entry matches, and
        the update is rejected.
        """
        text = f"{'c' * 64} *{ASSET_NAME}\n"

        assert us.parse_sha256sums(text, ASSET_NAME) == "c" * 64

    def test_returns_none_when_file_absent(self):
        """A checksum file listing other files must not match ours.

        How the regression manifests: returning the first digest found would accept
        any asset whose name is not listed, defeating the check.
        """
        text = f"{'d' * 64}  something-else.deb\n"

        assert us.parse_sha256sums(text, ASSET_NAME) is None

    def test_ignores_path_prefixes(self):
        """A listed name is matched on its basename.

        CI runs sha256sum inside the release directory, but a future change could
        emit "./name" or "dir/name". Matching the basename keeps verification
        working instead of silently failing closed on every board.
        """
        text = f"{'e' * 64}  ./{ASSET_NAME}\n"

        assert us.parse_sha256sums(text, ASSET_NAME) == "e" * 64

    @pytest.mark.parametrize("text", ["", "\n", "garbage", "notahash  file.deb"])
    def test_malformed_input_returns_none(self, text):
        """Empty or malformed checksum files must not produce a digest.

        How the regression manifests: an exception here would crash the update
        thread instead of cleanly reporting a failed verification.
        """
        assert us.parse_sha256sums(text, ASSET_NAME) is None


class TestDownloadVerification:
    """download_update must verify the .deb before staging it for install."""

    def test_matching_checksum_is_accepted(self, service, fake_download, fake_checksums):
        """An asset whose digest matches must be staged normally.

        Confirms verification does not break the happy path: the fix is only
        acceptable if legitimate updates still install.
        """
        path = service.download_update(_release())

        assert path is not None
        assert path.exists()
        assert service._state.pending_deb == str(path)

    def test_tampered_payload_is_rejected(self, service, fake_download, fake_checksums):
        """A substituted asset must abort the update.

        This is the core fix. How the regression manifests: dropping verification
        returns a path for a payload that does not match the published checksum,
        which then gets installed as root by the pinned helper.
        """
        fake_download["payload"] = b"malicious replacement payload"

        assert service.download_update(_release()) is None

    def test_tampered_payload_is_deleted(self, service, fake_download, fake_checksums):
        """A rejected download must not be left on disk.

        How the regression manifests: leaving the file lets a later install path
        (or a stale pending record) pick up the unverified .deb after the rejection
        has been forgotten.
        """
        fake_download["payload"] = b"malicious replacement payload"

        service.download_update(_release())

        assert list((us.PENDING_DEB_DIR).glob("*.deb")) == []

    def test_tampered_payload_is_not_recorded_as_pending(
        self, service, fake_download, fake_checksums
    ):
        """A failed verification must leave no pending-install state.

        How the regression manifests: recording pending_deb before verifying makes
        the next boot install the rejected package -- the mismatch would be detected
        and then ignored.
        """
        fake_download["payload"] = b"malicious replacement payload"

        service.download_update(_release())

        assert service._state.pending_deb is None

    def test_missing_checksum_asset_is_rejected(self, service, fake_download):
        """A release with no SHA256SUMS.txt must not install (fail closed).

        How the regression manifests: treating a missing checksum as "skip
        verification" restores the original hole, because an attacker who can serve
        a release can also omit the checksum file.
        """
        assert service.download_update(_release(checksums_url=None)) is None

    def test_unfetchable_checksum_is_rejected(self, service, fake_download, monkeypatch):
        """A checksum file that cannot be fetched must abort the update.

        How the regression manifests: treating a network failure as success would
        let an attacker who can block just the checksum request bypass the check.
        """
        monkeypatch.setattr(UpdateService, "_fetch_checksums", lambda self, url: None)

        assert service.download_update(_release()) is None

    def test_asset_not_listed_is_rejected(self, service, fake_download, fake_checksums):
        """A checksum file that omits our asset must abort the update.

        How the regression manifests: accepting an unlisted file means a renamed
        asset skips verification while still being installed as root.
        """
        fake_checksums["text"] = f"{_sha256(DEB_BYTES)}  a-different-file.deb\n"

        assert service.download_update(_release()) is None

    def test_verification_uses_the_published_asset_name(
        self, service, fake_download, fake_checksums
    ):
        """The digest is looked up by the release asset's own name.

        The local file is renamed to universal-chess_<version>_all.deb, which need
        not equal the published asset name; SHA256SUMS.txt lists the published
        names. How the regression manifests: looking up the local filename finds no
        entry and every update fails closed.
        """
        fake_checksums["text"] = f"{_sha256(DEB_BYTES)}  {ASSET_NAME}\n"
        release = _release()
        release.download_name = ASSET_NAME

        assert service.download_update(release) is not None
