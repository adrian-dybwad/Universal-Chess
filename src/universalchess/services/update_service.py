"""Unified update service for Universal Chess.

Handles:
- Checking for updates from GitHub releases (stable and nightly)
- Downloading updates in background
- Installing updates (immediately or on next restart)
- Tracking installation source (channel)
- Persisting update state

The update state is stored in /opt/universalchess/update-state.json and includes:
- channel: "stable" | "nightly" - which release channel to follow
- pending_deb: path to downloaded .deb waiting for install, or null
- last_check: ISO timestamp of last update check
- available_version: version string if update available, or null
- current_version: currently installed version
"""

import hashlib
import hmac
import json
import os
import subprocess  # nosec B404 - only runs fixed, trusted argv lists (dpkg-query/curl/wget/systemctl and the pinned install helper); no shell, no user input
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, List

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.services.event_log import log_event
from universalchess.utils.timeutils import utcnow_iso


# Configuration
GITHUB_OWNER = "adrian-dybwad"
GITHUB_REPO = "Universal-Chess"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

STATE_FILE = Path("/opt/universalchess/update-state.json")
PENDING_DEB_DIR = Path("/opt/universalchess/pending-updates")
VERSION_FILE = Path("/opt/universalchess/VERSION")

# Release asset holding the SHA-256 digests of the other assets, published by
# .github/workflows/{release,nightly}.yml. A downloaded .deb is installed as root,
# so it is verified against this manifest before it is staged; a release without it
# cannot be verified and is refused.
CHECKSUMS_ASSET_NAME = "SHA256SUMS.txt"
# Detached signature over the manifest. The root install helper verifies it with a
# keyring shipped in the package, so the manifest can be trusted even though it is
# downloaded into a directory the service user can write.
CHECKSUMS_SIGNATURE_ASSET_NAME = f"{CHECKSUMS_ASSET_NAME}.asc"

# Name of the transient systemd unit used to run the install. Running dpkg
# inside a transient unit (its own cgroup, managed by PID 1) is the only way
# to survive the postinst restarting universal-chess.service and
# universal-chess-web.service: both run with KillMode=control-group, so any
# process inside their cgroup -- including a setsid-detached child -- is
# killed when the service is restarted. A fixed unit name also gives
# cross-process mutual exclusion: systemd-run refuses to start a second
# install while one is active.
INSTALL_UNIT = "universal-chess-update"
UPDATE_LOG = "/var/log/universal-chess-update.log"

# Root helper that performs the privileged install. The service user is granted
# passwordless sudo for exactly this path (sudoers drop-in installed by the
# package postinst); it is the only command the service can run as root. The
# helper validates the .deb, launches it into the transient unit named above,
# and clears the pending state. See scripts/install-update for the rationale.
INSTALL_HELPER = "/opt/universalchess/scripts/install-update"


class UpdateChannel(Enum):
    """Update channel selection."""
    STABLE = "stable"
    NIGHTLY = "nightly"


class UpdateCheckError(Exception):
    """The update check could not determine whether a newer release exists.

    Raised instead of returning None so callers cannot treat a failed fetch
    (no DNS, GitHub unreachable, empty payload) as "up to date". None remains
    the completed "board is current" result.
    """


class UpdateEvent(Enum):
    """Events emitted by the update service."""
    CHECKING = "checking"
    UPDATE_AVAILABLE = "update_available"
    UP_TO_DATE = "up_to_date"
    DOWNLOADING = "downloading"
    DOWNLOAD_COMPLETE = "download_complete"
    DOWNLOAD_FAILED = "download_failed"
    INSTALLING = "installing"
    INSTALL_COMPLETE = "install_complete"
    INSTALL_FAILED = "install_failed"
    ERROR = "error"


@dataclass
class ReleaseInfo:
    """Information about a GitHub release."""
    tag: str
    version: str
    name: str
    published_at: str
    is_prerelease: bool
    is_nightly: bool
    download_url: Optional[str]
    download_size: int
    body: str = ""
    # The .deb asset's published name. SHA256SUMS.txt lists assets under these
    # names, while the local copy is renamed to universal-chess_<version>_all.deb,
    # so the checksum lookup must use this rather than the local filename.
    download_name: Optional[str] = None
    # URL of the release's SHA256SUMS.txt asset. Without it the download cannot be
    # verified and is refused.
    checksums_url: Optional[str] = None
    # URL of the detached signature over SHA256SUMS.txt. The root install helper
    # verifies this against the keyring shipped in the package, which is what lets
    # it trust a manifest that was downloaded into a service-user-writable
    # directory. Without it the helper refuses to install.
    signature_url: Optional[str] = None


def parse_sha256sums(text: str, filename: str) -> Optional[str]:
    """Return the SHA-256 hex digest listed for ``filename``, or None.

    Parses the format ``sha256sum`` emits: ``<64 hex chars><space><space or '*'
    for binary mode><name>``. Names are compared by basename so a listing that
    carries a path prefix (``./name``) still matches.

    Returns None -- never raises -- for empty, malformed, or non-matching input, so
    a corrupt checksum file becomes a clean verification failure rather than a
    crash in the update thread.
    """
    if not text:
        return None

    target = os.path.basename(filename)
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        digest = digest.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        # Binary-mode listings prefix the name with '*'.
        if os.path.basename(name.lstrip("*").strip()) == target:
            return digest
    return None


def sha256_of_file(path) -> Optional[str]:
    """Return the SHA-256 hex digest of a file, or None if it cannot be read.

    Reads in chunks so a large .deb is not loaded into memory on a Pi Zero.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        log.error(f"[UpdateService] Could not hash {path}: {exc}")
        return None
    return digest.hexdigest()


@dataclass
class UpdateState:
    """Persistent update state."""
    channel: str = "stable"
    auto_update: bool = False
    pending_deb: Optional[str] = None
    last_check: Optional[str] = None
    available_version: Optional[str] = None
    available_release_tag: Optional[str] = None
    current_version: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "UpdateState":
        """Create from dictionary."""
        return cls(
            channel=data.get("channel", "stable"),
            auto_update=data.get("auto_update", False),
            pending_deb=data.get("pending_deb"),
            last_check=data.get("last_check"),
            available_version=data.get("available_version"),
            available_release_tag=data.get("available_release_tag"),
            current_version=data.get("current_version"),
        )


class UpdateService:
    """Unified update service."""
    
    def __init__(self):
        self._state: UpdateState = self._load_state()
        self._checking = False
        self._downloading = False
        self._installing = False
        self._listeners: List[Callable[[UpdateEvent, str], None]] = []
        self._lock = threading.Lock()
        
        # Update current version on init
        self._state.current_version = self.get_current_version()
        self._save_state()
        
        log.info(f"[UpdateService] Initialized: channel={self._state.channel}, version={self._state.current_version}")
    
    # =========================================================================
    # State Management
    # =========================================================================
    
    def _load_state(self) -> UpdateState:
        """Load state from disk."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    return UpdateState.from_dict(data)
            except Exception as e:
                log.warning(f"[UpdateService] Failed to load state: {e}")
        return UpdateState()
    
    def _save_state(self) -> None:
        """Save state to disk."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, 'w') as f:
                json.dump(self._state.to_dict(), f, indent=2)
        except Exception as e:
            log.error(f"[UpdateService] Failed to save state: {e}")
    
    def get_state(self) -> UpdateState:
        """Get current update state."""
        return self._state
    
    # =========================================================================
    # Event System
    # =========================================================================
    
    def add_listener(self, callback: Callable[[UpdateEvent, str], None]) -> None:
        """Add event listener."""
        self._listeners.append(callback)
    
    def remove_listener(self, callback: Callable[[UpdateEvent, str], None]) -> None:
        """Remove event listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)
    
    def _notify(self, event: UpdateEvent, message: str) -> None:
        """Notify all listeners."""
        for listener in self._listeners:
            try:
                listener(event, message)
            except Exception as e:
                log.error(f"[UpdateService] Listener error: {e}")
    
    # =========================================================================
    # Version Information
    # =========================================================================
    
    def get_current_version(self) -> str:
        """Get currently installed version."""
        # Try VERSION file first
        if VERSION_FILE.exists():
            try:
                return VERSION_FILE.read_text().strip()
            except OSError as e:
                # Fall through to the dpkg query; log so a persistent read/permission
                # problem on the VERSION file is diagnosable.
                log.debug(f"[UpdateService] Could not read {VERSION_FILE}: {e}")
        
        # Fallback to dpkg
        try:
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv, no shell
                ["dpkg-query", "-W", "-f=${Version}", "universal-chess"],  # noqa: S607
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            # dpkg-query missing or timed out; version stays "unknown".
            log.debug(f"[UpdateService] dpkg-query for version failed: {e}")
        
        return "unknown"
    
    def get_channel(self) -> UpdateChannel:
        """Get current update channel."""
        return UpdateChannel(self._state.channel)
    
    def set_channel(self, channel: UpdateChannel) -> None:
        """Set update channel."""
        self._state.channel = channel.value
        # Clear pending update when switching channels
        if self._state.pending_deb:
            self._clear_pending_update()
        self._state.available_version = None
        self._state.available_release_tag = None
        self._save_state()
        log.info(f"[UpdateService] Channel set to {channel.value}")
    
    def is_auto_update_enabled(self) -> bool:
        """Check if auto-update is enabled."""
        return self._state.auto_update

    def set_auto_update(self, enabled: bool) -> None:
        """Enable or disable auto-update."""
        self._state.auto_update = enabled
        self._save_state()
        log.info(f"[UpdateService] Auto-update {'enabled' if enabled else 'disabled'}")

    def run_startup_update_check(self) -> str:
        """Auto-update step, run once at board startup.

        Deliberately never installs: on a chess board an update must not be
        applied on its own, because the install restarts the services and would
        interrupt play. Auto-update only *stages* a build -- it checks the
        release channel and downloads the newest one in the background -- and a
        toolbar indicator then invites the user to install it from Settings ->
        System at their convenience.

        Gated on the auto-update preference (a no-op when off, so manual users
        keep full control). When an update is already downloaded and waiting to
        be installed, startup leaves it in place rather than re-downloading over
        it. The download runs on a background thread so a slow or absent network
        never stalls boot.

        Returns a short status token for logging and tests:
          - "disabled": auto-update is off; nothing done.
          - "pending":  an update is already staged; left for the user to install.
          - "checking": a background check+download was started.
        """
        if not self.is_auto_update_enabled():
            return "disabled"
        if self.has_pending_update():
            log.info("[UpdateService] Auto-update: an update is already staged; awaiting user install")
            return "pending"
        log.info("[UpdateService] Auto-update: checking and downloading in background")
        self.check_and_download_async()
        return "checking"
    
    # =========================================================================
    # Update Checking
    # =========================================================================
    
    def check_for_updates(self) -> Optional[ReleaseInfo]:
        """Check for available updates.

        Returns:
            ReleaseInfo if a newer release exists, None if the board is current.

        Raises:
            UpdateCheckError: the release list could not be fetched or compared,
                so availability is unknown. Callers must not treat this as
                up-to-date: that is what made Settings claim "latest version"
                on a board that could not reach GitHub.
        """
        if self._checking:
            log.warning("[UpdateService] Already checking for updates")
            raise UpdateCheckError("Already checking for updates")
        
        with self._lock:
            self._checking = True
        
        self._notify(UpdateEvent.CHECKING, "Checking for updates...")
        
        try:
            releases = self._fetch_releases()
            if not releases:
                self._notify(UpdateEvent.ERROR, "Could not fetch releases")
                raise UpdateCheckError("Could not fetch releases")
            
            current = self.get_current_version()
            channel = self.get_channel()
            
            log.info(f"[UpdateService] Current: {current}, Channel: {channel.value}")
            
            for release in releases:
                # Filter by channel
                if channel == UpdateChannel.STABLE:
                    if release.is_prerelease or release.is_nightly:
                        continue
                else:  # NIGHTLY
                    if not release.is_nightly:
                        continue
                
                # Check if newer
                if self._is_newer(release.version, current):
                    self._state.available_version = release.version
                    self._state.available_release_tag = release.tag
                    # UTC-designated (+00:00) so the web UI parses it as UTC and
                    # renders it in the viewer's local timezone. A bare
                    # datetime.utcnow().isoformat() is naive (no designator) and
                    # would be read as browser-local, showing the UTC digits.
                    self._state.last_check = utcnow_iso()
                    self._save_state()
                    
                    log.info(f"[UpdateService] Update available: {release.version}")
                    self._notify(UpdateEvent.UPDATE_AVAILABLE, f"v{release.version} available")
                    return release
            
            self._state.available_version = None
            self._state.available_release_tag = None
            # UTC-designated: see the update-available branch above.
            self._state.last_check = utcnow_iso()
            self._save_state()
            
            log.info("[UpdateService] Up to date")
            self._notify(UpdateEvent.UP_TO_DATE, f"Up to date (v{current})")
            return None
            
        except UpdateCheckError:
            raise
        except Exception as e:
            log.error(f"[UpdateService] Check failed: {e}")
            self._notify(UpdateEvent.ERROR, str(e))
            raise UpdateCheckError("Could not fetch releases") from e
        finally:
            with self._lock:
                self._checking = False
    
    def _fetch_releases(self) -> List[ReleaseInfo]:
        """Fetch releases from GitHub API."""
        try:
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv to a constant GitHub API URL, no shell
                ["curl", "-s", "-H", "Accept: application/vnd.github+json", GITHUB_API_URL],  # noqa: S607
                capture_output=True, text=True, timeout=30
            )
            
            if result.returncode != 0:
                log.error(f"[UpdateService] curl failed: {result.stderr}")
                return []
            
            data = json.loads(result.stdout)
            
            if isinstance(data, dict) and "message" in data:
                log.error(f"[UpdateService] GitHub API error: {data['message']}")
                return []
            
            releases = []
            for item in data[:20]:  # Check last 20 releases
                # Find the .deb asset, the checksum manifest that verifies it, and
                # the detached signature that makes the manifest trustworthy.
                # Matched independently rather than in an if/elif chain: the
                # signature's name also fails the .deb test, so chaining them makes
                # which asset wins depend on the order GitHub returns them.
                deb_asset = None
                checksums_asset = None
                signature_asset = None
                for asset in item.get("assets", []):
                    name = asset.get("name", "")
                    if deb_asset is None and name.endswith(".deb"):
                        deb_asset = asset
                    if name == CHECKSUMS_ASSET_NAME:
                        checksums_asset = asset
                    if name == CHECKSUMS_SIGNATURE_ASSET_NAME:
                        signature_asset = asset
                
                tag = item.get("tag_name", "")
                is_nightly = "nightly" in tag.lower()
                
                # Extract version from tag
                if is_nightly:
                    # nightly-2024-12-29-abc1234 -> extract base version from .deb name or use tag
                    version = tag
                else:
                    version = tag.lstrip("v")
                
                releases.append(ReleaseInfo(
                    tag=tag,
                    version=version,
                    name=item.get("name", tag),
                    published_at=item.get("published_at", ""),
                    is_prerelease=item.get("prerelease", False),
                    is_nightly=is_nightly,
                    download_url=deb_asset.get("browser_download_url") if deb_asset else None,
                    download_size=deb_asset.get("size", 0) if deb_asset else 0,
                    body=item.get("body", "")[:500],
                    download_name=deb_asset.get("name") if deb_asset else None,
                    checksums_url=(
                        checksums_asset.get("browser_download_url")
                        if checksums_asset
                        else None
                    ),
                    signature_url=(
                        signature_asset.get("browser_download_url")
                        if signature_asset
                        else None
                    ),
                ))
            
            log.debug(f"[UpdateService] Fetched {len(releases)} releases")
            return releases
            
        except Exception as e:
            log.error(f"[UpdateService] Fetch error: {e}")
            return []
    
    @staticmethod
    def _parse_nightly_tag(tag: str) -> tuple:
        """Split a nightly tag into (timestamp_digits, short_sha).

        Three tag formats are supported so a board running an older build can be
        compared against newer releases (see .github/workflows/nightly.yml):

          - date only:         ``nightly-<YYYY-MM-DD>-<short_sha>``          (legacy)
          - compact date+time: ``nightly-<YYYYMMDD>-<HHMMSS>-<short_sha>``   (interim)
          - dashed date+time:  ``nightly-<YYYY-MM-DD>-<HHMMSS>-<short_sha>`` (current)

        The last ``-`` segment is the git short sha; it has NO chronological
        order, so it is returned separately and callers must only test it for
        equality, never compare it with ``<``/``>``. Everything before the sha
        is reduced to its digits, yielding a positionally-comparable timestamp
        ("20260619" or "20260619031500"). Because only the digits are kept, the
        dashes are cosmetic: the compact and dashed date+time forms parse to the
        same stamp, so switching the workflow between them is not seen as an
        update or a downgrade. A date-only tag is treated as the start of that
        day when compared against a date+time tag (see :meth:`_is_newer`, which
        right-pads the shorter stamp with zeros).
        """
        rest = tag[len("nightly-"):]
        stamp_part, _, short_sha = rest.rpartition("-")
        stamp_digits = "".join(ch for ch in stamp_part if ch.isdigit())
        return stamp_digits, short_sha

    def _is_newer(self, new_version: str, current_version: str) -> bool:
        """Compare versions."""
        if current_version == "unknown":
            return True
        
        try:
            # Handle nightly tags like "nightly-20260619-031500-0a6a09c"
            # (and the legacy "nightly-2026-06-19-0a6a09c" form).
            if new_version.startswith("nightly-"):
                if current_version.startswith("nightly-"):
                    new_stamp, new_sha = self._parse_nightly_tag(new_version)
                    cur_stamp, cur_sha = self._parse_nightly_tag(current_version)
                    # Align the numeric stamps so they compare positionally:
                    # right-pad the shorter (a date-only stamp is the start of
                    # that day) so a date+time build on the same day correctly
                    # ranks above a legacy date-only build. The stamp is built
                    # from date (and time, in current tags), both of which sort
                    # chronologically.
                    width = max(len(new_stamp), len(cur_stamp))
                    new_stamp = new_stamp.ljust(width, "0")
                    cur_stamp = cur_stamp.ljust(width, "0")
                    if new_stamp != cur_stamp:
                        return new_stamp > cur_stamp
                    # Identical stamp: the trailing short sha has NO
                    # chronological order, so it must never be compared with
                    # </>. A different sha for the same stamp is a rebuild and
                    # therefore newer; comparing the sha lexicographically (the
                    # original bug) ordered builds at random, so a newer build
                    # whose sha sorted earlier was judged older and no update
                    # was offered.
                    return new_sha != cur_sha
                else:
                    # Comparing nightly to stable - nightlies track main and are
                    # considered newer than any stable for the nightly channel.
                    return True
            
            # Handle stable versions
            def parse_version(v: str) -> tuple:
                # Strip 'v' prefix and nightly suffix
                v = v.lstrip("v").split("-")[0]
                parts = v.split(".")
                return tuple(int(p) for p in parts if p.isdigit())
            
            new_parsed = parse_version(new_version)
            current_parsed = parse_version(current_version)
            
            return new_parsed > current_parsed
            
        except Exception as e:
            log.warning(f"[UpdateService] Version comparison error: {e}")
            return False
    
    # =========================================================================
    # Download
    # =========================================================================
    
    def download_update(self, release: Optional[ReleaseInfo] = None) -> Optional[Path]:
        """Download an update.
        
        Args:
            release: Release to download. If None, fetches the latest.
            
        Returns:
            Path to downloaded .deb, or None on failure
        """
        if self._downloading:
            log.warning("[UpdateService] Already downloading")
            return None
        
        if release is None:
            try:
                release = self.check_for_updates()
            except UpdateCheckError:
                return None
            if release is None:
                return None
        
        if not release.download_url:
            log.error("[UpdateService] No download URL")
            self._notify(UpdateEvent.ERROR, "No download available")
            return None
        
        with self._lock:
            self._downloading = True
        
        self._notify(UpdateEvent.DOWNLOADING, f"Downloading v{release.version}...")
        
        try:
            # Create pending updates directory
            PENDING_DEB_DIR.mkdir(parents=True, exist_ok=True)
            
            # Clear any existing pending update
            for f in PENDING_DEB_DIR.glob("*.deb"):
                f.unlink()
            
            # Stage under the published asset name. The signed manifest lists assets
            # under those names, and the root install helper looks up the local
            # file's basename in it -- renaming the file locally would make that
            # lookup miss and every install be refused.
            deb_filename = release.download_name or f"universal-chess_{release.version}_all.deb"
            deb_path = PENDING_DEB_DIR / deb_filename
            
            log.info(f"[UpdateService] Downloading {release.download_url}")
            
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv; URL comes from the release metadata this service fetched, no shell
                ["wget", "-q", "-O", str(deb_path), release.download_url],  # noqa: S607
                capture_output=True, text=True, timeout=600
            )
            
            if result.returncode != 0 or not deb_path.exists():
                log.error(f"[UpdateService] Download failed: {result.stderr}")
                self._notify(UpdateEvent.DOWNLOAD_FAILED, "Download failed")
                return None

            # Verify before staging. This .deb is installed as root by the pinned
            # helper, so an unverified package is arbitrary root code. Fail closed:
            # a missing manifest, a missing entry, or a mismatch all abort and
            # delete the file, and pending state is only recorded after it passes.
            if not self._verify_download(deb_path, release):
                deb_path.unlink(missing_ok=True)
                self._notify(
                    UpdateEvent.DOWNLOAD_FAILED,
                    "Update rejected: could not verify the download",
                )
                return None

            # Stage the signed manifest alongside the .deb so the root helper can
            # repeat the verification against its own keyring. This check passing
            # is not sufficient on its own: it happens in the service user's own
            # process, so it protects against a corrupted or substituted download,
            # not against that user deliberately installing something else.
            if not self._stage_verification_material(release):
                deb_path.unlink(missing_ok=True)
                self._notify(
                    UpdateEvent.DOWNLOAD_FAILED,
                    "Update rejected: release is not signed",
                )
                return None

            # Update state
            self._state.pending_deb = str(deb_path)
            self._save_state()
            
            log.info(f"[UpdateService] Downloaded {deb_path.stat().st_size} bytes")
            self._notify(UpdateEvent.DOWNLOAD_COMPLETE, f"Downloaded v{release.version}")
            
            return deb_path
            
        except Exception as e:
            log.error(f"[UpdateService] Download error: {e}")
            self._notify(UpdateEvent.DOWNLOAD_FAILED, str(e))
            return None
        finally:
            with self._lock:
                self._downloading = False
    
    def _stage_verification_material(self, release: ReleaseInfo) -> bool:
        """Place the signed manifest and its signature next to the staged .deb.

        The root install helper re-verifies the package against its own keyring
        before handing it to apt, and reads both files from the pending-updates
        directory. Staging them here keeps the download in one place; the helper
        does not trust these files, it verifies the signature over them.

        Returns False when the release publishes no signature, in which case the
        caller discards the download: shipping it would stage a package the helper
        will refuse anyway, leaving the board reporting an update it cannot install.
        """
        if not release.signature_url:
            log.error(
                f"[UpdateService] Release {release.tag} publishes no "
                f"{CHECKSUMS_SIGNATURE_ASSET_NAME}; the install helper would refuse it"
            )
            return False

        manifest = self._fetch_checksums(release.checksums_url)
        if manifest is None:
            return False

        signature = self._fetch_checksums(release.signature_url)
        if signature is None:
            log.error("[UpdateService] Could not fetch the manifest signature")
            return False

        try:
            (PENDING_DEB_DIR / CHECKSUMS_ASSET_NAME).write_text(manifest)
            (PENDING_DEB_DIR / CHECKSUMS_SIGNATURE_ASSET_NAME).write_text(signature)
        except OSError as e:
            log.error(f"[UpdateService] Could not stage verification material: {e}")
            return False

        return True

    def _fetch_checksums(self, url: str) -> Optional[str]:
        """Fetch the SHA256SUMS.txt body, or None if it cannot be retrieved.

        ``-L`` follows the redirect GitHub issues for release asset downloads;
        without it the body is the redirect page and no digest parses.
        """
        try:
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv; URL comes from the release metadata this service fetched, no shell
                ["curl", "-sSL", url],  # noqa: S607
                capture_output=True, text=True, timeout=60
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.error(f"[UpdateService] Checksum fetch failed: {exc}")
            return None

        if result.returncode != 0:
            log.error(f"[UpdateService] Checksum fetch failed: {result.stderr}")
            return None
        return result.stdout

    def _verify_download(self, deb_path, release: ReleaseInfo) -> bool:
        """Whether ``deb_path`` matches the digest published for this release.

        Fails closed on every uncertain outcome (no manifest URL, unfetchable
        manifest, asset not listed, unreadable file, digest mismatch): the
        alternative is installing an unverified package as root.

        The lookup uses the release asset's published name, not the local filename,
        because the local copy is renamed to ``universal-chess_<version>_all.deb``
        while the manifest lists the names as published.
        """
        if not release.checksums_url:
            log.error(
                f"[UpdateService] Release {release.tag} publishes no "
                f"{CHECKSUMS_ASSET_NAME}; refusing to install an unverified package"
            )
            return False

        manifest = self._fetch_checksums(release.checksums_url)
        if manifest is None:
            log.error("[UpdateService] Could not fetch checksums; refusing the update")
            return False

        asset_name = release.download_name or os.path.basename(release.download_url or "")
        expected = parse_sha256sums(manifest, asset_name)
        if not expected:
            log.error(
                f"[UpdateService] {asset_name} is not listed in "
                f"{CHECKSUMS_ASSET_NAME}; refusing the update"
            )
            return False

        actual = sha256_of_file(deb_path)
        if actual is None:
            return False

        if not hmac.compare_digest(actual, expected):
            log.error(
                f"[UpdateService] Checksum mismatch for {asset_name}: "
                f"expected {expected}, got {actual}"
            )
            return False

        log.info(f"[UpdateService] Verified {asset_name} against {CHECKSUMS_ASSET_NAME}")
        return True

    # =========================================================================
    # Installation
    # =========================================================================
    
    def has_pending_update(self) -> bool:
        """Whether a downloaded update is staged and ready to install.

        Reads the persisted state fresh so the answer is correct across
        processes: auto-update downloads run in the board process while the web
        process (which serves the "install" action and the navbar indicator)
        holds its own in-memory state. Returns True only if the recorded .deb
        still exists on disk.
        """
        return self.get_pending_update_path() is not None

    def get_pending_update_path(self) -> Optional[Path]:
        """Path to the staged pending .deb, or None if nothing is staged.

        Sourced from the persisted state (not this instance's in-memory copy) so
        a build downloaded by another process is visible here. The .deb must
        still exist on disk; a marker pointing at a missing file reads as no
        pending update.
        """
        pending_deb = self._load_state().pending_deb
        if pending_deb:
            path = Path(pending_deb)
            if path.exists():
                return path
        return None
    
    def _clear_pending_update(self) -> None:
        """Clear pending update."""
        if self._state.pending_deb:
            path = Path(self._state.pending_deb)
            if path.exists():
                try:
                    path.unlink()
                except OSError as e:
                    # Best-effort cleanup of the staged .deb; a leftover file is
                    # harmless (it is overwritten on the next download).
                    log.debug(f"[UpdateService] Could not remove staged update {path}: {e}")
        self._state.pending_deb = None
        self._save_state()
    
    def install_pending_update(self) -> bool:
        """Install the pending (already downloaded) update.

        The pending .deb is resolved from the persisted state, so this installs a
        build staged by another process too -- e.g. the web "Install" action
        installing what auto-update downloaded in the board process.

        Returns:
            True if the install was launched, False if there is no pending
            update or the launch failed. See ``install_update`` for the
            meaning of "launched".
        """
        pending = self.get_pending_update_path()
        if pending is None:
            log.error("[UpdateService] No pending update")
            return False

        return self.install_update(pending)

    def install_update(self, deb_path: Path) -> bool:
        """Install an update from a .deb file.

        The install always runs in a transient systemd unit
        (``systemd-run``) rather than inline. This is mandatory, not an
        optimization: the package's postinst restarts both
        universal-chess.service and universal-chess-web.service, and both
        run with KillMode=control-group. Any process in those cgroups --
        including a plain background/``setsid`` child -- is killed when the
        service restarts. Only a transient unit, which PID 1 places in its
        own cgroup, survives long enough for dpkg + postinst to finish.

        Because the install is detached, this method returns as soon as the
        unit is launched. It does NOT wait for the install to complete; the
        caller (web, e-paper, shutdown handler) should show an "installing,
        the board will restart" state and let the postinst restart the
        services onto the new version. Progress can be observed via
        ``is_installing`` (which queries the transient unit) and the log at
        ``UPDATE_LOG``.

        Args:
            deb_path: Path to the .deb file to install.

        Returns:
            True if the install unit was launched, False otherwise.
        """
        if not deb_path.exists():
            log.error(f"[UpdateService] .deb not found: {deb_path}")
            return False

        if self.is_installing():
            log.warning("[UpdateService] Install already in progress")
            return False

        self._notify(UpdateEvent.INSTALLING, "Installing update...")
        # The install is detached (systemd-run) and restarts the services, so it
        # cannot be timed from here; record the launch. The post-restart "service
        # started" event marks the new version coming up.
        log_event("update", f"Installing software update ({deb_path.name})", level="info")
        return self._launch_install(deb_path)

    def _is_channel_switch(self) -> bool:
        """Whether the selected channel differs from the installed version's.

        Derived by comparing the selected channel with the installed version
        string rather than tracking a flag when the channel is changed: the change
        and the install are separate user actions, potentially separated by a
        restart, so a flag would have to be persisted and could go stale. The
        installed version is the ground truth for which channel the board is
        actually on.
        """
        installed_is_nightly = "nightly" in (self.get_current_version() or "").lower()
        selected_is_nightly = self._state.channel == UpdateChannel.NIGHTLY.value
        return installed_is_nightly != selected_is_nightly

    def _launch_install(self, deb_path: Path) -> bool:
        """Launch the install via the pinned root helper.

        The service user cannot run apt/systemd-run as root directly; it is
        granted passwordless sudo for exactly one command -- the fixed helper
        at ``INSTALL_HELPER`` -- which validates the .deb and runs the install
        in the transient unit named ``INSTALL_UNIT``. Escalating through a
        general-purpose tool would be equivalent to unrestricted root, so the
        helper is the only thing the service may invoke privileged.

        The helper returns as soon as the transient unit is launched, so this
        call returns quickly and does NOT wait for the install to finish.

        Returns True if the helper launched the unit, False otherwise (for
        example, the sudo grant is missing or an install is already active).
        """
        argv = ["sudo", "-n", INSTALL_HELPER]
        if self._is_channel_switch():
            # The helper refuses a version older than the installed one, so that a
            # genuine but outdated release cannot be used to reintroduce a fixed
            # issue. Leaving the nightly channel is legitimately such a downgrade
            # (2.0.0 sorts before 2.0.0-nightly), so the intent has to be declared
            # or the user could never switch back to stable.
            argv.append("--allow-channel-switch")
        argv.append(str(deb_path))

        try:
            log.info(f"[UpdateService] Launching install helper for {deb_path}")
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv to a pinned helper path under a locked-down sudo grant, no shell
                argv,
                capture_output=True, text=True, timeout=30,
            )

            if result.returncode != 0:
                log.error(
                    f"[UpdateService] install helper failed (rc={result.returncode}): "
                    f"{result.stderr.strip()}"
                )
                self._notify(UpdateEvent.INSTALL_FAILED, "Failed to launch installer")
                return False

            log.info(f"[UpdateService] Install launched as transient unit {INSTALL_UNIT}")
            return True

        except Exception as e:
            log.error(f"[UpdateService] Failed to launch install: {e}")
            self._notify(UpdateEvent.INSTALL_FAILED, str(e))
            return False

    def install_local_deb(self, deb_path: str) -> bool:
        """Install a local .deb file.
        
        Args:
            deb_path: Path to local .deb file
            
        Returns:
            True if the install was launched, False otherwise.
        """
        path = Path(deb_path)
        if not path.exists():
            log.error(f"[UpdateService] File not found: {deb_path}")
            return False
        
        return self.install_update(path)
    
    # =========================================================================
    # Async Operations
    # =========================================================================
    
    def check_and_download_async(
        self,
        callback: Optional[Callable[[bool, Optional[str]], None]] = None
    ) -> None:
        """Check for updates and download in background.
        
        Args:
            callback: Called with (success, version) when complete
        """
        def worker():
            try:
                release = self.check_for_updates()
                if release:
                    deb_path = self.download_update(release)
                    if callback:
                        callback(deb_path is not None, release.version if deb_path else None)
                else:
                    if callback:
                        callback(False, None)
            except Exception as e:
                log.error(f"[UpdateService] Async check error: {e}")
                if callback:
                    callback(False, None)
        
        thread = threading.Thread(target=worker, name="update-check", daemon=True)
        thread.start()
    
    # =========================================================================
    # Status
    # =========================================================================
    
    def is_checking(self) -> bool:
        """Check if currently checking for updates."""
        return self._checking
    
    def is_downloading(self) -> bool:
        """Check if currently downloading."""
        return self._downloading
    
    def is_installing(self) -> bool:
        """Check if an install is currently in progress.

        Queries the transient install unit rather than an in-memory flag:
        installs are launched by either the web service or the board
        service (separate processes with separate UpdateService instances),
        so the systemd unit is the only source of truth shared across both.
        """
        try:
            result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - fixed argv, no shell
                ["systemctl", "is-active", INSTALL_UNIT],  # noqa: S607
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                return True
        except (OSError, subprocess.SubprocessError) as e:
            # systemctl unavailable/timed out; fall back to the in-process flag.
            log.debug(f"[UpdateService] systemctl is-active check failed: {e}")
        return self._installing
    
    def get_status_dict(self) -> dict:
        """Get status as dictionary (for API/UI).

        The persisted fields (channel, auto_update, pending/available, last_check)
        are read from a FRESH on-disk snapshot rather than this instance's
        in-memory ``self._state``. The board process and the web process each
        hold their own UpdateService singleton, and either may change the
        persisted state -- notably the board process stages an auto-update
        download at startup while the web process serves this status for the
        navbar's "update ready" indicator. Reading the snapshot makes the status
        reflect a change made by the other process without waiting for a service
        restart. The read is non-destructive (it does not replace ``self._state``)
        so it cannot clobber an in-flight download/check mutating this instance.

        ``is_installing`` is likewise cross-process: it queries the transient
        install unit (see :meth:`is_installing`), not the per-process
        ``self._installing`` flag. The volatile progress flags
        (``is_checking``/``is_downloading``) are intentionally per-process --
        they report activity in THIS process only.
        """
        persisted = self._load_state()
        pending_deb = persisted.pending_deb
        has_pending = bool(pending_deb) and Path(pending_deb).exists()
        return {
            "channel": persisted.channel,
            "auto_update": persisted.auto_update,
            "current_version": persisted.current_version or self.get_current_version(),
            "available_version": persisted.available_version,
            "has_pending_update": has_pending,
            "last_check": persisted.last_check,
            "is_checking": self._checking,
            "is_downloading": self._downloading,
            "is_installing": self.is_installing(),
        }


# Module singleton
_update_service: Optional[UpdateService] = None


def get_update_service() -> UpdateService:
    """Get the update service singleton."""
    global _update_service
    if _update_service is None:
        _update_service = UpdateService()
    return _update_service

