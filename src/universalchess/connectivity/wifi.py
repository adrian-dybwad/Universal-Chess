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
import subprocess  # nosec B404  # trusted, fixed-arg nmcli/iwlist/iwgetid calls below
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
        result = subprocess.run(["sudo", "iwlist", WLAN_INTERFACE, "scan"], capture_output=True, text=True, timeout=_SCAN_TIMEOUT_SECONDS)  # noqa: S603, S607  # nosec B603 B607
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
        result = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
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
        listing = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_SECONDS)  # noqa: S607  # nosec B603 B607
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
        listing = subprocess.run(["nmcli", "-t", "-f", "UUID,NAME,TYPE", "connection", "show"], capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_SECONDS)  # noqa: S607  # nosec B603 B607
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
                proc = subprocess.run(["sudo", "nmcli", "connection", "delete", "uuid", uuid], capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_SECONDS)  # noqa: S603, S607  # nosec B603 B607
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

    WPA3-SAE fallback: the primary ``nmcli device wifi connect`` lets
    NetworkManager pick the AP's advertised security. For a WPA3 (or WPA2/WPA3
    transition) AP that is SAE, and the Raspberry Pi's Broadcom ``brcmfmac``
    driver frequently fails to complete the SAE handshake -- wpa_supplicant logs
    "Authentication ... timed out" and NetworkManager reports a no-secrets error
    that is indistinguishable from a wrong PSK (observed against a real
    ``STARLINK_5G`` AP, where a correct password was shown as "Wrong password").
    So when the auto attempt fails with an authentication/secrets error and a
    password was given, retry forcing WPA2-PSK, which a transition AP still
    accepts. The WPA2-PSK 4-way handshake actually verifies the passphrase, so a
    failure there is a genuine wrong-password (or a WPA3-only AP that refuses
    PSK) and is reported as such.
    """
    log = _resolve_log(log)
    if not ssid:
        return False, "No network specified"
    if not _validate_ssid(ssid):
        return False, "Invalid network name"

    remove_profiles(ssid, log)

    # SECURITY INVARIANT (do not break): user-supplied ssid/password are passed
    # only as individual argv elements with shell=False. This is what makes the
    # CodeQL py/command-line-injection finding a false positive and is why a
    # leading-dash SSID is safe:
    #   * shell=False + list form -> each value is exactly one argv slot; it
    #     cannot word-split into extra arguments or nmcli keywords.
    #   * nmcli global dash-options must precede the object word ("device"), so a
    #     dash-leading ssid in the `connect` positional is not reparsed as an
    #     option, and `wifi connect` has no dangerous dash-option.
    #   * password is the value of the `password` keyword, never an option.
    # The same reasoning covers _connect_wpa2_psk_fallback below, where ssid and
    # password are keyword values (con-name/ssid/wifi-sec.psk), never options.
    # If this is ever changed to shell=True or a string-built command, the
    # injection becomes real again -- re-open the finding rather than suppress it.
    command = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
    if password:
        command += ["password", password]

    try:
        # shell=False (see SECURITY INVARIANT above): each value is one argv slot.
        result = subprocess.run(command, capture_output=True, text=True, timeout=_CONNECT_TIMEOUT_SECONDS, shell=False)  # noqa: S603  # nosec B603
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

    # A secrets/auth failure with a password may be the brcmfmac SAE timeout
    # rather than a wrong key (see docstring); retry forcing WPA2-PSK before
    # trusting the "wrong password" verdict.
    if password and _is_auth_failure(stderr):
        return _connect_wpa2_psk_fallback(ssid, password, log)

    return False, format_connect_error(stderr, bool(password))


def _connect_wpa2_psk_fallback(
    ssid: str, password: str, log: logging.Logger
) -> Tuple[bool, str]:
    """Retry a failed connect by forcing a WPA2-PSK profile, bypassing WPA3-SAE.

    See :func:`connect_network` for why this exists (brcmfmac cannot complete the
    SAE handshake on many WPA3/transition APs). Builds an explicit ``wpa-psk``
    profile with PMF optional -- a WPA2/WPA3 transition AP accepts it -- and
    brings it up. Returns ``(success, message)``; on any failure the partial
    profile is removed so a later retry starts clean, and the message is derived
    from the real WPA2-PSK handshake result (which, unlike the SAE timeout, does
    validate the passphrase).
    """
    log.info(f"[WiFi] Auto connect failed as SAE; retrying '{ssid}' forcing WPA2-PSK")
    # shell=False + keyword values only (see SECURITY INVARIANT in connect_network).
    add_command = [
        "sudo", "nmcli", "connection", "add", "type", "wifi",
        "con-name", ssid, "ifname", WLAN_INTERFACE, "ssid", ssid,
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
        "wifi-sec.pmf", "2",
    ]
    up_command = ["sudo", "nmcli", "connection", "up", ssid]
    try:
        add_result = subprocess.run(add_command, capture_output=True, text=True, timeout=_NMCLI_TIMEOUT_SECONDS, shell=False)  # noqa: S603  # nosec B603
        if add_result.returncode != 0:
            stderr = (add_result.stderr or "").strip()
            log.error(f"[WiFi] WPA2-PSK profile add failed: {stderr}")
            remove_profiles(ssid, log)
            return False, format_connect_error(stderr, True)
        up_result = subprocess.run(up_command, capture_output=True, text=True, timeout=_CONNECT_TIMEOUT_SECONDS, shell=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired:
        log.error("[WiFi] WPA2-PSK fallback timed out")
        remove_profiles(ssid, log)
        return False, "Connection timed out"
    except Exception as e:  # noqa: BLE001
        log.error(f"[WiFi] WPA2-PSK fallback error: {e}")
        remove_profiles(ssid, log)
        return False, "Connection failed"

    if up_result.returncode == 0:
        log.info(f"[WiFi] Connected to {ssid} via WPA2-PSK fallback")
        return True, "Connected"

    stderr = (up_result.stderr or "").strip()
    log.error(f"[WiFi] WPA2-PSK fallback failed: {stderr}")
    remove_profiles(ssid, log)
    return False, format_connect_error(stderr, True)


def _is_auth_failure(stderr: str) -> bool:
    """Return True if an nmcli failure looks like an authentication/secrets error.

    NetworkManager emits these messages both for a genuinely wrong PSK and for a
    WPA3-SAE handshake that never completed (the brcmfmac SAE timeout);
    :func:`connect_network` uses this to decide whether the WPA2-PSK fallback is
    worth trying, and :func:`format_connect_error` uses it for the password
    verdict. Kept as one predicate so the two stay in sync.
    """
    lowered = stderr.lower()
    return "secret" in lowered or "no-secrets" in lowered or "802-1x" in lowered


def format_connect_error(stderr: str, had_password: bool) -> str:
    """Map an nmcli failure to a short, human-readable reason.

    A wrong PSK surfaces from nmcli as a "Secrets were required, but not
    provided" / "no-secrets" style message; treat that as a bad password so the
    user knows to re-enter it rather than assuming a system fault. Note this is
    only a reliable "wrong password" signal after the WPA2-PSK path has run (see
    :func:`connect_network`): the same message from a raw WPA3-SAE attempt can be
    a brcmfmac handshake timeout, not a bad key.
    """
    lowered = stderr.lower()
    if had_password and _is_auth_failure(stderr):
        return "Wrong password"
    if "property is missing" in lowered:
        # Should no longer happen now that stale profiles are removed first.
        return "Profile error, try again"
    return "Connection failed"
