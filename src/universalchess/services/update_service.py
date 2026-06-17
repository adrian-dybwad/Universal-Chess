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

import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, List

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Configuration
GITHUB_OWNER = "adrian-dybwad"
GITHUB_REPO = "Universal-Chess"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

STATE_FILE = Path("/opt/universalchess/update-state.json")
PENDING_DEB_DIR = Path("/opt/universalchess/pending-updates")
VERSION_FILE = Path("/opt/universalchess/VERSION")

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


class UpdateChannel(Enum):
    """Update channel selection."""
    STABLE = "stable"
    NIGHTLY = "nightly"


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
            except Exception:
                pass
        
        # Fallback to dpkg
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", "universal-chess"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        
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
    
    # =========================================================================
    # Update Checking
    # =========================================================================
    
    def check_for_updates(self) -> Optional[ReleaseInfo]:
        """Check for available updates.
        
        Returns:
            ReleaseInfo if update available, None if up to date or error
        """
        if self._checking:
            log.warning("[UpdateService] Already checking for updates")
            return None
        
        with self._lock:
            self._checking = True
        
        self._notify(UpdateEvent.CHECKING, "Checking for updates...")
        
        try:
            releases = self._fetch_releases()
            if not releases:
                self._notify(UpdateEvent.ERROR, "Could not fetch releases")
                return None
            
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
                    self._state.last_check = datetime.utcnow().isoformat()
                    self._save_state()
                    
                    log.info(f"[UpdateService] Update available: {release.version}")
                    self._notify(UpdateEvent.UPDATE_AVAILABLE, f"v{release.version} available")
                    return release
            
            self._state.available_version = None
            self._state.available_release_tag = None
            self._state.last_check = datetime.utcnow().isoformat()
            self._save_state()
            
            log.info("[UpdateService] Up to date")
            self._notify(UpdateEvent.UP_TO_DATE, f"Up to date (v{current})")
            return None
            
        except Exception as e:
            log.error(f"[UpdateService] Check failed: {e}")
            self._notify(UpdateEvent.ERROR, str(e))
            return None
        finally:
            with self._lock:
                self._checking = False
    
    def _fetch_releases(self) -> List[ReleaseInfo]:
        """Fetch releases from GitHub API."""
        try:
            result = subprocess.run(
                ["curl", "-s", "-H", "Accept: application/vnd.github+json", GITHUB_API_URL],
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
                # Find .deb asset
                deb_asset = None
                for asset in item.get("assets", []):
                    if asset.get("name", "").endswith(".deb"):
                        deb_asset = asset
                        break
                
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
                ))
            
            log.debug(f"[UpdateService] Fetched {len(releases)} releases")
            return releases
            
        except Exception as e:
            log.error(f"[UpdateService] Fetch error: {e}")
            return []
    
    def _is_newer(self, new_version: str, current_version: str) -> bool:
        """Compare versions."""
        if current_version == "unknown":
            return True
        
        try:
            # Handle nightly tags like "nightly-2024-12-29-abc1234"
            if new_version.startswith("nightly-"):
                # For nightlies, compare the date portion
                if current_version.startswith("nightly-"):
                    # Both nightly - compare date+hash
                    return new_version > current_version
                else:
                    # Comparing nightly to stable - check if nightly is for newer base
                    return True  # Nightlies are considered newer than any stable
            
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
            release = self.check_for_updates()
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
            
            # Download
            deb_filename = f"universal-chess_{release.version}_all.deb"
            deb_path = PENDING_DEB_DIR / deb_filename
            
            log.info(f"[UpdateService] Downloading {release.download_url}")
            
            result = subprocess.run(
                ["wget", "-q", "-O", str(deb_path), release.download_url],
                capture_output=True, text=True, timeout=600
            )
            
            if result.returncode != 0 or not deb_path.exists():
                log.error(f"[UpdateService] Download failed: {result.stderr}")
                self._notify(UpdateEvent.DOWNLOAD_FAILED, "Download failed")
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
    
    # =========================================================================
    # Installation
    # =========================================================================
    
    def has_pending_update(self) -> bool:
        """Check if there's a pending update to install."""
        if self._state.pending_deb:
            return Path(self._state.pending_deb).exists()
        return False
    
    def get_pending_update_path(self) -> Optional[Path]:
        """Get path to pending update .deb."""
        if self._state.pending_deb:
            path = Path(self._state.pending_deb)
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
                except Exception:
                    pass
        self._state.pending_deb = None
        self._save_state()
    
    def install_pending_update(self) -> bool:
        """Install the pending (already downloaded) update.

        Returns:
            True if the install was launched, False if there is no pending
            update or the launch failed. See ``install_update`` for the
            meaning of "launched".
        """
        if not self.has_pending_update():
            log.error("[UpdateService] No pending update")
            return False

        return self.install_update(Path(self._state.pending_deb))

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
        return self._launch_install(deb_path)

    def _build_install_script(self, deb_path: Path) -> str:
        """Build the shell script run by the transient install unit.

        The script runs as root (the unit is started via sudo), so it does
        not use sudo internally. It installs the .deb, repairs dependencies
        on failure, clears the pending state, and self-deletes. It does not
        restart services -- the package postinst does that as its final
        step, and the script runs in a separate cgroup so that restart does
        not kill it mid-install.
        """
        return f"""#!/bin/sh
exec >>{UPDATE_LOG} 2>&1
DEB="{deb_path}"
echo "$(date): [update] install start: $DEB"

if dpkg -i "$DEB"; then
    echo "$(date): [update] dpkg succeeded"
else
    echo "$(date): [update] dpkg failed; running apt-get -f install"
    apt-get install -f -y || true
    if ! dpkg -i "$DEB"; then
        echo "$(date): [update] install FAILED after retry"
        rm -f "$0"
        exit 1
    fi
fi

python3 - <<'PYEOF' || true
import json, pathlib
p = pathlib.Path("{STATE_FILE}")
try:
    s = json.loads(p.read_text())
    s["pending_deb"] = None
    s["available_version"] = None
    s["available_release_tag"] = None
    p.write_text(json.dumps(s, indent=2))
except Exception:
    pass
PYEOF

echo "$(date): [update] complete"
rm -f "$0"
"""

    def _launch_install(self, deb_path: Path) -> bool:
        """Launch the install in a transient systemd unit.

        Returns True if the unit was started, False if systemd-run failed
        (for example, because an install unit is already active).
        """
        script = self._build_install_script(deb_path)
        try:
            fd, script_name = tempfile.mkstemp(prefix="uc-update-", suffix=".sh")
            os.close(fd)
            script_path = Path(script_name)
            script_path.write_text(script)
            script_path.chmod(0o755)

            log.info(f"[UpdateService] Launching install unit for {deb_path}")
            result = subprocess.run(
                [
                    "sudo", "systemd-run",
                    "--collect",
                    f"--unit={INSTALL_UNIT}",
                    "/bin/sh", str(script_path),
                ],
                capture_output=True, text=True, timeout=30,
            )

            if result.returncode != 0:
                log.error(f"[UpdateService] systemd-run failed: {result.stderr.strip()}")
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
            result = subprocess.run(
                ["systemctl", "is-active", INSTALL_UNIT],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                return True
        except Exception:
            pass
        return self._installing
    
    def get_status_dict(self) -> dict:
        """Get status as dictionary (for API/UI)."""
        return {
            "channel": self._state.channel,
            "auto_update": self._state.auto_update,
            "current_version": self._state.current_version or self.get_current_version(),
            "available_version": self._state.available_version,
            "has_pending_update": self.has_pending_update(),
            "last_check": self._state.last_check,
            "is_checking": self._checking,
            "is_downloading": self._downloading,
            "is_installing": self._installing,
        }


# Module singleton
_update_service: Optional[UpdateService] = None


def get_update_service() -> UpdateService:
    """Get the update service singleton."""
    global _update_service
    if _update_service is None:
        _update_service = UpdateService()
    return _update_service


def install_pending_update_on_startup() -> bool:
    """Check for and install a pending update.

    The install runs in a transient systemd unit (see install_update), so
    it is safe to call from any context -- it is not killed when the
    services are restarted by the postinst.

    Returns:
        True if an install was launched, False otherwise.
    """
    service = get_update_service()
    
    if service.has_pending_update():
        log.info("[UpdateService] Pending update found - installing")
        return service.install_pending_update()
    
    return False

