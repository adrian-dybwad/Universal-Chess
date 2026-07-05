"""Static hardware identity: wireless chip, firmware/OS versions, and display.

Why this is separate from :mod:`universalchess.board.system_info`:
  ``system_info`` reports *telemetry* -- numbers that change every second (CPU,
  memory, uptime) and are polled on an interval. This module reports *identity*
  -- facts fixed for the life of a boot (which Broadcom wireless die is fitted,
  the kernel/firmware versions, the e-paper panel). Mixing the two would make
  the telemetry card re-run kernel-log parsing on every 5-second poll. Identity
  is gathered once and cached.

Primary motivation -- the Bluetooth advertising health row:
  The DGT Centaur's Pi uses a Broadcom combo (Wi-Fi + Bluetooth on one die).
  Field investigation proved that on the **BCM43430B0** stepping running the
  Raspberry Pi **kernel 6.18.x** line, BlueZ LE advertising stops working: the
  identical ``RegisterAdvertisement`` call accepted on kernel 6.12.x is rejected
  with ``Invalid Parameters`` on 6.18 (the companion app can no longer see the
  board). The *same* B0 die works on kernel 6.12.x, and the older **BCM43430A1**
  stepping works on every kernel observed. So the honest signal is the chip
  stepping *together with* the kernel version -- not the chip alone. The scope
  is strictly Bluetooth LE advertising; the Wi-Fi STA/AP path was not shown to
  fail and is deliberately not claimed here.

  Mitigation: running a 6.12.x kernel avoids it, and Universal Chess applies a
  self-healing patch on install (to be rolled back once the official fix ships).
  :func:`assess_wireless_health` encodes exactly the proven data points and
  reports "unknown" for combinations never observed, rather than guessing.

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

# The proven-broken stepping and the kernel boundary where it breaks. Both are
# evidence, not assumption: BCM43430B0 was confirmed working on 6.12.47/6.12.75
# and broken on 6.18.34, where BlueZ LE advertising is rejected with "Invalid
# Parameters". The known-good recovery is to run a 6.12.x kernel (or rely on the
# self-healing patch applied at install).
_AFFECTED_CHIP = "BCM43430B0"
_AFFECTED_KERNEL_MIN: tuple[int, int] = (6, 18)
_KNOWN_GOOD_KERNEL_MAX: tuple[int, int] = (6, 12)

# Packages whose versions are part of the wireless story and worth surfacing.
_WIFI_FIRMWARE_PACKAGE = "firmware-brcm80211"
_BLUEZ_PACKAGE = "bluez"


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

    def to_dict(self) -> dict:
        """Flat, JSON-serializable contract consumed by the React System card."""
        return {
            "pi_model": self.pi_model,
            "kernel_release": self.kernel_release,
            "wireless_chip": self.wireless_chip,
            "wifi_firmware_version": self.wifi_firmware_version,
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
    """
    if not dpkg_status:
        return None
    # Stanzas are separated by blank lines; scan for the exact package stanza.
    for stanza in dpkg_status.split("\n\n"):
        if re.search(rf"^Package:\s*{re.escape(package)}\s*$", stanza, re.MULTILINE):
            version = re.search(r"^Version:\s*(.+?)\s*$", stanza, re.MULTILINE)
            return version.group(1) if version else None
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
    wireless_chip: Optional[str], kernel_release: str
) -> tuple[str, str]:
    """Classify Bluetooth LE advertising reliability for the fitted chip+kernel.

    Returns ``(health, human_summary)`` where ``health`` is one of
    :data:`HEALTH_OK`, :data:`HEALTH_AFFECTED`, :data:`HEALTH_UNKNOWN`.

    Only the proven data points drive an ``ok``/``affected`` verdict; every other
    combination is ``unknown`` so the card never asserts a result that was not
    actually observed (see module docstring for the evidence).
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
        return (
            HEALTH_AFFECTED,
            f"{wireless_chip} on kernel {kernel_release} has a known fault: "
            "Bluetooth advertising can stop working. Running a 6.12.x kernel "
            "resolves it, or on installing Universal Chess a self-healing patch "
            "is applied that will be rolled back as soon as the official fix is "
            "released.",
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

    wireless_chip = parse_wireless_chip(kernel_log)
    health, summary = assess_wireless_health(wireless_chip, kernel_release)
    bluez_stack, bluez_stack_summary = summarize_bluez_stack(source.bluez_patch())
    status_raw = source.display_status()
    display_status, display_detail = derive_display_status(status_raw)
    busy_timeout = bool(status_raw.get("busy_timeout")) if status_raw else False
    active_controller = status_raw.get("active_controller") if status_raw else None
    display_controller, display_driver, display_model = resolve_active_display(
        active_controller
    )

    return HardwareInfo(
        pi_model=source.pi_model(),
        kernel_release=kernel_release,
        wireless_chip=wireless_chip,
        wifi_firmware_version=parse_dpkg_version(dpkg_status, _WIFI_FIRMWARE_PACKAGE),
        bluez_version=parse_dpkg_version(dpkg_status, _BLUEZ_PACKAGE),
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


def _read_pi_model() -> Optional[str]:
    """Read the board model from the device tree (rootless), or ``None``.

    ``/proc/device-tree/model`` is a NUL-terminated string (e.g.
    ``"Raspberry Pi Zero W Rev 1.1"``); absent on non-Pi/dev hosts.
    """
    try:
        raw = Path("/proc/device-tree/model").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\x00", "").strip()
    return text or None


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
    try:
        return Path("/var/lib/dpkg/status").read_text(encoding="utf-8", errors="replace")
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
        pi_model=_read_pi_model,
        kernel_release=lambda: os.uname().release,
        kernel_log=_read_kernel_log,
        dpkg_status=_read_dpkg_status,
        display_status=read_display_status,
        bluez_patch=bluez_patch_status.read_status,
    )


def get_hardware_info() -> HardwareInfo:
    """Collect hardware identity for the System card.

    Not cached as a whole: the boot-stable signals (chip, kernel, firmware) are
    memoized at their source (``_read_kernel_log``), but the display status is
    live runtime state -- the board may write it after the web process has
    already served a request, so it must be re-read every call.
    """
    return collect_hardware_info(default_source())
