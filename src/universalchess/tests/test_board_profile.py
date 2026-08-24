"""Board hardware profile: Raspberry Pi vs Orange Pi vs unknown.

Why these tests exist:
    Universal Chess grew up on Raspberry Pi BCM numbers, gpiozero,
    ``SPI.open(1, 0)``, and ``/dev/serial0``. That Pi contract is one profile
    for every Raspberry Pi (Zero through CM5). Orange Pi boards share the
    Centaur's 40-pin header but not those nodes; they share one Orange Pi
    profile the same way.

How a regression manifests:
    - Pi model strings picking the Orange Pi (or unknown) profile would break
      every shipping Centaur (wrong UART node, no SPI1).
    - Any Orange Pi model string picking the Pi profile would open
      ``/dev/serial0`` (often missing) and gpiozero BCM pins (wrong SoC).
    - An Orange Pi model classified as unknown has no UART and never gets
      the Armbian overlay.
    - Copying Pi BCM pin numbers onto the Orange Pi profile would look like
      support while wiggling unrelated SoC lines.
"""

from __future__ import annotations

import pytest

from universalchess.board.profile import (
    GPIO_BACKEND_GPIOZERO,
    GPIO_BACKEND_LIBGPIOD,
    PROFILE_ORANGEPI,
    PROFILE_RASPBERRY_PI,
    PROFILE_UNKNOWN,
    USB_GADGET_STACK_DWC3,
    USB_GADGET_STACK_RPI_DWC2,
    USB_GADGET_STACK_SUNXI_MUSB,
    OVERLAY_H3,
    OVERLAY_H616,
    BoardProfile,
    get_board_profile,
    profile_for_model,
)

# Real ``/proc/device-tree/model`` strings (NUL already stripped by the reader).
# The Orange Pi value is the string read from the bring-up board.
MODEL_ZERO_2_W = "Raspberry Pi Zero 2 W Rev 1.0"
MODEL_ZERO_W = "Raspberry Pi Zero W Rev 1.1"
MODEL_PI_4 = "Raspberry Pi 4 Model B Rev 1.4"
MODEL_PI_5 = "Raspberry Pi 5 Model B Rev 1.0"
MODEL_CM4 = "Raspberry Pi Compute Module 4 Rev 1.1"
MODEL_CM5 = "Raspberry Pi Compute Module 5 Rev 1.0"
MODEL_ORANGEPI_ZERO2W = "OrangePi Zero 2W"
MODEL_ORANGEPI_ZERO = "OrangePi Zero"
MODEL_ORANGEPI_ZERO3 = "Orange Pi Zero 3"
MODEL_ORANGEPI_5 = "Orange Pi 5"
MODEL_ORANGEPI_5_PLUS = "Orange Pi 5 Plus"
MODEL_ORANGEPI_PC = "Orange Pi PC"
MODEL_ORANGEPI_ONE = "OrangePi One"
MODEL_ORANGEPI_PLUS2E = "Orange Pi Plus 2E"


def _pi_contract(model: str) -> None:
    """Shipping Pi UART/SPI/GPIO contract used by epdconfig and SyncCentaur."""
    profile = profile_for_model(model)
    assert profile.id == PROFILE_RASPBERRY_PI
    assert profile.model == model
    assert profile.uart_device == "/dev/serial0"
    assert profile.gpio_backend == GPIO_BACKEND_GPIOZERO
    assert profile.spi_bus == 1
    assert profile.spi_device == 0
    # BCM numbers currently hardcoded on RaspberryPi in epdconfig.py.
    assert profile.epaper_rst == 12
    assert profile.epaper_dc == 16
    assert profile.epaper_busy == 7
    assert profile.epaper_cs == 18
    assert profile.gpiochip is None
    assert profile.usb_gadget_stack == USB_GADGET_STACK_RPI_DWC2


@pytest.mark.parametrize(
    "model",
    [MODEL_ZERO_2_W, MODEL_ZERO_W, MODEL_PI_4, MODEL_PI_5, MODEL_CM4, MODEL_CM5],
)
def test_raspberry_pi_models_keep_the_shipping_uart_spi_gpio_contract(model):
    # Why: every Raspberry Pi this project already ships on must keep serial0,
    # SPI1.0, gpiozero, and the BCM RST/DC/BUSY/CS numbers. A regression
    # manifests as a Pi profile that opens the Orange Pi UART node or drops SPI.
    _pi_contract(model)


def _orangepi_identity(model: str) -> None:
    """Every Orange Pi is classified Orange Pi, never Pi BCM or unknown."""
    profile = profile_for_model(model)
    assert profile.id == PROFILE_ORANGEPI
    assert profile.model == model
    assert profile.gpio_backend == GPIO_BACKEND_LIBGPIOD
    assert profile.spi_bus is None
    assert profile.spi_device is None


def test_orangepi_zero2w_uses_measured_uart_and_epaper_gpiochip1_offsets():
    # Why: with the 40-pin header soldered, Centaur e-paper is the Pi SPI1
    # header pins. Live pinctrl on H618 names those SoC pins and their
    # gpiochip1 offsets (PI11=267 RST, PC12=76 DC, PH9=233 BUSY, PI1=257 CS).
    profile = profile_for_model(MODEL_ORANGEPI_ZERO2W)
    _orangepi_identity(MODEL_ORANGEPI_ZERO2W)
    assert profile.uart_device == "/dev/ttyS0"
    assert profile.gpiochip == "gpiochip1"
    assert profile.epaper_rst == 267
    assert profile.epaper_dc == 76
    assert profile.epaper_busy == 233
    assert profile.epaper_cs == 257
    assert profile.usb_gadget_stack == USB_GADGET_STACK_SUNXI_MUSB
    assert profile.spi_gpio_overlay == OVERLAY_H616


@pytest.mark.parametrize(
    "model",
    [
        MODEL_ORANGEPI_PC,
        MODEL_ORANGEPI_ONE,
        MODEL_ORANGEPI_PLUS2E,
        "Orange Pi PC Plus",
        "Orange Pi Prime",
        "Orange Pi Plus",
        "Orange Pi Plus 2",
    ],
)
def test_h3_h5_40pin_orangepi_uses_vendor_header_offsets_not_h618(model):
    # Why: H3/H5 40-pin boards follow the linux-sunxi header table (PA/PD/PG),
    # not H618 PI/PC/PH offsets. Applying 267/76/233/257 would wiggle the
    # wrong balls. UART3 is on header 8/10 as ttyS3.
    profile = profile_for_model(model)
    _orangepi_identity(model)
    assert profile.uart_device == "/dev/ttyS3"
    assert profile.gpiochip == "gpiochip0"
    assert profile.epaper_rst == 200  # PG8, header 32
    assert profile.epaper_dc == 201  # PG9, header 36
    assert profile.epaper_busy == 21  # PA21, header 26
    assert profile.epaper_cs == 110  # PD14, header 12
    assert profile.usb_gadget_stack == USB_GADGET_STACK_SUNXI_MUSB
    assert profile.spi_gpio_overlay == OVERLAY_H3


@pytest.mark.parametrize(
    "model",
    [MODEL_ORANGEPI_5, MODEL_ORANGEPI_5_PLUS, MODEL_ORANGEPI_ZERO, MODEL_ORANGEPI_ZERO3, "Orange Pi 3B", "Orange Pi 800"],
)
def test_orangepi_without_a_40pin_map_does_not_use_h618_epaper_offsets(model):
    # Why: Orange Pi 5 is RK3588 (26-pin or a non-Pi 40-pin). Zero / Zero 3
    # are 26-pin H616. Copying H618 gpiochip1 267 would drive the wrong line.
    # UART still opens so the chess link can be tried; e-paper stays unset.
    profile = profile_for_model(model)
    _orangepi_identity(model)
    assert profile.uart_device == "/dev/ttyS0"
    assert profile.epaper_rst is None
    assert profile.epaper_dc is None
    assert profile.epaper_busy is None
    assert profile.epaper_cs is None
    assert profile.spi_gpio_overlay is None
    if any(token in model.lower() for token in ("5", "3b", "800")):
        assert profile.usb_gadget_stack == USB_GADGET_STACK_DWC3
    else:
        assert profile.usb_gadget_stack == USB_GADGET_STACK_SUNXI_MUSB


@pytest.mark.parametrize(
    "model",
    [
        MODEL_ORANGEPI_ZERO2W,
        MODEL_ORANGEPI_ZERO,
        MODEL_ORANGEPI_ZERO3,
        MODEL_ORANGEPI_5,
        MODEL_ORANGEPI_PC,
    ],
)
def test_every_orange_pi_model_uses_the_orangepi_profile(model):
    # Why: every Orange Pi model string must get the Orange Pi profile.
    # A regression manifests as PROFILE_UNKNOWN for these strings.
    _orangepi_identity(model)


@pytest.mark.parametrize(
    "model",
    ["Orange Pi One Plus", "Orange Pi Lite 2"],
)
def test_h6_orangepi_does_not_use_h3_or_h618_maps(model):
    # Why: One Plus and Lite 2 are H6. The H3 40-pin PD/PA/PG map and the
    # H618 PI-bank map are the wrong balls. Manifests as ttyS3 or gpiochip1
    # 267 on an H6 board.
    profile = profile_for_model(model)
    _orangepi_identity(model)
    assert profile.uart_device == "/dev/ttyS0"
    assert profile.epaper_rst is None
    assert profile.spi_gpio_overlay is None
    assert profile.usb_gadget_stack == USB_GADGET_STACK_SUNXI_MUSB


def test_orangepi_classification_ignores_spacing_and_case():
    # Why: the blob is NUL-terminated and vendors write "Orange Pi" or
    # "OrangePi". A spacing/case regression would classify the board as
    # unknown and leave UART unset. Manifests as profile id "unknown" for
    # "  orange pi zero 2w  ".
    profile = profile_for_model("  orange pi zero 2w  ")
    assert profile.id == PROFILE_ORANGEPI
    assert profile_for_model("ORANGEPI ZERO 2W").id == PROFILE_ORANGEPI
    assert profile_for_model("OrangePi-5").id == PROFILE_ORANGEPI


@pytest.mark.parametrize(
    "model",
    [None, "", "Some Other Single Board Computer"],
)
def test_unknown_model_does_not_invent_uart_spi_or_pins(model):
    # Why: an unrecognized string must not guess /dev/serial0 or BCM pins --
    # that is how a future board would silently talk to the wrong device. A
    # regression manifests as uart_device or epaper_busy being set on unknown.
    profile = profile_for_model(model)
    assert profile.id == PROFILE_UNKNOWN
    assert profile.model == (model or None)
    assert profile.uart_device is None
    assert profile.gpio_backend is None
    assert profile.spi_bus is None
    assert profile.spi_device is None
    assert profile.epaper_rst is None
    assert profile.epaper_dc is None
    assert profile.epaper_busy is None
    assert profile.epaper_cs is None
    assert profile.usb_gadget_stack is None


def test_get_board_profile_reads_the_injected_model(monkeypatch):
    # Why: production entry point must use the device-tree reader, and tests
    # must be able to inject a model without a real /proc. A regression that
    # ignores the reader would always return unknown on the test host, or always
    # hit the live device tree on CI.
    monkeypatch.setattr(
        "universalchess.board.profile.read_device_tree_model",
        lambda: MODEL_ORANGEPI_ZERO2W,
    )
    profile = get_board_profile()
    assert profile.id == PROFILE_ORANGEPI
    assert profile.uart_device == "/dev/ttyS0"


def test_board_profile_is_immutable():
    # Why: callers cache this for the life of a boot. Mutation would let one
    # subsystem change UART for everyone else. A regression manifests as a
    # writable field (FrozenInstanceError not raised).
    profile = profile_for_model(MODEL_ZERO_2_W)
    with pytest.raises(AttributeError):
        profile.uart_device = "/dev/ttyS0"
    assert isinstance(profile, BoardProfile)
