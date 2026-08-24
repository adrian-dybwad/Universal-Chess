"""
WiFi status information module.

Provides functions to query WiFi adapter status and format
WiFi information for display in menus.

Supports subscribing to WiFi status changes via callbacks.
"""

import subprocess  # nosec B404  # trusted, fixed-arg network tool invocations below
import threading
import os
from typing import Optional, Tuple, Callable, List

from universalchess.paths import WIFI_ADMIN
from universalchess.connectivity import radio as wifi_radio
from universalchess.i18n import t

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Module-level subscription system
_subscribers: List[Callable[[dict], None]] = []
_monitor_thread: Optional[threading.Thread] = None
_monitor_running = False
_monitor_stop_event = threading.Event()
_last_status: Optional[dict] = None
_hook_notification_file = "/var/run/dgtcm-wifi-hook-notify"
_last_hook_mtime = 0.0


def get_wifi_status() -> dict:
    """Get current WiFi adapter status and connection information.
    
    Returns:
        Dictionary with keys:
        - enabled: bool, whether WiFi is enabled (not blocked by rfkill)
        - connected: bool, whether connected to a network
        - ssid: str, current network SSID (empty if not connected)
        - ip_address: str, IP address (empty if not connected)
        - netmask: str, subnet mask (empty if not available)
        - gateway: str, default gateway (empty if not available)
        - signal: int, signal strength percentage (0-100, 0 if not connected)
        - frequency: str, connection frequency (e.g., "2.4 GHz", empty if not connected)
        - mac_address: str, WiFi adapter MAC address
    """
    status = {
        'enabled': False,
        'connected': False,
        'ssid': '',
        'ip_address': '',
        'netmask': '',
        'gateway': '',
        'signal': 0,
        'frequency': '',
        'mac_address': '',
    }
    
    # Check rfkill / sysfs. ``rfkill`` lives in /sbin on Armbian and is not
    # on a login PATH; wireless-tools (iwgetid/iwconfig) is not installed.
    status['enabled'] = wifi_radio.wifi_enabled()

    # link_status prefers iw (the only option on Armbian) and falls back to
    # wireless-tools for signal and band on images where iw reports neither.
    link = wifi_radio.link_status()
    if link['connected'] and link['ssid']:
        status['ssid'] = link['ssid']
        status['connected'] = True
        if link['signal_dbm'] is not None:
            status['signal'] = wifi_radio.dbm_to_percent(link['signal_dbm'])
        status['frequency'] = link['frequency']

    # Get IP address and netmask via ip command
    if status['connected']:
        try:
            result = subprocess.run(['ip', '-o', '-4', 'addr', 'show', 'wlan0'],  # noqa: S607  # nosec B603 B607
                                   capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse output like: "3: wlan0    inet 192.168.1.100/24 brd 192.168.1.255 scope global wlan0"
                parts = result.stdout.split()
                for i, part in enumerate(parts):
                    if part == 'inet' and i + 1 < len(parts):
                        ip_cidr = parts[i + 1]
                        if '/' in ip_cidr:
                            ip, cidr = ip_cidr.split('/')
                            status['ip_address'] = ip
                            # Convert CIDR to netmask
                            cidr_int = int(cidr)
                            mask = (0xffffffff >> (32 - cidr_int)) << (32 - cidr_int)
                            status['netmask'] = f"{(mask >> 24) & 0xff}.{(mask >> 16) & 0xff}.{(mask >> 8) & 0xff}.{mask & 0xff}"
                        else:
                            status['ip_address'] = ip_cidr
                        break
        except Exception as e:
            log.warning(f"[WiFi] Failed to get IP address: {e}")
        
        # Fallback to hostname -I if ip command didn't work
        if not status['ip_address']:
            try:
                result = subprocess.run(['hostname', '-I'],  # noqa: S607  # nosec B603 B607
                                       capture_output=True, text=True, timeout=5)
                ips = result.stdout.strip().split()
                if ips:
                    status['ip_address'] = ips[0]
            except Exception as e:
                log.debug("[WiFi] hostname -I IP fallback failed: %s", e)
    
    # Get gateway
    try:
        result = subprocess.run(['ip', 'route', 'show', 'default'],  # noqa: S607  # nosec B603 B607
                               capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            # Parse output like: "default via 192.168.1.1 dev wlan0"
            parts = result.stdout.split()
            for i, part in enumerate(parts):
                if part == 'via' and i + 1 < len(parts):
                    status['gateway'] = parts[i + 1]
                    break
    except Exception as e:
        log.warning(f"[WiFi] Failed to get gateway: {e}")
    
    # Get MAC address
    try:
        result = subprocess.run(['cat', '/sys/class/net/wlan0/address'],  # noqa: S607  # nosec B603 B607
                               capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            status['mac_address'] = result.stdout.strip().upper()
    except Exception as e:
        log.warning(f"[WiFi] Failed to get MAC address: {e}")
    
    return status


def format_status_label(status: dict) -> str:
    """Format WiFi status into a multi-line label for display.
    
    Shows SSID, IP address, signal strength, and other connection details.
    
    Args:
        status: Dictionary from get_wifi_status()
        
    Returns:
        Multi-line string for display
    """
    lines = []
    
    if status['connected']:
        # Connected - show network details
        lines.append(status['ssid'])
        
        if status['ip_address']:
            lines.append(status['ip_address'])
        
        if status['signal'] > 0:
            lines.append(t("wifi.signal", percent=status['signal']))
        
        if status['frequency']:
            lines.append(status['frequency'])
    elif status['enabled']:
        lines.append(t("common.not_connected"))
        lines.append(t("wifi.enabled"))
    else:
        lines.append(t("wifi.disabled"))
    
    return '\n'.join(lines)


def _wifi_admin(action: str) -> bool:
    """Run the pinned ``uc-wifi-admin`` helper for a radio ``action``.

    Routes the toggle through the same passwordless helper the scan and connect
    paths use, so there is one privileged path and one NOPASSWD grant. Uses
    ``sudo -n`` so a missing grant fails fast and is logged rather than hanging on
    a password prompt, and checks the return code: the previous direct
    ``sudo rfkill`` passed no ``check`` and never read ``returncode``, so it
    returned True on any non-exception and a denied sudo was reported to the UI as
    a working switch.
    """
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607
            ['sudo', '-n', WIFI_ADMIN, action],  # noqa: S607
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip() or "unknown error"
            log.error(f"[WiFi] uc-wifi-admin {action} failed: {err}")
            return False
        log.info(f"[WiFi] Radio {action}d via uc-wifi-admin")
        return True
    except Exception as e:
        log.error(f"[WiFi] Failed to {action}: {e}")
        return False


def enable_wifi() -> bool:
    """Enable the WiFi radio. Returns command success."""
    return _wifi_admin("enable")


def disable_wifi() -> bool:
    """Disable the WiFi radio. Returns command success."""
    return _wifi_admin("disable")


def _status_changed(old: Optional[dict], new: dict) -> bool:
    """Check if WiFi status has meaningfully changed.
    
    Compares key fields to determine if subscribers should be notified.
    Only checks connection-related fields, not signal strength (which
    fluctuates constantly and would cause excessive refreshes).
    
    Args:
        old: Previous status dict (may be None on first check)
        new: Current status dict
        
    Returns:
        True if status changed, False otherwise
    """
    if old is None:
        return True
    
    # Compare only connection-related fields (not signal strength which fluctuates)
    # Signal changes are cosmetic and don't need menu refreshes
    fields_to_check = ['enabled', 'connected', 'ssid', 'ip_address']
    for field in fields_to_check:
        if old.get(field) != new.get(field):
            return True
    return False


def _notify_subscribers(status: dict) -> None:
    """Notify all subscribers of a status change.
    
    Calls each subscriber callback with the new status.
    Removes any subscribers that raise exceptions.
    
    Args:
        status: Current WiFi status dict
    """
    global _subscribers
    failed_subscribers = []
    
    for callback in _subscribers:
        try:
            callback(status)
        except Exception as e:
            log.warning(f"[WiFi] Subscriber callback failed: {e}")
            failed_subscribers.append(callback)
    
    # Remove failed subscribers
    for callback in failed_subscribers:
        try:
            _subscribers.remove(callback)
        except ValueError:
            # Already gone (e.g. a concurrent unsubscribe between notify and
            # this cleanup). Safe to ignore, but log so an unexpected double
            # removal is visible rather than silently swallowed.
            log.debug("[WiFi] Failed subscriber already removed before cleanup")


def _monitor_loop() -> None:
    """Background loop that monitors WiFi status changes.
    
    Polls every 5 seconds and also checks for dhcpcd hook notifications.
    Notifies subscribers when connection status changes (not signal strength).
    """
    global _last_status, _last_hook_mtime, _monitor_running
    
    log.debug("[WiFi] Monitor thread started")
    
    while _monitor_running:
        try:
            # Check for dhcpcd hook notification (immediate update)
            hook_notified = False
            if os.path.exists(_hook_notification_file):
                try:
                    current_mtime = os.path.getmtime(_hook_notification_file)
                    if current_mtime > _last_hook_mtime:
                        _last_hook_mtime = current_mtime
                        hook_notified = True
                        log.debug("[WiFi] dhcpcd hook notification detected")
                except Exception as e:
                    log.debug(f"[WiFi] Error checking hook notification: {e}")
            
            # Get current status
            current_status = get_wifi_status()
            
            # Check if status changed or hook notified
            if _status_changed(_last_status, current_status) or hook_notified:
                log.debug(f"[WiFi] Status changed: connected={current_status['connected']}, ssid={current_status['ssid']}")
                _last_status = current_status
                _notify_subscribers(current_status)
            
            # Sleep for 5 seconds, interruptible
            _monitor_stop_event.wait(timeout=5.0)
            _monitor_stop_event.clear()
            
        except Exception as e:
            log.error(f"[WiFi] Monitor loop error: {e}")
            _monitor_stop_event.wait(timeout=5.0)
            _monitor_stop_event.clear()
    
    log.debug("[WiFi] Monitor thread stopped")


def subscribe(callback: Callable[[dict], None]) -> None:
    """Subscribe to WiFi status change notifications.
    
    The callback will be called with the current status dict whenever
    WiFi status changes (connect, disconnect, enable, disable, signal change).
    
    Starts the monitor thread if not already running.
    
    Args:
        callback: Function to call with status dict when status changes
    """
    global _monitor_thread, _monitor_running, _subscribers
    
    if callback not in _subscribers:
        _subscribers.append(callback)
        log.debug(f"[WiFi] Subscriber added, total: {len(_subscribers)}")
    
    # Start monitor thread if not running
    if not _monitor_running:
        _monitor_running = True
        _monitor_stop_event.clear()
        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            name="wifi-monitor",
            daemon=True
        )
        _monitor_thread.start()
        log.debug("[WiFi] Monitor thread started")


def unsubscribe(callback: Callable[[dict], None]) -> None:
    """Unsubscribe from WiFi status change notifications.
    
    Stops the monitor thread if no subscribers remain.
    
    Args:
        callback: Previously subscribed callback function
    """
    global _monitor_thread, _monitor_running, _subscribers
    
    try:
        _subscribers.remove(callback)
        log.debug(f"[WiFi] Subscriber removed, remaining: {len(_subscribers)}")
    except ValueError:
        # Unsubscribe for a callback that was never subscribed. Not fatal, but
        # worth a debug line since it usually means a caller lifecycle bug
        # (double-unsubscribe, or unsubscribing something never registered).
        log.debug("[WiFi] unsubscribe() for a callback that was not subscribed")
    
    # Stop monitor thread if no subscribers
    if len(_subscribers) == 0 and _monitor_running:
        _monitor_running = False
        _monitor_stop_event.set()
        if _monitor_thread:
            _monitor_thread.join(timeout=3.0)
            if _monitor_thread.is_alive():
                log.warning("[WiFi] Monitor thread did not stop within timeout")
            _monitor_thread = None
        log.debug("[WiFi] Monitor thread stopped (no subscribers)")


def get_last_status() -> Optional[dict]:
    """Get the last cached WiFi status.
    
    Returns the most recent status from the monitor thread,
    or None if no status has been cached yet.
    
    Returns:
        Last status dict, or None if not available
    """
    return _last_status
