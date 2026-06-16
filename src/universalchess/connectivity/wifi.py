"""UI-agnostic WiFi operations (scan, connect, saved networks, forget, status).

Pure wrappers over ``iwlist``/``nmcli``/``iwgetid`` returning plain data, with no
board or e-paper dependencies. The board's keyboard/splash UX lives in
``utils/wifi.py`` which delegates the actual system calls here; the Flask web API
calls these directly. Keeping the logic in one place means scan parsing and the
stale-profile handling cannot drift between the two surfaces.

Note on ``forget``/``connect`` and the active network: deleting or replacing the
profile of the network the caller is currently using will drop that connection.
Callers (especially the web API, which may be reaching the board over that same
WiFi) are responsible for warning the user; these functions do not refuse the
operation.
"""

import logging
import re
import subprocess
from typing import List, Optional, Tuple

_DEFAULT_LOG = logging.getLogger(__name__)

WLAN_INTERFACE = "wlan0"

# IEEE 802.11 SSIDs are at most 32 bytes; reject anything clearly invalid or
# that looks like command-line flag injection before handing it to nmcli.
_MAX_SSID_LENGTH = 64  # generous limit for multibyte encodings
_SSID_REJECT_RE = re.compile(r"[\x00-\x1f]")  # no control characters


def _validate_ssid(ssid: str) -> bool:
    """Return True if ``ssid`` is safe to pass to nmcli as a positional arg."""
    if not ssid or len(ssid) > _MAX_SSID_LENGTH:
        return False
    if _SSID_REJECT_RE.search(ssid):
        return False
    return True
_SCAN_TIMEOUT_SECONDS = 30
_CONNECT_TIMEOUT_SECONDS = 30
_NMCLI_TIMEOUT_SECONDS = 10


def _resolve_log(log: Optional[logging.Logger]) -> logging.Logger:
    return log if log is not None else _DEFAULT_LOG


def scan_networks(log: Optional[logging.Logger] = None) -> List[dict]:
    """Scan for nearby WiFi networks via ``iwlist``.

    Returns a list of ``{"ssid", "signal", "security"}`` dicts, de-duplicated by
    SSID and sorted by signal strength descending. Returns an empty list on any
    failure (the caller decides how to surface that). Blank/hidden SSIDs are
    skipped because they are not selectable.
    """
    log = _resolve_log(log)
    networks: List[dict] = []
    try:
        result = subprocess.run(
            ["sudo", "iwlist", WLAN_INTERFACE, "scan"],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.error("[WiFi] Network scan timed out")
        return networks
    except Exception as e:  # noqa: BLE001 - subprocess can raise OSError variants
        log.error(f"[WiFi] Error scanning networks: {e}")
        return networks

    if result.returncode != 0:
        log.error(f"[WiFi] iwlist failed: {result.stderr}")
        return networks

    seen_ssids = set()
    current_ssid: Optional[str] = None
    current_signal = 0
    current_security = ""

    def flush() -> None:
        nonlocal current_ssid, current_signal, current_security
        if current_ssid and current_ssid not in seen_ssids:
            seen_ssids.add(current_ssid)
            networks.append(
                {"ssid": current_ssid, "signal": current_signal, "security": current_security}
            )
        current_ssid = None
        current_signal = 0
        current_security = ""

    for raw_line in result.stdout.split("\n"):
        line = raw_line.strip()
        if line.startswith("Cell "):
            flush()
        if "ESSID:" in line:
            match = re.search(r'ESSID:"([^"]*)"', line)
            if match:
                current_ssid = match.group(1)
        if "Quality=" in line:
            match = re.search(r"Quality=(\d+)/(\d+)", line)
            if match:
                quality = int(match.group(1))
                max_quality = int(match.group(2))
                if max_quality:
                    current_signal = int((quality / max_quality) * 100)
        if "Encryption key:on" in line:
            current_security = "WPA"
    flush()

    networks.sort(key=lambda n: n["signal"], reverse=True)
    log.info(f"[WiFi] Found {len(networks)} networks")
    return networks


def get_active_ssid(log: Optional[logging.Logger] = None) -> Optional[str]:
    """Return the SSID currently associated, or None if not connected."""
    log = _resolve_log(log)
    try:
        result = subprocess.run(
            ["iwgetid", "-r"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WiFi] Failed to get active SSID: {e}")
        return None
    ssid = result.stdout.strip()
    return ssid or None


def list_saved_networks(log: Optional[logging.Logger] = None) -> List[dict]:
    """List saved NetworkManager WiFi profiles.

    Returns ``{"ssid", "active"}`` dicts where ``active`` marks the profile of the
    network currently in use, so a UI can warn before forgetting it. Returns an
    empty list on failure.
    """
    log = _resolve_log(log)
    active_ssid = get_active_ssid(log)
    saved: List[dict] = []
    try:
        listing = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            capture_output=True,
            text=True,
            timeout=_NMCLI_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WiFi] Could not list saved networks: {e}")
        return saved

    if listing.returncode != 0:
        log.debug(f"[WiFi] nmcli connection show failed: {listing.stderr}")
        return saved

    seen = set()
    for line in listing.stdout.splitlines():
        # -t output is colon-separated; the NAME may contain an escaped colon
        # ("\:"), so split off the trailing TYPE field and unescape the rest.
        parts = line.split(":")
        if len(parts) < 2:
            continue
        conn_type = parts[-1]
        name = ":".join(parts[:-1]).replace("\\:", ":")
        if "wireless" not in conn_type or name in seen:
            continue
        seen.add(name)
        saved.append({"ssid": name, "active": name == active_ssid})
    return saved


def remove_profiles(ssid: str, log: Optional[logging.Logger] = None) -> int:
    """Delete saved NetworkManager profiles whose name matches ``ssid``.

    Returns the number of profiles deleted. Matching is by exact connection name
    AND a wireless type so a non-WiFi or differently-named connection is never
    removed. Used both to clear a stale profile before a retry (a prior wrong
    password leaves a profile that nmcli then fails to *update*) and to implement
    "forget network".
    """
    log = _resolve_log(log)
    deleted = 0
    try:
        listing = subprocess.run(
            ["nmcli", "-t", "-f", "UUID,NAME,TYPE", "connection", "show"],
            capture_output=True,
            text=True,
            timeout=_NMCLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.warning("[WiFi] Timed out listing profiles to remove")
        return deleted
    except Exception as e:  # noqa: BLE001
        log.warning(f"[WiFi] Error listing profiles to remove: {e}")
        return deleted

    if listing.returncode != 0:
        log.debug(f"[WiFi] Could not list connections: {listing.stderr}")
        return deleted

    for line in listing.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        uuid = parts[0]
        conn_type = parts[-1]
        name = ":".join(parts[1:-1]).replace("\\:", ":")
        if name == ssid and "wireless" in conn_type:
            log.info(f"[WiFi] Removing profile for '{ssid}' (uuid={uuid})")
            try:
                proc = subprocess.run(
                    ["sudo", "nmcli", "connection", "delete", "uuid", uuid],
                    capture_output=True,
                    text=True,
                    timeout=_NMCLI_TIMEOUT_SECONDS,
                )
                if proc.returncode == 0:
                    deleted += 1
            except Exception as e:  # noqa: BLE001
                log.warning(f"[WiFi] Error deleting profile {uuid}: {e}")
    return deleted


def forget_network(ssid: str, log: Optional[logging.Logger] = None) -> bool:
    """Forget a saved WiFi network. Returns True if a profile was removed."""
    return remove_profiles(ssid, log) > 0


def connect_network(
    ssid: str, password: Optional[str] = None, log: Optional[logging.Logger] = None
) -> Tuple[bool, str]:
    """Connect to ``ssid`` via ``nmcli``, returning ``(success, message)``.

    Any stale saved profile for the SSID is removed first so a retry after a
    failed attempt (e.g. a mistyped password) starts from a clean, fully
    specified profile instead of failing with a "key-mgmt: property is missing"
    profile-update error. On failure the half-created profile is removed too. The
    message is a short, human-readable reason suitable for either surface.
    """
    log = _resolve_log(log)
    if not ssid:
        return False, "No network specified"
    if not _validate_ssid(ssid):
        return False, "Invalid network name"

    remove_profiles(ssid, log)

    command = ["sudo", "nmcli", "device", "wifi", "connect", ssid]  # noqa: S603
    if password:
        command += ["password", password]

    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, timeout=_CONNECT_TIMEOUT_SECONDS,
            shell=False,  # list-form: no shell injection
        )
    except subprocess.TimeoutExpired:
        log.error("[WiFi] Connection timed out")
        remove_profiles(ssid, log)
        return False, "Connection timed out"
    except Exception as e:  # noqa: BLE001
        log.error(f"[WiFi] Error connecting: {e}")
        return False, "Connection failed"

    if result.returncode == 0:
        log.info(f"[WiFi] Connected to {ssid}")
        return True, "Connected"

    stderr = (result.stderr or "").strip()
    log.error(f"[WiFi] Failed to connect: {stderr}")
    remove_profiles(ssid, log)
    return False, format_connect_error(stderr, bool(password))


def format_connect_error(stderr: str, had_password: bool) -> str:
    """Map an nmcli failure to a short, human-readable reason.

    A wrong PSK surfaces from nmcli as a "Secrets were required, but not
    provided" / "no-secrets" style message; treat that as a bad password so the
    user knows to re-enter it rather than assuming a system fault.
    """
    lowered = stderr.lower()
    if had_password and ("secret" in lowered or "no-secrets" in lowered or "802-1x" in lowered):
        return "Wrong password"
    if "property is missing" in lowered:
        # Should no longer happen now that stale profiles are removed first.
        return "Profile error, try again"
    return "Connection failed"
