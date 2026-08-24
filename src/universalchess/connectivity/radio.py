"""nl80211 helpers so Wi-Fi status/scan work without wireless-tools.

Raspberry Pi OS ships ``iwlist`` / ``iwgetid`` / ``iwconfig`` (wireless-tools)
and ``rfkill`` on a PATH the service can see. Armbian on Orange Pi ships
``iw`` and ``rfkill`` in ``/sbin`` and does not install wireless-tools.
Callers that exec the Pi-OS names fail: status looks disconnected while wlan0
is associated, and Scan is empty because ``iwlist: not found``.

The reverse holds too, so the substitution is not one-way. ``iw`` is not a
declared dependency of this package, and an image without it would report a
connected board as disconnected if these helpers only spoke nl80211. Every
probe here therefore prefers ``iw`` and falls back to wireless-tools, and each
fallback is free on a board that lacks the tool -- :func:`find_tool` returns
None and nothing is spawned.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # nosec B404  # fixed-argv wireless tool invocations, never shell=True
from pathlib import Path
from typing import List, Optional

_log = logging.getLogger(__name__)

WLAN_IFACE = "wlan0"
_SBIN = ("/usr/sbin", "/sbin")

# Shape every link probe returns, so callers can treat iw and wireless-tools
# interchangeably.
_NO_LINK = {"connected": False, "ssid": "", "signal_dbm": None, "frequency": ""}


def find_tool(name: str) -> Optional[str]:
    """Return an executable path for ``name``, including /sbin (often absent from PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for root in _SBIN:
        candidate = Path(root) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def dbm_to_percent(dbm: int) -> int:
    """Map dBm to 0-100 the same way the former iwconfig path did (-30=100, -90=0)."""
    return max(0, min(100, (dbm + 90) * 100 // 60))


def parse_iw_link(text: str) -> dict:
    """Parse ``iw dev <iface> link`` into connected/ssid/signal/frequency."""
    parsed = dict(_NO_LINK)
    if not text or "Not connected" in text:
        return parsed
    ssid_match = re.search(r"^\s*SSID:\s*(.*)$", text, re.MULTILINE)
    if ssid_match:
        parsed["ssid"] = ssid_match.group(1).strip()
    if "Connected to" in text or parsed["ssid"]:
        parsed["connected"] = True
    dbm_match = re.search(r"signal:\s*(-?\d+)\s*dBm", text)
    if dbm_match:
        parsed["signal_dbm"] = int(dbm_match.group(1))
    freq_match = re.search(r"freq:\s*(\d+(?:\.\d+)?)", text)
    if freq_match:
        mhz = float(freq_match.group(1))
        parsed["frequency"] = "5 GHz" if mhz >= 3000 else "2.4 GHz"
    return parsed


def parse_iwlist_scan(text: str) -> List[dict]:
    """Parse classic ``iwlist scan`` Cell blocks (Raspberry Pi OS)."""
    networks: List[dict] = []
    seen = set()
    current_ssid: Optional[str] = None
    current_signal = 0
    current_security = ""

    def flush() -> None:
        nonlocal current_ssid, current_signal, current_security
        if current_ssid and current_ssid not in seen:
            seen.add(current_ssid)
            networks.append(
                {"ssid": current_ssid, "signal": current_signal, "security": current_security}
            )
        current_ssid = None
        current_signal = 0
        current_security = ""

    for raw_line in text.split("\n"):
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
    return networks


def parse_iw_scan(text: str) -> List[dict]:
    """Parse ``iw dev <iface> scan`` BSS blocks (nl80211 / Armbian)."""
    best: dict[str, dict] = {}
    for block in re.split(r"(?m)^BSS ", text):
        if not block.strip():
            continue
        ssid_match = re.search(r"^\s*SSID:\s*(.*)$", block, re.MULTILINE)
        if not ssid_match:
            continue
        ssid = ssid_match.group(1).strip()
        if not ssid:
            continue
        dbm = None
        dbm_match = re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", block)
        if dbm_match:
            dbm = int(float(dbm_match.group(1)))
        signal = dbm_to_percent(dbm) if dbm is not None else 0
        security = "WPA" if re.search(r"^\s*(RSN|WPA):", block, re.MULTILINE) else ""
        row = {"ssid": ssid, "signal": signal, "security": security}
        previous = best.get(ssid)
        if previous is None or row["signal"] > previous["signal"]:
            best[ssid] = row
    networks = list(best.values())
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


def parse_scan_output(text: str) -> List[dict]:
    """Parse helper stdout from either iwlist or iw scan."""
    if "Cell " in text or "ESSID:" in text:
        return parse_iwlist_scan(text)
    return parse_iw_scan(text)


def wifi_enabled(*, sysfs_rfkill: str = "/sys/class/rfkill") -> bool:
    """Whether the WLAN radio is unblocked, assuming enabled when undetermined.

    Both block kinds count: a hard-blocked radio (physical switch) is as
    unusable as a soft-blocked one, and reporting it enabled would show the
    user a radio that cannot associate.

    An undetermined answer is reported as enabled, which is what the ``iwconfig``
    era did deliberately. Neither "rfkill is missing" nor "this board has no
    rfkill entry for wlan" means the radio is off -- a board with no rfkill
    switch at all has nothing blocking it -- and returning False there would
    show Wi-Fi as disabled on a working board. Whether the board has a radio at
    all is a separate question, answered by
    :mod:`universalchess.board.wireless_capability`.
    """
    rfkill = find_tool("rfkill")
    if rfkill:
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603
                [rfkill, "list", "wifi"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                lowered = result.stdout.lower()
                return (
                    "soft blocked: yes" not in lowered
                    and "hard blocked: yes" not in lowered
                )
        except (OSError, subprocess.SubprocessError) as exc:
            # rfkill is on disk but would not run. The sysfs view below answers
            # the same question from the same kernel data, so this falls through
            # rather than failing.
            _log.debug("rfkill list failed (%s); reading sysfs instead", exc)
    root = Path(sysfs_rfkill)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return True
    for entry in entries:
        type_file = entry / "type"
        soft_file = entry / "soft"
        if not type_file.is_file() or not soft_file.is_file():
            continue
        try:
            if type_file.read_text().strip() != "wlan":
                continue
            if soft_file.read_text().strip() != "0":
                return False
            hard_file = entry / "hard"
            if hard_file.is_file() and hard_file.read_text().strip() != "0":
                return False
        except OSError as exc:
            # An rfkill node can disappear between the listing and the read (a
            # USB dongle unplugged mid-poll). Skipping leaves any remaining
            # entry to decide, which beats calling the radio blocked because one
            # node went away.
            _log.debug("unreadable rfkill entry %s (%s); skipping", entry, exc)
            continue
        return True
    return True


def _tool_stdout(name: str, *args: str) -> str:
    """Stdout of ``name args``, or empty when the tool is absent or fails."""
    tool = find_tool(name)
    if not tool:
        return ""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [tool, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def iw_link_text(iface: str = WLAN_IFACE) -> str:
    """Stdout of ``iw dev <iface> link``, or empty when iw is missing."""
    return _tool_stdout("iw", "dev", iface, "link")


def iwconfig_text(iface: str = WLAN_IFACE) -> str:
    """Stdout of ``iwconfig <iface>``, or empty when wireless-tools is absent."""
    return _tool_stdout("iwconfig", iface)


def iwgetid_ssid(iface: str = WLAN_IFACE) -> str:
    """Associated SSID from ``iwgetid -r``, or empty."""
    return _tool_stdout("iwgetid", iface, "-r").strip()


def parse_iwconfig(text: str) -> dict:
    """Parse ``iwconfig <iface>`` into the same shape as :func:`parse_iw_link`.

    wireless-tools reports an idle interface as ``ESSID:off/any`` and/or
    ``Access Point: Not-Associated``; either means not connected.
    """
    parsed = dict(_NO_LINK)
    if not text or "Not-Associated" in text:
        return parsed
    ssid_match = re.search(r'ESSID:"([^"]*)"', text)
    if not ssid_match:
        return parsed
    parsed["ssid"] = ssid_match.group(1)
    parsed["connected"] = bool(parsed["ssid"])
    dbm_match = re.search(r"Signal level[=:](-?\d+)", text)
    if dbm_match:
        parsed["signal_dbm"] = int(dbm_match.group(1))
    freq_match = re.search(r"Frequency[=:](\d+(?:\.\d+)?)\s*GHz", text)
    if freq_match:
        parsed["frequency"] = "2.4 GHz" if float(freq_match.group(1)) < 3 else "5 GHz"
    return parsed


def _merge_link(primary: dict, secondary: dict) -> dict:
    """Fill gaps in ``primary`` from ``secondary`` without downgrading it."""
    if not primary["connected"]:
        return secondary if secondary["connected"] else primary
    merged = dict(primary)
    if not merged["ssid"]:
        merged["ssid"] = secondary["ssid"]
    if merged["signal_dbm"] is None:
        merged["signal_dbm"] = secondary["signal_dbm"]
    if not merged["frequency"]:
        merged["frequency"] = secondary["frequency"]
    return merged


def link_status(iface: str = WLAN_IFACE) -> dict:
    """Association state for ``iface`` from whichever tool the image ships.

    ``iw`` (nl80211) first, because it is the one present on both images and
    the only one on Armbian. Raspberry Pi OS additionally has wireless-tools,
    which is consulted when ``iw`` answers nothing or answers incompletely.

    The fallback is not optional decoration: ``iw`` is not a declared package
    dependency, and an ``iw``-only probe reports a connected board as
    disconnected wherever it is absent -- the same defect that made status read
    "disconnected" on Armbian, mirrored onto the Pi.

    Each fallback costs nothing on a board that lacks the tool: :func:`find_tool`
    returns None and no process is spawned.
    """
    status = parse_iw_link(iw_link_text(iface))
    if not (status["connected"] and status["ssid"] and status["signal_dbm"] is not None
            and status["frequency"]):
        status = _merge_link(status, parse_iwconfig(iwconfig_text(iface)))
    if not status["connected"]:
        ssid = iwgetid_ssid(iface)
        if ssid:
            status = {**status, "connected": True, "ssid": ssid}
    return status
