"""WiFi utility functions for scan/connect/password entry."""

import subprocess
import time
import re
from typing import Callable, List, Optional

from universalchess.epaper import SplashScreen


def scan_wifi_networks(board, log) -> List[dict]:
    """Scan for available WiFi networks using iwlist."""
    networks: List[dict] = []
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message="Scanning...", leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["sudo", "iwlist", "wlan0", "scan"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        log.debug(f"[WiFi] iwlist return code: {result.returncode}")
        if result.stderr:
            log.debug(f"[WiFi] iwlist stderr: {result.stderr}")

        if result.returncode == 0:
            seen_ssids = set()
            current_ssid = None
            current_signal = 0
            current_security = ""

            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("Cell "):
                    if current_ssid and current_ssid not in seen_ssids:
                        seen_ssids.add(current_ssid)
                        networks.append(
                            {"ssid": current_ssid, "signal": current_signal, "security": current_security}
                        )
                    current_ssid = None
                    current_signal = 0
                    current_security = ""

                if "ESSID:" in line:
                    match = re.search(r'ESSID:"([^"]*)"', line)
                    if match:
                        current_ssid = match.group(1)

                if "Quality=" in line:
                    match = re.search(r"Quality=(\d+)/(\d+)", line)
                    if match:
                        quality = int(match.group(1))
                        max_quality = int(match.group(2))
                        current_signal = int((quality / max_quality) * 100)

                if "Encryption key:on" in line:
                    current_security = "WPA"

            if current_ssid and current_ssid not in seen_ssids:
                seen_ssids.add(current_ssid)
                networks.append({"ssid": current_ssid, "signal": current_signal, "security": current_security})

            networks.sort(key=lambda x: x["signal"], reverse=True)
            log.info(f"[WiFi] Found {len(networks)} networks")
        else:
            log.error(f"[WiFi] iwlist failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        log.error("[WiFi] Network scan timed out")
    except Exception as e:
        log.error(f"[WiFi] Error scanning networks: {e}")
    return networks


def remove_wifi_profiles(log, ssid: str) -> None:
    """Delete any saved NetworkManager profiles for the given SSID.

    A prior failed connect (e.g. a wrong password) leaves a saved profile named
    after the SSID. ``nmcli device wifi connect`` then tries to *update* that
    stale profile on the next attempt instead of creating a clean one, which
    fails with "802-11-wireless-security.key-mgmt: property is missing" and
    never associates. Removing matching profiles first guarantees each attempt
    starts from a clean, fully-specified profile. Deleting is safe here because
    a (re)connect immediately recreates the profile.

    Matching is by exact connection name AND a wireless type, so this never
    touches the active non-WiFi connections or a different network.
    """
    try:
        listing = subprocess.run(
            ["nmcli", "-t", "-f", "UUID,NAME,TYPE", "connection", "show"],
            capture_output=True, text=True, timeout=10,
        )
        if listing.returncode != 0:
            log.debug(f"[WiFi] Could not list connections: {listing.stderr}")
            return
        for line in listing.stdout.splitlines():
            # -t output is colon-separated; the name may itself contain an
            # escaped colon ("\:"), so split into exactly 3 fields from the ends.
            parts = line.split(":")
            if len(parts) < 3:
                continue
            uuid = parts[0]
            conn_type = parts[-1]
            name = ":".join(parts[1:-1]).replace("\\:", ":")
            if name == ssid and "wireless" in conn_type:
                log.info(f"[WiFi] Removing stale profile for '{ssid}' (uuid={uuid})")
                subprocess.run(["sudo", "nmcli", "connection", "delete", "uuid", uuid],
                               capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("[WiFi] Timed out removing stale profiles")
    except Exception as e:
        log.warning(f"[WiFi] Error removing stale profiles: {e}")


def connect_to_wifi(board, log, ssid: str, password: Optional[str] = None) -> bool:
    """Connect to a WiFi network using nmcli.

    Any stale saved profile for the SSID is removed first so that retries after
    a failed attempt (e.g. a mistyped password) start clean instead of failing
    with a "key-mgmt: property is missing" profile-update error.
    """
    try:
        board.display_manager.clear_widgets(addStatusBar=False)
        promise = board.display_manager.add_widget(
            SplashScreen(board.display_manager.update, message="Connecting...", leave_room_for_status_bar=False)
        )
        if promise:
            try:
                promise.result(timeout=5.0)
            except Exception:
                pass

        # Clear any stale profile so this attempt builds a fresh, valid one.
        remove_wifi_profiles(log, ssid)

        if password:
            result = subprocess.run(
                ["sudo", "nmcli", "device", "wifi", "connect", ssid, "password", password],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            result = subprocess.run(
                ["sudo", "nmcli", "device", "wifi", "connect", ssid],
                capture_output=True,
                text=True,
                timeout=30,
            )

        if result.returncode == 0:
            log.info(f"[WiFi] Connected to {ssid}")
            board.beep(board.SOUND_GENERAL, event_type="key_press")
            return True

        # Failure: surface a clear reason and remove the half-created profile so
        # the next attempt is not poisoned by it.
        stderr = (result.stderr or "").strip()
        log.error(f"[WiFi] Failed to connect: {stderr}")
        message = _format_connect_error(stderr, bool(password))
        remove_wifi_profiles(log, ssid)
        _show_message(board, message, hold_seconds=3.0)
        board.beep(board.SOUND_WRONG, event_type="error")
        return False
    except subprocess.TimeoutExpired:
        log.error("[WiFi] Connection timed out")
        remove_wifi_profiles(log, ssid)
        _show_message(board, "Connection\ntimed out", hold_seconds=3.0)
        board.beep(board.SOUND_WRONG, event_type="error")
        return False
    except Exception as e:
        log.error(f"[WiFi] Error connecting: {e}")
        board.beep(board.SOUND_WRONG, event_type="error")
        return False


def _format_connect_error(stderr: str, had_password: bool) -> str:
    """Map an nmcli failure to a short, board-friendly message.

    A wrong PSK surfaces from nmcli as a "Secrets were required, but not
    provided" / "no-secrets" style message; treat that as a bad password so the
    user knows to re-enter it rather than assuming a system fault.
    """
    lowered = stderr.lower()
    if had_password and ("secret" in lowered or "no-secrets" in lowered or "802-1x" in lowered):
        return "Wrong password\nTry again"
    if "property is missing" in lowered:
        # Should no longer happen now that stale profiles are removed first.
        return "Profile error\nTry again"
    return "Connection\nfailed"


def _show_message(board, message: str, hold_seconds: float = 0.0) -> None:
    """Show a full-screen status message, optionally holding it briefly."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message=message, leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    if hold_seconds > 0:
        time.sleep(hold_seconds)


def get_wifi_password_from_board(
    board,
    log,
    ssid: str,
    keyboard_factory: Callable[[Callable, str, int], object],
    set_active_keyboard: Callable[[object], None],
    clear_active_keyboard: Callable[[], None],
) -> Optional[str]:
    """Display keyboard widget to collect WiFi password."""
    log.info(f"[WiFi] Opening keyboard for password entry: {ssid}")
    board.display_manager.clear_widgets(addStatusBar=False)
    keyboard = keyboard_factory(board.display_manager.update, f"Password: {ssid[:10]}", 64)
    set_active_keyboard(keyboard)
    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    try:
        result = keyboard.wait_for_input(timeout=300.0)
        log.info(f"[WiFi] Keyboard input complete, got {'password' if result else 'cancelled'}")
        return result
    finally:
        clear_active_keyboard()

