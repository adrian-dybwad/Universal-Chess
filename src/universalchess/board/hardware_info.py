"""Static hardware identity: wireless chip, firmware/OS versions, display, and OS edition.

Why this is separate from :mod:`universalchess.board.system_info`:
  ``system_info`` reports *telemetry* -- numbers that change every second (CPU,
  memory, uptime) and are polled on an interval. This module reports *identity*
  -- facts fixed for the life of a boot (which wireless part is fitted, the
  kernel/firmware versions, the e-paper panel, the OS distro and edition).
  Mixing the two would make the telemetry card re-run kernel-log parsing on
  every 5-second poll. Identity is gathered once and cached.

  The wireless part is read from the kernel log, which names the Broadcom
  stepping the advertising verdict depends on, and falls back to the part the
  board profile declares for boards whose kernel prints no part number at all.
  The firmware version comes from whichever candidate package dpkg reports as
  installed, because the package holding the radio's firmware differs per
  distribution (see :func:`find_wifi_firmware_package`).

Primary motivation -- the Bluetooth advertising health row:
  The DGT Centaur's Pi uses a Broadcom combo (Wi-Fi + Bluetooth on one die).
  Field investigation proved that on the **BCM43430B0** stepping running the
  Raspberry Pi **kernel 6.18.x** line with **BlueZ 5.82-1.1+rpt1**, LE
  advertising stops working: the identical ``RegisterAdvertisement`` call
  accepted on kernel 6.12.x is rejected with ``Invalid Parameters`` on 6.18
  (the companion app can no longer see the board). The *same* B0 die works on
  kernel 6.12.x, the older **BCM43430A1** stepping works on every kernel
  observed, and Raspberry Pi **BlueZ 5.82-1.1+rpt2** backports the upstream
  length fix, so stock advertising works on 6.18 and the install-time self-heal
  does not patch. Chip+kernel alone is therefore a false "affected". The honest
  signal is the chip stepping, the kernel version, *and* whether BlueZ is still
  the build that sends the over-long MGMT command. The scope is strictly
  Bluetooth LE advertising; the Wi-Fi STA/AP path was not shown to fail and is
  deliberately not claimed here.

  Mitigation: a 6.12.x kernel, or a BlueZ that carries the advertising-length
  fix (Raspberry Pi ``5.82-1.1+rpt2`` or later, upstream 5.85+). Universal Chess
  also applies a self-healing patch on install when stock BlueZ cannot
  advertise; that patch retires once stock works. :func:`assess_wireless_health`
  encodes exactly the proven data points and reports "unknown" for combinations
  never observed, rather than guessing.

Design mirrors ``system_info``: all OS access is isolated behind an injectable
:class:`HardwareInfoSource`, so assembly (:func:`collect_hardware_info`), the
parsers, and the health assessment are pure and unit-testable without a Pi.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404 - only runs fixed, trusted argv lists (journalctl/dmesg), never a shell, no user input
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from universalchess.board import profile, wireless_capability
from universalchess.managers import bluez_patch_status
from universalchess.paths import TMP_DIR

log = logging.getLogger(__name__)

# --- e-paper panel identity ------------------------------------------------
# The panel is fixed hardware. These mirror the active Waveshare driver
# (``epaper/framework/waveshare/epd2in9d.py``: UC8151D controller, 128x296).
# Duplicated here as plain constants so the web process can report the display
# without importing the RPi.GPIO-dependent driver module.
DISPLAY_MODEL = 'Waveshare 2.9" e-Paper (DGT Centaur V2 panel)'
DISPLAY_CONTROLLER = "UC8151D"
DISPLAY_DRIVER = "epd2in9d"
DISPLAY_INTERFACE = "SPI"
DISPLAY_WIDTH_PX = 128
DISPLAY_HEIGHT_PX = 296

# Panel model reported when the SSD1680 (V1-family) driver drove the panel. The
# physical module is the same Waveshare 2.9", but the controller distinguishes
# the DGT Centaur board generation (UC8151D=V2, SSD1680/IL3820=V1).
DISPLAY_MODEL_SSD1680 = 'Waveshare 2.9" e-Paper (DGT Centaur V1 panel)'

# Map each known controller to the (driver_module, panel_model) it implies. The
# board may fall back from the default UC8151D (V2) to the SSD1680 (V1) driver at
# startup, so the System card must report the controller that *actually* drove
# the panel -- read from the status file's ``active_controller`` -- not the
# configured default. Keys mirror display_selection.CONTROLLER_* (kept as plain
# strings so the web process needs no import from the RPi.GPIO-dependent driver
# modules).
_CONTROLLER_VARIANTS = {
    DISPLAY_CONTROLLER: (DISPLAY_DRIVER, DISPLAY_MODEL),
    "SSD1680": ("epd2in9_ssd1680", DISPLAY_MODEL_SSD1680),
}

# --- display operational status -------------------------------------------
# The panel *identity* above is fixed, but whether it actually initialized is
# runtime state the web process cannot observe directly (the board owns the SPI
# panel in a separate process). The board writes the startup outcome to this
# file (see write_display_status); the web reads it (read_display_status) so the
# System card shows whether the panel is responding -- e.g. a V1 panel that
# trips the BUSY timeout latches "failed" instead of falsely reporting "V2 OK".
DISPLAY_STATUS_FILE = f"{TMP_DIR}/display_status.json"

# Display status is a closed union; the UI renders an exhaustive mapping.
DISPLAY_OK = "ok"
DISPLAY_FAILED = "failed"
DISPLAY_UNKNOWN = "unknown"

# --- wireless health classification ---------------------------------------
# Health is a closed union; the UI renders an exhaustive mapping over it.
HEALTH_OK = "ok"
HEALTH_AFFECTED = "affected"
HEALTH_UNKNOWN = "unknown"

# The proven-broken combination. Evidence, not assumption: BCM43430B0 was
# confirmed working on 6.12.47/6.12.75 and broken on 6.18.34 *with BlueZ
# 5.82-1.1+rpt1*, where LE advertising is rejected with "Invalid Parameters".
# Raspberry Pi 5.82-1.1+rpt2 backported the upstream length fix (2a6968b); the
# same chip+kernel with that package advertises, so BlueZ is a required third
# input. Known-good recoveries: a 6.12.x kernel, a BlueZ that carries the fix,
# or the self-healing patch applied at install when stock still fails.
_AFFECTED_CHIP = "BCM43430B0"
_AFFECTED_KERNEL_MIN: tuple[int, int] = (6, 18)
_KNOWN_GOOD_KERNEL_MAX: tuple[int, int] = (6, 12)
# First upstream BlueZ that contains 2a6968b ("advertising: Fix sending extra
# bytes with MGMT_OP_ADD_EXT_ADV_DATA"). 5.82/5.83/5.84 do not.
_BLUEZ_FIX_UPSTREAM: tuple[int, int] = (5, 85)
# Raspberry Pi's 5.82-1.1+rptN line: rpt1 is the investigation baseline (faulty);
# rpt2 is the first package whose changelog names the extra-bytes fix.
_RPI_BLUEZ_RPT = re.compile(r"^5\.82-1\.1\+rpt(\d+)$")
_BLUEZ_UPSTREAM = re.compile(r"^(\d+)\.(\d+)")

# Packages whose versions are part of the wireless story and worth surfacing.
# The Wi-Fi firmware candidates are ordered most-specific-to-the-radio first, so
# a board that has both reports the package that actually feeds its radio; see
# find_wifi_firmware_package.
_WIFI_FIRMWARE_PACKAGES: tuple[str, ...] = (
    "firmware-brcm80211",
    "armbian-firmware",
    "armbian-firmware-full",
    "linux-firmware",
)
_BLUEZ_PACKAGE = "bluez"
# dpkg's status triplet for a package whose files are installed and configured.
_DPKG_INSTALLED = re.compile(r"^Status:\s*install ok installed\s*$", re.MULTILINE)


@dataclass(frozen=True)
class HardwareInfo:
    """Aggregated, boot-stable hardware identity for the System card.

    Every ``Optional`` field is ``None`` when the underlying signal could not be
    read (e.g. the kernel log is not accessible, or a package is not installed),
    never a fabricated placeholder -- a guessed chip name would drive a wrong
    "hotspot health" verdict.
    """

    pi_model: Optional[str]
    kernel_release: str
    wireless_chip: Optional[str]
    wifi_firmware_version: Optional[str]
    # The package the version came from. Which package carries the radio's
    # firmware differs per distribution, so the name travels with the version to
    # keep the card's row unambiguous.
    wifi_firmware_package: Optional[str]
    bluez_version: Optional[str]
    # Active bluetoothd stack (stock/patched/unknown) from the install-time
    # self-heal marker, surfaced so the operator sees when the board runs a
    # substituted binary that forgoes distro security updates (see
    # summarize_bluez_stack / managers.bluez_patch_status).
    bluez_stack: str
    bluez_stack_summary: str
    hotspot_health: str
    hotspot_summary: str
    display_model: str
    display_controller: str
    display_driver: str
    display_resolution: str
    display_status: str
    display_detail: str
    # Runtime facts published by the board process at startup. ``busy_timeout``
    # is True when the UC8151D init tripped the BUSY timeout (the V1-panel
    # signature) and gates the web UI's IL3820 opt-in. ``active_controller`` is
    # the controller that actually drove the panel (e.g. SSD1680 after the
    # IL3820 opt-in), or None when the display is disabled / not yet reported.
    display_busy_timeout: bool
    display_active_controller: Optional[str]
    # Distro identity for the Operating system row. ``os_pretty_name`` is
    # os-release PRETTY_NAME, rewritten to "Raspberry Pi OS {VERSION}" when
    # the board is Raspberry Pi OS (that distro's 64-bit image still IDs as
    # Debian). ``os_variant`` is Lite/Desktop/Full/Server/Minimal when a signal
    # actually names the edition; None rather than a guessed "Lite" on a
    # generic headless Debian.
    os_pretty_name: Optional[str]
    os_variant: Optional[str]

    def to_dict(self) -> dict:
        """Flat, JSON-serializable contract consumed by the React System card."""
        return {
            "pi_model": self.pi_model,
            "kernel_release": self.kernel_release,
            "os_pretty_name": self.os_pretty_name,
            "os_variant": self.os_variant,
            "wireless_chip": self.wireless_chip,
            "wifi_firmware_version": self.wifi_firmware_version,
            "wifi_firmware_package": self.wifi_firmware_package,
            "bluez_version": self.bluez_version,
            "bluez_stack": self.bluez_stack,
            "bluez_stack_summary": self.bluez_stack_summary,
            "hotspot_health": self.hotspot_health,
            "hotspot_summary": self.hotspot_summary,
            "display_model": self.display_model,
            "display_controller": self.display_controller,
            "display_driver": self.display_driver,
            "display_resolution": self.display_resolution,
            "display_status": self.display_status,
            "display_detail": self.display_detail,
            "display_busy_timeout": self.display_busy_timeout,
            "display_active_controller": self.display_active_controller,
        }


@dataclass(frozen=True)
class HardwareInfoSource:
    """Injectable boundary between assembly and OS side effects.

    Each reader returns raw text (or a release string); the pure parsers below
    turn that into structured fields. Tests pass canned strings; production uses
    :func:`default_source`.
    """

    pi_model: Callable[[], Optional[str]]
    kernel_release: Callable[[], str]
    kernel_log: Callable[[], str]
    dpkg_status: Callable[[], str]
    display_status: Callable[[], Optional[dict]]
    bluez_patch: Callable[[], dict]
    # The radio part this board's profile declares, or None when the profile
    # makes no claim. Used only when the kernel log names no part (see
    # collect_hardware_info).
    declared_wireless_chip: Callable[[], Optional[str]]
    # Raw /etc/os-release, /etc/rpi-issue, and the systemd default-target unit
    # name (e.g. multi-user.target). Empty string when the file is absent.
    os_release: Callable[[], str]
    rpi_issue: Callable[[], str]
    systemd_default_target: Callable[[], str]


# ---------------------------------------------------------------------------
# Pure parsers / classifier
# ---------------------------------------------------------------------------

# Match a Broadcom part with a stepping suffix first (e.g. "BCM43430B0"), since
# that carries the A1-vs-B0 distinction the health verdict hinges on. The bare
# form ("BCM43430", as printed on the brcmfmac line) is the fallback.
_CHIP_WITH_STEPPING = re.compile(r"\bBCM\d{4,5}[A-Z]\d\b")
_CHIP_BARE = re.compile(r"\bBCM\d{4,5}\b")


def parse_wireless_chip(kernel_log: str) -> Optional[str]:
    """Extract the Broadcom wireless chip model from kernel-log text.

    The Bluetooth (btbcm) init line prints the precise stepping, e.g.
    ``Bluetooth: hci0: BCM43430B0``. Returns that full model when present,
    otherwise the bare family (``BCM43430``) from the brcmfmac firmware line,
    otherwise ``None``. ``None`` (never a guess) keeps the health verdict honest.
    """
    if not kernel_log:
        return None
    stepping = _CHIP_WITH_STEPPING.search(kernel_log)
    if stepping:
        return stepping.group(0)
    bare = _CHIP_BARE.search(kernel_log)
    return bare.group(0) if bare else None


def parse_dpkg_version(dpkg_status: str, package: str) -> Optional[str]:
    """Read one package's installed version from ``/var/lib/dpkg/status`` text.

    Parses the world-readable dpkg status file directly (no subprocess) by
    locating the ``Package: <name>`` stanza and returning its ``Version:`` line.
    Returns ``None`` if the package is absent. Guards against a substring match
    (``bluez`` vs ``bluez-tools``) by requiring an exact package-name line.

    A stanza is not evidence that the files are on the board: dpkg keeps the
    stanza, ``Version:`` included, for a package removed without being purged,
    and lists packages it merely knows about with no version at all (an Orange Pi
    lists ``firmware-brcm80211`` this way). Only ``install ok installed`` counts,
    so a leftover stanza cannot report a version for software that is gone.
    """
    if not dpkg_status:
        return None
    # Stanzas are separated by blank lines; scan for the exact package stanza.
    for stanza in dpkg_status.split("\n\n"):
        if not re.search(
            rf"^Package:\s*{re.escape(package)}\s*$", stanza, re.MULTILINE
        ):
            continue
        if not _DPKG_INSTALLED.search(stanza):
            return None
        version = re.search(r"^Version:\s*(.+?)\s*$", stanza, re.MULTILINE)
        return version.group(1) if version else None
    return None


# os-release KEY=value; PRETTY_NAME is quoted, ID often is not.
_OS_RELEASE_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
# pi-gen writes "stageN" on the Generated-using line of /etc/rpi-issue.
_RPI_ISSUE_STAGE = re.compile(r"\bstage(\d)\b")
# pi-gen stages: 0–2 Lite, 3–4 Desktop, 5+ Full (desktop + recommended software).
_PIGEN_DESKTOP_MIN_STAGE = 3
_PIGEN_FULL_MIN_STAGE = 5

# Packages that mean a graphical session is installed (Raspberry Pi OS Desktop
# plus the common Debian/Armbian display managers and desktop environments).
_DESKTOP_PACKAGES: tuple[str, ...] = (
    "raspberrypi-ui-mods",
    "lightdm",
    "gdm3",
    "sddm",
    "lxde-core",
    "lxqt-session",
    "xfce4-session",
    "gnome-session",
    "plasma-desktop",
    "labwc",
    "wayfire",
    "weston",
)
# Packages that exist on Raspberry Pi OS (Lite and Desktop) and not on a
# generic Debian/Armbian image. Used to rewrite Debian's PRETTY_NAME and to
# label a headless Pi OS image Lite.
_RASPI_OS_PACKAGES: tuple[str, ...] = (
    "raspberrypi-sys-mods",
    "raspi-config",
    "raspberrypi-archive-keyring",
)
# VARIANT / VARIANT_ID aliases. Unknown strings pass through unchanged.
_VARIANT_ALIASES: dict[str, str] = {
    "desktop": "Desktop",
    "server": "Server",
    "minimal": "Minimal",
    "lite": "Lite",
    "full": "Full",
    "cli": "Minimal",
    "min": "Minimal",
    "xfce": "Desktop",
    "gnome": "Desktop",
    "kde": "Desktop",
    "cinnamon": "Desktop",
    "mate": "Desktop",
    "lxde": "Desktop",
    "lxqt": "Desktop",
    "i3": "Desktop",
    "sway": "Desktop",
    "labwc": "Desktop",
    "wayfire": "Desktop",
}


def parse_os_release(text: str) -> dict[str, str]:
    """Parse ``/etc/os-release`` KEY=value text into a dict.

    Values may be bare, single-quoted, or double-quoted. Comments and blank
    lines are skipped. Returns an empty dict for empty or unparseable text --
    never invented keys -- so a missing file cannot fabricate a distro name.
    """
    parsed: dict[str, str] = {}
    if not text:
        return parsed
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _OS_RELEASE_LINE.match(line)
        if not match:
            continue
        parsed[match.group(1)] = _unquote_os_release_value(match.group(2))
    return parsed


def _unquote_os_release_value(value: str) -> str:
    """Strip matching quotes from an os-release value; leave bare values intact."""
    quote = value[:1]
    if quote in "\"'" and value.endswith(quote) and len(value) > 1:
        inner = value[1:-1]
        if quote == '"':
            return inner.replace("\\\\", "\\").replace('\\"', '"')
        return inner
    return value


def parse_rpi_issue_stage(rpi_issue: str) -> Optional[int]:
    """Return the pi-gen stage number from ``/etc/rpi-issue``, or ``None``.

    stage2 is Raspberry Pi OS Lite, stage3/4 Desktop, stage5 Full. ``None``
    when the file is absent or has no stage token -- the caller must not
    invent Lite vs Desktop from an empty string.
    """
    if not rpi_issue:
        return None
    match = _RPI_ISSUE_STAGE.search(rpi_issue)
    return int(match.group(1)) if match else None


def derive_os_identity(
    os_release_text: str,
    dpkg_status: str,
    default_target: str,
    rpi_issue: str,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(pretty_name, variant)`` from injected OS identity files.

    Priority for variant (first match wins), because no single file is
    reliable across the boards this product runs on:

    1. os-release ``VARIANT`` / ``VARIANT_ID`` -- Armbian declares Server or
       Desktop here; Raspberry Pi OS does not set it.
    2. An installed desktop package (``raspberrypi-ui-mods``, a display
       manager, a DE session). Current state: a Lite image that later gained
       a desktop must show Desktop, not the original pi-gen stage.
    3. pi-gen stage in ``/etc/rpi-issue`` -- Lite / Desktop / Full for
       Raspberry Pi OS when packages do not already decide.
    4. systemd ``graphical.target`` -- Desktop only; ``multi-user.target``
       is not mapped to Lite, because that is also a generic Debian server.
    5. Raspberry Pi OS with none of the above -- Lite.

    Pretty name is os-release ``PRETTY_NAME``, rewritten to
    ``Raspberry Pi OS {VERSION}`` when the board is Raspberry Pi OS (64-bit
    images still ID as Debian). A generic Debian/Armbian pretty name is left
    alone. Either field is ``None`` when there is no signal, never a guessed
    distro or a "Lite" label on a headless Debian that is not Pi OS.
    """
    os_release = parse_os_release(os_release_text)
    is_raspi = _is_raspberry_pi_os(os_release, dpkg_status, rpi_issue)
    pretty = _os_pretty_name(os_release, is_raspi=is_raspi)
    variant = _os_variant(
        os_release,
        dpkg_status,
        default_target,
        rpi_issue,
        is_raspi=is_raspi,
    )
    return pretty, variant


def _is_raspberry_pi_os(
    os_release: dict[str, str], dpkg_status: str, rpi_issue: str
) -> bool:
    distro_id = os_release.get("ID", "").lower()
    if distro_id in {"raspbian", "raspios"}:
        return True
    if rpi_issue.strip():
        return True
    return any(
        parse_dpkg_version(dpkg_status, package) is not None
        for package in _RASPI_OS_PACKAGES
    )


def _os_pretty_name(os_release: dict[str, str], *, is_raspi: bool) -> Optional[str]:
    pretty = os_release.get("PRETTY_NAME") or None
    version = os_release.get("VERSION") or None
    name = os_release.get("NAME") or None
    if is_raspi:
        if version:
            return f"Raspberry Pi OS {version}"
        return "Raspberry Pi OS"
    if pretty:
        return pretty
    if name and version:
        return f"{name} {version}"
    return name


def _variant_from_pigen_stage(stage: Optional[int]) -> Optional[str]:
    """Map a pi-gen stage number to Lite/Desktop/Full, or ``None`` if unknown."""
    if stage is None:
        return None
    if stage >= _PIGEN_FULL_MIN_STAGE:
        return "Full"
    if stage >= _PIGEN_DESKTOP_MIN_STAGE:
        return "Desktop"
    return "Lite"


def _os_variant(
    os_release: dict[str, str],
    dpkg_status: str,
    default_target: str,
    rpi_issue: str,
    *,
    is_raspi: bool,
) -> Optional[str]:
    declared = os_release.get("VARIANT") or os_release.get("VARIANT_ID")
    if declared:
        return _normalize_os_variant(declared) or None

    desktop_installed = any(
        parse_dpkg_version(dpkg_status, package) is not None
        for package in _DESKTOP_PACKAGES
    )
    from_stage = _variant_from_pigen_stage(parse_rpi_issue_stage(rpi_issue))
    if desktop_installed:
        return "Full" if from_stage == "Full" else "Desktop"
    if from_stage is not None:
        return from_stage

    target = Path(default_target.strip()).name if default_target.strip() else ""
    if target == "graphical.target":
        return "Desktop"
    if is_raspi:
        return "Lite"
    return None


def _normalize_os_variant(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return stripped
    return _VARIANT_ALIASES.get(stripped.lower(), stripped)


def find_wifi_firmware_package(dpkg_status: str) -> tuple[Optional[str], Optional[str]]:
    """The installed package supplying the Wi-Fi firmware, as ``(name, version)``.

    Which package holds the radio's firmware is distribution-specific, so the
    candidates are tried in order of how specific they are to the radio: a
    Raspberry Pi carries it in ``firmware-brcm80211``, while Armbian ships one
    tree for the whole board (an Orange Pi Zero 2W loads
    ``/lib/firmware/uwe5622/wcnmodem.bin`` from ``armbian-firmware``). Reading
    only the Broadcom package left the row blank on every non-Pi board.

    Returns ``(None, None)`` when no candidate is installed, rather than naming a
    package at a guessed version. The name is returned with the version because
    the version alone does not say which firmware it describes.
    """
    for package in _WIFI_FIRMWARE_PACKAGES:
        version = parse_dpkg_version(dpkg_status, package)
        if version:
            return package, version
    return None, None


def classify_bluez_ext_adv_fix(bluez_version: Optional[str]) -> Optional[bool]:
    """Whether this BlueZ package is known to carry the advertising-length fix.

    Returns ``True`` if the package is known to include upstream ``2a6968b``
    (or Raspberry Pi's backport of it), ``False`` if it is known to still send
    the over-long ``MGMT_OP_ADD_EXT_ADV_DATA`` command, and ``None`` when this
    package has not been classified. ``None`` is the honest answer for an
    unread version or an older BlueZ that was never part of the investigation;
    the health classifier must not invent affected/ok from that.

    Version strings are matched as dpkg reports them. The Raspberry Pi
    ``5.82-1.1+rptN`` line is classified by revision (rpt1 faulty, rpt2+
    fixed) rather than by the upstream 5.82 number, because rpt2 is still
    labelled 5.82 and a 5.82-wide "faulty" rule would keep warning after the
    distro already shipped the fix.
    """
    if not bluez_version:
        return None
    version = bluez_version.strip()
    if not version:
        return None
    rpt = _RPI_BLUEZ_RPT.match(version)
    if rpt:
        return int(rpt.group(1)) >= 2
    upstream = _BLUEZ_UPSTREAM.match(version)
    if not upstream:
        return None
    major_minor = (int(upstream.group(1)), int(upstream.group(2)))
    if major_minor >= _BLUEZ_FIX_UPSTREAM:
        return True
    if major_minor in ((5, 82), (5, 83), (5, 84)):
        return False
    return None


def parse_kernel_tuple(kernel_release: str) -> Optional[tuple[int, int]]:
    """Parse the leading ``major.minor`` from a kernel release string.

    ``"6.18.34+rpt-rpi-v7"`` -> ``(6, 18)``. Returns ``None`` when the string
    does not start with two dotted integers, so the classifier can fall back to
    "unknown" instead of mis-parsing.
    """
    match = re.match(r"^(\d+)\.(\d+)", kernel_release or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def assess_wireless_health(
    wireless_chip: Optional[str],
    kernel_release: str,
    bluez_version: Optional[str] = None,
    bluez_stack: Optional[str] = None,
) -> tuple[str, str]:
    """Classify Bluetooth LE advertising reliability for this chip+kernel+BlueZ.

    Returns ``(health, human_summary)`` where ``health`` is one of
    :data:`HEALTH_OK`, :data:`HEALTH_AFFECTED`, :data:`HEALTH_UNKNOWN`.

    The proven failure is BCM43430B0 + kernel 6.18+ *and* a BlueZ that still
    sends the over-long extended-advertising MGMT command. Chip+kernel without
    a faulty BlueZ is not that failure -- Raspberry Pi ``5.82-1.1+rpt2``
    advertises on 6.18, and a patched ``bluetoothd`` does too. Only proven data
    points drive an ``ok``/``affected`` verdict; every other combination is
    ``unknown`` so the card never asserts a result that was not actually
    observed (see module docstring for the evidence).
    """
    if not wireless_chip:
        return (
            HEALTH_UNKNOWN,
            "Wireless chip could not be identified, so Bluetooth advertising "
            "reliability is unknown.",
        )

    if wireless_chip != _AFFECTED_CHIP:
        # Any other identified Broadcom stepping (notably the older BCM43430A1)
        # has no reported Bluetooth advertising fault in this project's testing.
        return (
            HEALTH_OK,
            f"{wireless_chip}: no known Bluetooth advertising issue on this chip.",
        )

    kernel = parse_kernel_tuple(kernel_release)
    if kernel is None:
        return (
            HEALTH_UNKNOWN,
            f"{wireless_chip} fitted, but the kernel version could not be read, "
            "so Bluetooth advertising reliability is unknown.",
        )

    if kernel >= _AFFECTED_KERNEL_MIN:
        # Running patched bluetoothd already has the length fix, even when dpkg
        # still names the faulty package the binary was built from.
        if bluez_stack == bluez_patch_status.STACK_PATCHED:
            return (
                HEALTH_OK,
                f"{wireless_chip} on kernel {kernel_release}: Bluetooth "
                "advertising was restored by a patched bluetoothd.",
            )
        has_fix = classify_bluez_ext_adv_fix(bluez_version)
        if has_fix is True:
            return (
                HEALTH_OK,
                f"{wireless_chip} on kernel {kernel_release} with BlueZ "
                f"{bluez_version}: this BlueZ carries the advertising-length "
                "fix, so no Bluetooth advertising issue is expected.",
            )
        if has_fix is False:
            return (
                HEALTH_AFFECTED,
                f"{wireless_chip} on kernel {kernel_release} with BlueZ "
                f"{bluez_version} has a known fault: Bluetooth advertising can "
                "stop working. The fault needs this chip, a 6.18+ kernel, and a "
                "BlueZ that still sends the over-long advertising command. A "
                "6.12.x kernel, or BlueZ 5.82-1.1+rpt2 or later, resolves it; "
                "installing Universal Chess also applies a self-healing patch "
                "that retires once stock BlueZ works.",
            )
        return (
            HEALTH_UNKNOWN,
            f"{wireless_chip} on kernel {kernel_release}: Bluetooth advertising "
            "reliability also depends on whether BlueZ still sends the over-long "
            "advertising command, and that BlueZ package could not be classified.",
        )
    if kernel <= _KNOWN_GOOD_KERNEL_MAX:
        return (
            HEALTH_OK,
            f"{wireless_chip} on kernel {kernel_release}: the known-good kernel "
            "for this chip; no Bluetooth advertising issue expected.",
        )
    return (
        HEALTH_UNKNOWN,
        f"{wireless_chip} on kernel {kernel_release}: this combination has not "
        "been verified, so Bluetooth advertising reliability is unknown.",
    )


def summarize_bluez_stack(stack_status: Optional[dict]) -> tuple[str, str]:
    """Classify the active ``bluetoothd`` as stock/patched/unknown for the card.

    Returns ``(stack, summary)`` where ``stack`` is one of
    ``bluez_patch_status.STACK_STOCK``/``STACK_PATCHED``/``STACK_UNKNOWN`` (the
    same closed set the install-time marker uses) and ``summary`` is a one-line
    human description for the System card.

    Only ``patched`` is a warning: a locally rebuilt ``bluetoothd`` carries a
    pre-release fix but stops receiving distribution security updates until it is
    rebuilt or retired, so the summary states that plainly (and appends the
    marker's ``reason`` when present). ``stock`` and ``unknown`` are
    non-alarming. A missing/malformed status degrades to ``unknown`` rather than
    asserting ``stock`` -- an absent marker means "not determined", not "stock".
    """
    status = stack_status if isinstance(stack_status, dict) else {}
    active = status.get("active", bluez_patch_status.STACK_UNKNOWN)
    if active == bluez_patch_status.STACK_PATCHED:
        base = status.get("base_version")
        lead = "Non-stock bluetoothd (pre-release fix)"
        if base:
            lead += f" based on BlueZ {base}"
        summary = (
            f"{lead}. Does not receive distribution security updates until it is "
            "rebuilt or retired by an app update."
        )
        reason = status.get("reason")
        if reason:
            summary += f" {reason}"
        return bluez_patch_status.STACK_PATCHED, summary
    if active == bluez_patch_status.STACK_STOCK:
        return bluez_patch_status.STACK_STOCK, "Stock distribution bluetoothd."
    return bluez_patch_status.STACK_UNKNOWN, "BlueZ stack not determined."


def format_resolution(width_px: int, height_px: int) -> str:
    """Pixel resolution as ``"128 x 296"``."""
    return f"{width_px} x {height_px}"


def derive_display_status(status_raw: Optional[dict]) -> tuple[str, str]:
    """Map the board-written status record to ``(display_status, detail)``.

    ``None`` (no record yet, or unreadable) -> ``unknown``: the board has not
    reported, so the card must not assert the panel is working. A record with
    ``initialized`` truthy -> ``ok``; otherwise ``failed`` with the board's error
    (e.g. the BUSY-timeout message for a V1 / unresponsive panel).
    """
    if status_raw is None:
        return DISPLAY_UNKNOWN, "Display status not yet reported by the board."
    if status_raw.get("initialized"):
        return DISPLAY_OK, "Panel initialized and responding."
    error = (status_raw.get("error") or "").strip()
    if error:
        return DISPLAY_FAILED, f"Panel did not initialize: {error}"
    return DISPLAY_FAILED, "Panel did not initialize (no response on the BUSY line)."


def resolve_active_display(active_controller: Optional[str]) -> tuple[str, str, str]:
    """Return ``(controller, driver_module, panel_model)`` for the live panel.

    Maps the board-reported ``active_controller`` to the driver module and panel
    model it implies, so the System card reports what actually drove the panel
    (e.g. the SSD1680 V1 fallback, named a "V1 panel") rather than the configured
    UC8151D / V2 default. Falls back to the configured default when the board has
    not reported yet (status file missing) or reported a controller this table
    does not know -- the card then shows the expected identity instead of a blank
    or a fabricated name.
    """
    if active_controller and active_controller in _CONTROLLER_VARIANTS:
        driver, model = _CONTROLLER_VARIANTS[active_controller]
        return active_controller, driver, model
    return DISPLAY_CONTROLLER, DISPLAY_DRIVER, DISPLAY_MODEL


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def collect_hardware_info(source: HardwareInfoSource) -> HardwareInfo:
    """Assemble :class:`HardwareInfo` from an injected source.

    Pure with respect to its argument: all side effects live in the source's
    callables, so this is deterministic under a fake source.
    """
    kernel_release = source.kernel_release()
    kernel_log = source.kernel_log()
    dpkg_status = source.dpkg_status()

    # The kernel log is preferred because it names the Broadcom stepping, and the
    # A1-vs-B0 distinction is what the advertising verdict turns on; the profile
    # can only name a part for the board as a whole. The fallback is what lets a
    # board whose kernel prints no part number be named at all (no Allwinner
    # kernel prints one, so those boards reported no chip and, with nothing to
    # assess, no advertising verdict either).
    wireless_chip = parse_wireless_chip(kernel_log) or source.declared_wireless_chip()
    firmware_package, firmware_version = find_wifi_firmware_package(dpkg_status)
    bluez_version = parse_dpkg_version(dpkg_status, _BLUEZ_PACKAGE)
    bluez_stack, bluez_stack_summary = summarize_bluez_stack(source.bluez_patch())
    health, summary = assess_wireless_health(
        wireless_chip,
        kernel_release,
        bluez_version=bluez_version,
        bluez_stack=bluez_stack,
    )
    status_raw = source.display_status()
    display_status, display_detail = derive_display_status(status_raw)
    busy_timeout = bool(status_raw.get("busy_timeout")) if status_raw else False
    active_controller = status_raw.get("active_controller") if status_raw else None
    display_controller, display_driver, display_model = resolve_active_display(
        active_controller
    )

    os_pretty_name, os_variant = derive_os_identity(
        source.os_release(),
        dpkg_status,
        source.systemd_default_target(),
        source.rpi_issue(),
    )

    return HardwareInfo(
        pi_model=source.pi_model(),
        kernel_release=kernel_release,
        os_pretty_name=os_pretty_name,
        os_variant=os_variant,
        wireless_chip=wireless_chip,
        wifi_firmware_version=firmware_version,
        wifi_firmware_package=firmware_package,
        bluez_version=bluez_version,
        bluez_stack=bluez_stack,
        bluez_stack_summary=bluez_stack_summary,
        hotspot_health=health,
        hotspot_summary=summary,
        display_model=display_model,
        display_controller=display_controller,
        display_driver=display_driver,
        display_resolution=format_resolution(DISPLAY_WIDTH_PX, DISPLAY_HEIGHT_PX),
        display_status=display_status,
        display_detail=display_detail,
        display_busy_timeout=busy_timeout,
        display_active_controller=active_controller,
    )


# ---------------------------------------------------------------------------
# Production source (real OS access)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _read_kernel_log() -> str:
    """Return kernel-log text containing the wireless init lines, or ``""``.

    The Broadcom chip stepping is only printed by the kernel at boot. Prefer the
    persistent journal (``journalctl -k -b``) which survives ring-buffer
    overwrite; fall back to ``dmesg``. Both can be unavailable (restricted
    journal access, ``kernel.dmesg_restrict``); an empty string makes the chip
    field degrade to ``None`` rather than raising.
    """
    for command in (
        ["journalctl", "-k", "-b", "--no-pager"],
        ["dmesg"],
    ):
        try:
            # S603/B603 are false positives: argv is a fixed literal list, never
            # a shell, with no user-controlled input.
            result = subprocess.run(  # noqa: S603  # nosec B603
                command, capture_output=True, text=True, timeout=5, check=False
            )
        # Best-effort: if a source is unavailable (restricted journal,
        # dmesg_restrict, or a dev host with neither), fall through to the next.
        except (OSError, subprocess.SubprocessError):  # noqa: S112
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return ""


def _read_dpkg_status() -> str:
    """Return the contents of the dpkg status file (rootless), or ``""``."""
    return _read_text_file("/var/lib/dpkg/status")


def _read_text_file(path: str) -> str:
    """Return a UTF-8 file's contents, or ``""`` when it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_systemd_default_target() -> str:
    """Return the systemd default-target unit name, or ``""``.

    Follows ``/etc/systemd/system/default.target`` (a symlink to
    ``multi-user.target`` or ``graphical.target``). Missing or unreadable
    yields empty so the variant stays unset rather than a guessed edition.
    """
    path = Path("/etc/systemd/system/default.target")
    try:
        return path.resolve(strict=True).name
    except OSError:
        return ""


def write_display_status(
    initialized: bool,
    error: Optional[str] = None,
    busy_timeout: bool = False,
    controller: Optional[str] = None,
) -> None:
    """Record the e-paper startup outcome for the (separate) web process.

    The board owns the SPI panel and is the only process that sees the init
    result; the web System card cannot. The board calls this once at startup so
    the card reflects reality -- a V1 / unresponsive panel that trips the BUSY
    timeout writes ``initialized=False`` and the card shows "Not responding"
    instead of falsely asserting the configured V2 panel is working.

    Args:
        initialized: whether a driver successfully drove the panel.
        error: failure detail when not initialized.
        busy_timeout: True when the UC8151D init tripped the BUSY timeout (the
            V1-panel signature). Gates the web UI's IL3820 opt-in.
        controller: the controller that drove the panel (e.g. "SSD1680"), or
            None when disabled.

    Best-effort: a write failure is logged but never aborts board startup. The
    file is truncated on each write so a prior boot's result never lingers.
    """
    payload = {
        "initialized": bool(initialized),
        "error": error,
        "busy_timeout": bool(busy_timeout),
        "active_controller": controller,
        "written_at": time.time(),
    }
    try:
        path = Path(DISPLAY_STATUS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log.warning("[hardware_info] could not write display status: %s", e)


def read_display_status() -> Optional[dict]:
    """Read the board-written display status, or ``None`` if absent/unreadable.

    ``None`` (file missing, unreadable, or not valid JSON object) means the board
    has not reported yet; the caller maps that to the honest ``unknown`` status
    rather than guessing the panel works.
    """
    try:
        raw = Path(DISPLAY_STATUS_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def default_source() -> HardwareInfoSource:
    """Build the production source backed by the OS."""
    return HardwareInfoSource(
        pi_model=wireless_capability.read_pi_model,
        kernel_release=lambda: os.uname().release,
        kernel_log=_read_kernel_log,
        dpkg_status=_read_dpkg_status,
        display_status=read_display_status,
        bluez_patch=bluez_patch_status.read_status,
        declared_wireless_chip=lambda: profile.get_board_profile().wireless_chip,
        os_release=lambda: _read_text_file("/etc/os-release"),
        rpi_issue=lambda: _read_text_file("/etc/rpi-issue"),
        systemd_default_target=_read_systemd_default_target,
    )


def get_hardware_info() -> HardwareInfo:
    """Collect hardware identity for the System card.

    Not cached as a whole: the boot-stable signals (chip, kernel, firmware) are
    memoized at their source (``_read_kernel_log``), but the display status is
    live runtime state -- the board may write it after the web process has
    already served a request, so it must be re-read every call.
    """
    return collect_hardware_info(default_source())
