"""Whether this board physically has Wi-Fi and Bluetooth radios.

Why this exists
---------------
Universal Chess runs on Raspberry Pi boards that may have **no wireless hardware
at all** -- notably the plain Pi Zero (the "W" is what carries the Wi-Fi +
Bluetooth combo die). Every Wi-Fi/Bluetooth feature is then inert, and one of
them was actively destructive: the bluez self-heal asks "can bluetoothd register
an LE advertisement?", gets a failure because there is no controller, concludes
the stock BlueZ is broken, and rebuilds ``bluetoothd`` from source -- up to 45
minutes on a Zero, on every boot, forever. The menus likewise offered a Wi-Fi
scan that can never find a network and a pairing flow with no adapter.

How the answer is derived
-------------------------
Two independent signals, combined with OR, because each one knows something the
other cannot:

``/proc/device-tree/model``
    Authoritative about *onboard* radios and independent of driver state. This is
    what keeps a Pi Zero W whose brcmfmac firmware failed to load from silently
    presenting as a non-wireless board: the features stay available so the
    failure is visible and diagnosable in the menus instead of the controls
    disappearing. Deliberately tri-state -- a model that does not imply a fixed
    answer (Compute Module 4 ships in wireless and non-wireless variants under
    one model string; a future Pi generation is simply unknown) returns ``None``
    rather than a guess in either direction.

Device presence in sysfs
    What the model cannot know: a USB dongle on a board with no onboard radio,
    and the unknown-model case above. ``/sys/class/bluetooth/hciX`` and a
    wireless attribute under ``/sys/class/net/<iface>`` are created by the kernel
    driver, so they answer "is a controller attached", not "is it switched on" --
    an rfkill-blocked adapter still appears.

A separate question, deliberately answered differently: the self-heal
(``scripts/bluez-selfheal``, which runs as root from bash and cannot import this
package) gates purely on controller presence, because with no controller there is
nothing that could ever advertise regardless of what the model claims. Both
implementations must agree on ``SYSFS_BLUETOOTH``; a test asserts the script
carries the same literal.

All OS access is confined to :func:`read_pi_model`, :func:`wifi_interface_present`
and :func:`bluetooth_adapter_present`, each of which returns a value instead of
raising (these run on the board's startup path). The classification
(:func:`model_has_onboard_wireless`) and the combination
(:func:`derive_capability`) are pure and directly testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Kernel-populated class directories. Stable sysfs ABI; overridable per call so
# the probes are testable against a synthetic tree.
SYSFS_NET = "/sys/class/net"
SYSFS_BLUETOOTH = "/sys/class/bluetooth"

# Board model (device tree). NUL-terminated string, e.g. "Raspberry Pi Zero W
# Rev 1.1"; absent on non-Pi hosts.
DEVICE_TREE_MODEL = "/proc/device-tree/model"

# Attributes a netdev carries when it is a wireless interface. cfg80211 devices
# expose ``phy80211``; the wireless-extensions compatibility layer
# (CONFIG_CFG80211_WEXT) exposes ``wireless``. Which one is present depends on
# the kernel build, so either counts. Checking these rather than an ``wlan*``
# name is what keeps the USB Ethernet gadget's ``usb0`` from reading as Wi-Fi.
_WIRELESS_ATTRIBUTES = ("phy80211", "wireless")

# Prefix of a Bluetooth controller's class entry (hci0, hci1, ...).
_HCI_PREFIX = "hci"

# Model -> onboard combo-die present, evaluated in order (the first match wins,
# so the "W" Zero variants must precede the bare "Zero" rule they share a prefix
# with). Absence from this table means "unknown", never "no": see the module
# docstring on why a guess is worse than deferring to device presence.
_MODEL_WIRELESS_RULES = (
    # Orange Pi. Must precede the plain-Pi-Zero False rule, which would
    # otherwise match the word "zero" and hide the radios on firmware failure.
    (re.compile(r"^orange\s*pi\b"), True),
    (re.compile(r"\bzero\s+(?:2\s+)?w\b"), True),        # Zero W, Zero 2 W
    (re.compile(r"^raspberry pi zero\b"), False),         # plain Pi Zero only
    (re.compile(r"^raspberry pi model [ab]\b"), False),   # Pi 1 A/B/A+/B+
    (re.compile(r"^raspberry pi 2 model b\b"), False),    # Pi 2 B (all revisions)
    (re.compile(r"^raspberry pi (?:400|500|3|4|5)\b"), True),
)


@dataclass(frozen=True)
class WirelessCapability:
    """Which radio features this board can offer.

    ``has_wifi`` / ``has_bluetooth`` answer "should this feature be offered and
    driven at all", not "is the radio currently switched on" -- an enabled/blocked
    radio is live status owned by ``epaper.wifi_info`` and the Bluetooth status
    state. ``pi_model`` is carried for reporting and is ``None`` when the device
    tree could not be read.
    """

    has_wifi: bool
    has_bluetooth: bool
    pi_model: Optional[str]

    def to_dict(self) -> dict:
        """JSON-serializable contract consumed by the web UI."""
        return {
            "has_wifi": self.has_wifi,
            "has_bluetooth": self.has_bluetooth,
            "pi_model": self.pi_model,
        }


def model_has_onboard_wireless(model: Optional[str]) -> Optional[bool]:
    """Classify a board model string as having onboard radios.

    Returns ``True``/``False`` only for models whose wireless fitment is fixed and
    known, and ``None`` when it is not determined by the model alone (an
    unreadable model, a Compute Module whose variants differ, an unrecognized
    board). Callers must treat ``None`` as "ask the hardware", never as "no".
    """
    if not model:
        return None
    normalized = " ".join(model.lower().split())
    for pattern, has_wireless in _MODEL_WIRELESS_RULES:
        if pattern.search(normalized):
            return has_wireless
    return None


def read_pi_model() -> Optional[str]:
    """Read the board model from the device tree (rootless), or ``None``.

    ``/proc/device-tree/model`` is a NUL-terminated string (e.g. ``"Raspberry Pi
    Zero W Rev 1.1"``); absent on non-Pi/dev hosts, which yields ``None`` rather
    than a fabricated model that would drive a wrong capability verdict.
    """
    try:
        raw = Path(DEVICE_TREE_MODEL).read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\x00", "").strip()
    return text or None


def wifi_interface_present(sysfs_net: str = SYSFS_NET) -> bool:
    """Whether the kernel has registered at least one wireless network interface.

    True for an onboard radio and for a USB Wi-Fi dongle; unaffected by whether
    the interface is up or rfkill-blocked. A missing sysfs tree (non-Linux dev
    host) reads as absent rather than raising.
    """
    try:
        interfaces = list(Path(sysfs_net).iterdir())
    except OSError:
        return False
    return any(
        (interface / attribute).exists()
        for interface in interfaces
        for attribute in _WIRELESS_ATTRIBUTES
    )


def bluetooth_adapter_present(sysfs_bluetooth: str = SYSFS_BLUETOOTH) -> bool:
    """Whether the kernel has registered at least one Bluetooth controller.

    True for an onboard controller and for a USB dongle; an rfkill-blocked
    adapter still appears, so this answers "is one attached", not "is it on". On a
    plain Pi Zero the directory exists (the subsystem is built into the Pi
    kernel) but holds no ``hciX`` entry. A missing directory reads as absent
    rather than raising.
    """
    try:
        entries = list(Path(sysfs_bluetooth).iterdir())
    except OSError:
        return False
    return any(entry.name.startswith(_HCI_PREFIX) for entry in entries)


def derive_capability(
    pi_model: Optional[str], *, wifi_present: bool, bluetooth_present: bool
) -> WirelessCapability:
    """Combine the model classification with observed device presence.

    A feature is offered when the model says the board has that radio onboard
    **or** a device for it is actually attached. The OR is what makes a USB
    dongle work on a plain Zero while still keeping a Zero W's menus when its
    driver failed to bind (see the module docstring).
    """
    onboard = model_has_onboard_wireless(pi_model)
    return WirelessCapability(
        has_wifi=onboard is True or wifi_present,
        has_bluetooth=onboard is True or bluetooth_present,
        pi_model=pi_model,
    )


def get_wireless_capability() -> WirelessCapability:
    """Read the board's radio capability from the OS.

    Not cached: a USB dongle can be attached while the board runs, and the cost
    is two small directory scans, so a cache would trade honesty for nothing.
    """
    return derive_capability(
        read_pi_model(),
        wifi_present=wifi_interface_present(),
        bluetooth_present=bluetooth_adapter_present(),
    )
