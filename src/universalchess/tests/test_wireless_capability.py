"""Tests for wireless (Wi-Fi / Bluetooth) hardware capability detection.

Why this module exists at all
----------------------------
A plain Raspberry Pi Zero (no "W") has **no** wireless die: no Wi-Fi, no
Bluetooth. Every Bluetooth/Wi-Fi feature on such a board is dead weight, and one
of them was actively harmful: the bluez self-heal probes "can bluetoothd
register an LE advertisement?", got a failure because there is no controller at
all, concluded the *stock BlueZ was broken*, and spent up to 45 minutes
rebuilding bluetoothd from source (showing "Repairing Bluetooth advertising" on
the panel) -- on every boot, forever, because the resulting marker is never
healthy. The board menus likewise offered Wi-Fi and Bluetooth entries that can
never do anything.

Why the detection is shaped the way it is
-----------------------------------------
Two signals, deliberately combined with OR rather than either one alone:

* The **board model** (``/proc/device-tree/model``) is authoritative about
  *onboard* radios and is independent of driver state, so a Pi Zero W whose
  brcmfmac firmware failed to load still reports "has Wi-Fi" and keeps its menus
  (with the failure visible in them) instead of silently losing the feature.
* An actually-**present device** (a wireless netdev / an ``hciX`` controller)
  covers what the model cannot know: a USB dongle on a non-wireless board, and
  boards whose model string does not imply a fixed answer (Compute Module 4
  ships in wireless and non-wireless variants under the same model string).

An unrecognized model is therefore ``None`` (unknown), not a guess, and the
device probe decides. These tests pin that tri-state and the OR, because every
regression here is silent: it either resurrects the 45-minute rebuild loop on a
Zero, or hides Wi-Fi/Bluetooth on a board that has them.
"""

import pytest

from universalchess.board.wireless_capability import (
    WirelessCapability,
    bluetooth_adapter_present,
    derive_capability,
    model_has_onboard_wireless,
    wifi_interface_present,
)

# Real ``/proc/device-tree/model`` strings (trailing NUL already stripped by the
# reader). Kept verbatim so a regex regression against real-world spacing or the
# "Rev x.y" suffix is caught here rather than on a board.
MODEL_ZERO = "Raspberry Pi Zero Rev 1.3"
MODEL_ZERO_W = "Raspberry Pi Zero W Rev 1.1"
MODEL_ZERO_2_W = "Raspberry Pi Zero 2 W Rev 1.0"
MODEL_PI_1_B_PLUS = "Raspberry Pi Model B Plus Rev 1.2"
MODEL_PI_2_B = "Raspberry Pi 2 Model B Rev 1.1"
MODEL_PI_3_B_PLUS = "Raspberry Pi 3 Model B Plus Rev 1.3"
MODEL_PI_4_B = "Raspberry Pi 4 Model B Rev 1.4"
MODEL_PI_5 = "Raspberry Pi 5 Model B Rev 1.0"
MODEL_PI_400 = "Raspberry Pi 400 Rev 1.0"
MODEL_CM4 = "Raspberry Pi Compute Module 4 Rev 1.1"
MODEL_ORANGEPI_ZERO2W = "OrangePi Zero 2W"
MODEL_ORANGEPI_ZERO = "OrangePi Zero"
MODEL_ORANGEPI_ZERO3 = "Orange Pi Zero 3"
MODEL_ORANGEPI_5 = "Orange Pi 5"


# --------------------------------------------------------------------------- #
# Model -> onboard-radio classification (tri-state)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "model, expected",
    [
        # The two "W" variants this board family ships on: both carry a combo die.
        (MODEL_ZERO_W, True),
        (MODEL_ZERO_2_W, True),
        # The regression that started this: the plain Zero shares the "Zero"
        # prefix with the W variants, so a prefix match would classify it as
        # wireless and it would keep rebuilding bluetoothd forever.
        (MODEL_ZERO, False),
        # Older non-wireless boards, for completeness of the "known False" set.
        (MODEL_PI_1_B_PLUS, False),
        (MODEL_PI_2_B, False),
        # Wireless models must not be dragged into the False set by the
        # "Model B" substring they share with the Pi 1 / Pi 2 patterns.
        (MODEL_PI_3_B_PLUS, True),
        (MODEL_PI_4_B, True),
        (MODEL_PI_5, True),
        (MODEL_PI_400, True),
        # Orange Pi: onboard radios stay offered if firmware fails to bind.
        # Must precede the plain-Pi-Zero False rule (the word "zero" is shared).
        (MODEL_ORANGEPI_ZERO2W, True),
        ("Orange Pi Zero 2W", True),
        ("  orangepi zero 2w  ", True),
        (MODEL_ORANGEPI_ZERO, True),
        (MODEL_ORANGEPI_ZERO3, True),
        (MODEL_ORANGEPI_5, True),
        # Unknown, not guessed: CM4 ships with and without wireless under this
        # exact string, so only the device probe can answer. Claiming either way
        # would hide the radio on one SKU or offer a missing one on the other.
        (MODEL_CM4, None),
        # No model file (dev host / non-Pi) and junk values are unknown too.
        (None, None),
        ("", None),
        ("Some Other Single Board Computer", None),
    ],
)
def test_model_classification(model, expected):
    # Why: this table is the only place the board model is interpreted, and each
    # row is a real device-tree string. A regression manifests as the wrong
    # tri-state for one row -- False/None where True is expected hides working
    # radios; True where False is expected re-enables the self-heal rebuild loop
    # on a board with no controller.
    assert model_has_onboard_wireless(model) is expected


def test_model_classification_ignores_case_and_padding():
    # Why: the value comes from a NUL-terminated device-tree blob and is only
    # decoded/stripped by the reader, so classification must not depend on exact
    # casing or stray whitespace. A regression manifests as a known board
    # collapsing to "unknown" and falling back to the device probe.
    assert model_has_onboard_wireless("  raspberry pi zero 2 w rev 1.0  ") is True
    assert model_has_onboard_wireless("RASPBERRY PI ZERO REV 1.3") is False


# --------------------------------------------------------------------------- #
# Device presence probes (sysfs)
# --------------------------------------------------------------------------- #

def test_bluetooth_adapter_absent_when_no_hci_node(tmp_path):
    # Why: this is the exact state of a plain Pi Zero -- the bluetooth class
    # directory exists (the subsystem is built into the Pi kernel) but is empty.
    # A regression that reports an adapter here re-enables the pointless
    # bluetoothd rebuild.
    (tmp_path / "keep").mkdir()  # non-hci entry must not count
    assert bluetooth_adapter_present(str(tmp_path)) is False


def test_bluetooth_adapter_absent_when_class_dir_missing(tmp_path):
    # Why: on a kernel without the Bluetooth subsystem the directory itself is
    # absent. That must read as "no adapter", not raise -- this runs on the
    # startup path, where an exception would abort board bring-up.
    assert bluetooth_adapter_present(str(tmp_path / "nope")) is False


def test_bluetooth_adapter_present_when_hci_node_exists(tmp_path):
    # Why: the positive case must hold for the W variants and for a USB dongle
    # on a non-wireless board. A regression hides Bluetooth on every board.
    (tmp_path / "hci0").mkdir()
    assert bluetooth_adapter_present(str(tmp_path)) is True


def test_wifi_interface_absent_when_only_wired_and_gadget_links(tmp_path):
    # Why: a Pi Zero set up as a USB Ethernet gadget has real netdevs (``usb0``,
    # ``lo``) with no wireless attribute. Treating any netdev as Wi-Fi would show
    # the Wi-Fi menu on exactly the board this work is about; the regression
    # manifests as True here.
    for iface in ("lo", "usb0", "eth0"):
        (tmp_path / iface).mkdir()
    assert wifi_interface_present(str(tmp_path)) is False


@pytest.mark.parametrize("marker", ["wireless", "phy80211"])
def test_wifi_interface_present_via_either_sysfs_marker(tmp_path, marker):
    # Why: cfg80211 devices expose ``phy80211``; the wireless-extensions compat
    # layer exposes ``wireless``. Which one appears depends on kernel config, so
    # both must count. A regression that checks only one manifests as a board
    # with working Wi-Fi losing its menu on the other kernel build.
    iface = tmp_path / "wlan0"
    iface.mkdir()
    (iface / marker).mkdir()
    assert wifi_interface_present(str(tmp_path)) is True


def test_wifi_interface_absent_when_net_dir_missing(tmp_path):
    # Why: same non-raising contract as the Bluetooth probe -- a missing sysfs
    # tree (non-Linux dev host) must read as "no Wi-Fi" rather than raise on the
    # startup path.
    assert wifi_interface_present(str(tmp_path / "nope")) is False


# --------------------------------------------------------------------------- #
# Combining the two signals
# --------------------------------------------------------------------------- #

def test_plain_zero_with_no_devices_has_neither_radio():
    # Why: the case that motivated the change. Both flags must be False so the
    # self-heal is skipped and the Wi-Fi/Bluetooth menu entries are hidden. A
    # regression here restores the 45-minute rebuild on every boot.
    cap = derive_capability(MODEL_ZERO, wifi_present=False, bluetooth_present=False)
    assert cap == WirelessCapability(
        has_wifi=False, has_bluetooth=False, pi_model=MODEL_ZERO
    )


def test_present_device_enables_a_radio_the_model_lacks():
    # Why: a USB Wi-Fi dongle on a plain Zero really works, so the model's "no
    # onboard radio" must not veto it -- and the dongle must enable *only* the
    # radio it provides. A regression manifests either as a usable dongle being
    # hidden, or as one dongle enabling both features.
    wifi_only = derive_capability(MODEL_ZERO, wifi_present=True, bluetooth_present=False)
    assert (wifi_only.has_wifi, wifi_only.has_bluetooth) == (True, False)

    bt_only = derive_capability(MODEL_ZERO, wifi_present=False, bluetooth_present=True)
    assert (bt_only.has_wifi, bt_only.has_bluetooth) == (False, True)


def test_onboard_model_keeps_features_when_the_driver_failed_to_bind():
    # Why: on a Zero W whose wireless firmware failed to load there is no netdev
    # and no hci node, yet the hardware exists. The features must stay available
    # so the failure is visible and diagnosable in the menus instead of the
    # board silently presenting as a non-wireless model. A regression manifests
    # as the Wi-Fi/Bluetooth entries vanishing after a firmware hiccup.
    cap = derive_capability(MODEL_ZERO_W, wifi_present=False, bluetooth_present=False)
    assert (cap.has_wifi, cap.has_bluetooth) == (True, True)


@pytest.mark.parametrize(
    "wifi_present, bluetooth_present, expected",
    [
        (False, False, (False, False)),
        (True, False, (True, False)),
        (False, True, (False, True)),
        (True, True, (True, True)),
    ],
)
def test_unknown_model_defers_entirely_to_device_presence(
    wifi_present, bluetooth_present, expected
):
    # Why: an unrecognized model (CM4, a future Pi, a non-Pi host) must not be
    # guessed in either direction -- what is actually attached decides. A
    # regression that defaults unknown models to "capable" puts back the
    # self-heal loop on any radio-less unknown board; defaulting to "incapable"
    # hides the radios on the next Pi generation.
    cap = derive_capability(
        MODEL_CM4, wifi_present=wifi_present, bluetooth_present=bluetooth_present
    )
    assert (cap.has_wifi, cap.has_bluetooth) == expected


def test_capability_serializes_for_the_web_contract():
    # Why: the web UI hides its Wi-Fi/Bluetooth cards from this payload, so the
    # key names are a contract. A rename regression manifests as the web falling
    # back to "no radio" and hiding both cards on a fully wireless board.
    cap = derive_capability(MODEL_ZERO_2_W, wifi_present=True, bluetooth_present=True)
    assert cap.to_dict() == {
        "has_wifi": True,
        "has_bluetooth": True,
        "pi_model": MODEL_ZERO_2_W,
    }
