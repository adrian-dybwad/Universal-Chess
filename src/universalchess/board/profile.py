"""Hardware profile derived from the device-tree model string.

Universal Chess grew up on Raspberry Pi BCM numbering, gpiozero, SPI1.0, and
``/dev/serial0``. That Pi contract is one profile from Zero through CM5.
Orange Pi boards share a 40-pin header but not BCM numbering: each SoC family
has its own header map. This module is the single place that maps a model
string to UART, SPI, GPIO backend, and which spi-gpio overlay to load.

H616/H618 e-paper offsets come from live pinctrl on Orange Pi Zero 2W
(gpiochip1, PI11/PC12/PH9/PI1). H3/H5 40-pin offsets come from the
linux-sunxi header table (PG8/PG9/PA21/PD14). Other Orange Pi models stay
on the Orange Pi profile (UART + libgpiod) without copying those maps.
Do not copy Raspberry Pi BCM numbers onto any Orange Pi profile.

OS access is only :func:`read_device_tree_model` (same blob as
:mod:`wireless_capability`). Classification (:func:`profile_for_model`) is
pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from universalchess.board.wireless_capability import read_pi_model

PROFILE_RASPBERRY_PI = "raspberry-pi"
PROFILE_ORANGEPI = "orangepi"
PROFILE_UNKNOWN = "unknown"

GPIO_BACKEND_GPIOZERO = "gpiozero"
GPIO_BACKEND_LIBGPIOD = "libgpiod"

USB_GADGET_STACK_RPI_DWC2 = "rpi-dwc2"
USB_GADGET_STACK_SUNXI_MUSB = "sunxi-musb"
USB_GADGET_STACK_DWC3 = "dwc3"
OVERLAY_H616 = "uc-centaur-spi-gpio"
OVERLAY_H3 = "uc-centaur-spi-gpio-h3"

# Wireless part fitted to the Orange Pi Zero 2W, observed during its bring-up:
# the board comes up with a live hci0 on this Unisoc combo, which is why the
# BlueZ self-heal (written for a Broadcom defect) is gated to Raspberry Pi. No
# Allwinner kernel prints a part number the System card can parse, so the card
# has no other way to name it. Declared only for the model it was measured on.
WIRELESS_CHIP_UWE5622 = "UWE5622"
_ORANGEPI = re.compile(r"^orange\s*pi\b")
_RASPBERRY_PI = re.compile(r"^raspberry\s+pi\b")
_H616_40 = re.compile(r"zero\s*2\s*w\b")
_ROCKCHIP = re.compile(
    r"\b(?:[45](?:\s*(?:plus|pro|max|ultra|b))?|3\s*b|800)\b"
)
_H3_40 = re.compile(
    r"(?:\bpc(?:\s*plus|\s*2)?\b"
    r"|\bone\b(?!\s*plus)"
    r"|\blite\b(?!\s*2)"
    r"|(?<!one\s)(?<![45]\s)\bplus(?:\s*2(?:e)?)?\b"
    r"|\bplus2e\b"
    r"|\bprime\b"
    r"|\borange\s*pi\s+2\b(?!\s*w)"
    r"|\borangepi\s*2\b(?!\s*w))"
)


@dataclass(frozen=True)
class BoardProfile:
    """Boot-stable hardware map for UART, SPI, and e-paper GPIO.

    Pin fields are BCM numbers when ``gpio_backend`` is gpiozero. They are
    gpiochip line offsets when the backend is libgpiod -- and ``None`` when
    this family has no measured 40-pin Centaur map.
    ``spi_bus`` / ``spi_device`` are ``None`` when userspace must discover
    the SPI master (spi-gpio) rather than open a hardcoded ``/dev/spidev*``.
    ``spi_gpio_overlay`` is the Armbian user overlay basename, or ``None``
    when this board must not load a spi-gpio overlay.
    ``wireless_chip`` is the radio part fitted to this model, and is ``None``
    unless it was actually observed on the board -- a Raspberry Pi declares
    nothing because its kernel log names the part, down to the stepping the
    Bluetooth advertising verdict depends on.
    """

    id: str
    model: str | None
    uart_device: str | None
    gpio_backend: str | None
    spi_bus: int | None
    spi_device: int | None
    epaper_rst: int | None
    epaper_dc: int | None
    epaper_busy: int | None
    epaper_cs: int | None
    gpiochip: str | None = None
    usb_gadget_stack: str | None = None
    spi_gpio_overlay: str | None = None
    wireless_chip: str | None = None


def _normalize(model: str | None) -> str:
    if not model:
        return ""
    return " ".join(model.lower().split())


def _raspberry_pi(model: str) -> BoardProfile:
    return BoardProfile(
        id=PROFILE_RASPBERRY_PI,
        model=model,
        uart_device="/dev/serial0",
        gpio_backend=GPIO_BACKEND_GPIOZERO,
        spi_bus=1,
        spi_device=0,
        epaper_rst=12,
        epaper_dc=16,
        epaper_busy=7,
        epaper_cs=18,
        gpiochip=None,
        usb_gadget_stack=USB_GADGET_STACK_RPI_DWC2,
    )


def _orangepi_h616(model: str) -> BoardProfile:
    # Zero 2W: uart0 PH0/PH1 header 8/10. E-paper gpiochip1 PI11/PC12/PH9/PI1.
    return BoardProfile(
        id=PROFILE_ORANGEPI,
        model=model,
        uart_device="/dev/ttyS0",
        gpio_backend=GPIO_BACKEND_LIBGPIOD,
        spi_bus=None,
        spi_device=None,
        epaper_rst=267,
        epaper_dc=76,
        epaper_busy=233,
        epaper_cs=257,
        gpiochip="gpiochip1",
        usb_gadget_stack=USB_GADGET_STACK_SUNXI_MUSB,
        spi_gpio_overlay=OVERLAY_H616,
        wireless_chip=WIRELESS_CHIP_UWE5622,
    )


def _orangepi_h3(model: str) -> BoardProfile:
    # linux-sunxi 40-pin: header 32 PG8, 36 PG9, 26 PA21, 12 PD14.
    # Header 8/10 is UART3 (ttyS3). pio is gpiochip0 on these images.
    return BoardProfile(
        id=PROFILE_ORANGEPI,
        model=model,
        uart_device="/dev/ttyS3",
        gpio_backend=GPIO_BACKEND_LIBGPIOD,
        spi_bus=None,
        spi_device=None,
        epaper_rst=200,
        epaper_dc=201,
        epaper_busy=21,
        epaper_cs=110,
        gpiochip="gpiochip0",
        usb_gadget_stack=USB_GADGET_STACK_SUNXI_MUSB,
        spi_gpio_overlay=OVERLAY_H3,
    )


def _orangepi_rockchip(model: str) -> BoardProfile:
    # RK3588/RK3399/RK3566: UART node varies; e-paper header map is not Pi-compatible.
    return BoardProfile(
        id=PROFILE_ORANGEPI,
        model=model,
        uart_device="/dev/ttyS0",
        gpio_backend=GPIO_BACKEND_LIBGPIOD,
        spi_bus=None,
        spi_device=None,
        epaper_rst=None,
        epaper_dc=None,
        epaper_busy=None,
        epaper_cs=None,
        gpiochip=None,
        usb_gadget_stack=USB_GADGET_STACK_DWC3,
        spi_gpio_overlay=None,
    )


def _orangepi_generic(model: str) -> BoardProfile:
    # Allwinner boards without a 40-pin Centaur map (Zero, Zero 3, H6, …).
    return BoardProfile(
        id=PROFILE_ORANGEPI,
        model=model,
        uart_device="/dev/ttyS0",
        gpio_backend=GPIO_BACKEND_LIBGPIOD,
        spi_bus=None,
        spi_device=None,
        epaper_rst=None,
        epaper_dc=None,
        epaper_busy=None,
        epaper_cs=None,
        gpiochip=None,
        usb_gadget_stack=USB_GADGET_STACK_SUNXI_MUSB,
        spi_gpio_overlay=None,
    )


def _orangepi(model: str) -> BoardProfile:
    normalized = _normalize(model)
    if _H616_40.search(normalized):
        return _orangepi_h616(model)
    if _ROCKCHIP.search(normalized):
        return _orangepi_rockchip(model)
    if _H3_40.search(normalized):
        return _orangepi_h3(model)
    return _orangepi_generic(model)


def _unknown(model: str | None) -> BoardProfile:
    stored = model or None
    return BoardProfile(
        id=PROFILE_UNKNOWN,
        model=stored,
        uart_device=None,
        gpio_backend=None,
        spi_bus=None,
        spi_device=None,
        epaper_rst=None,
        epaper_dc=None,
        epaper_busy=None,
        epaper_cs=None,
        gpiochip=None,
        usb_gadget_stack=None,
    )


def profile_for_model(model: str | None) -> BoardProfile:
    """Return the hardware profile for a device-tree model string.

    Pure: no filesystem. Unknown or empty models leave every device field
    unset rather than guessing Raspberry Pi nodes.
    """
    normalized = _normalize(model)
    if not normalized:
        return _unknown(model)
    if _ORANGEPI.search(normalized):
        return _orangepi(model.strip() if model else model)
    if _RASPBERRY_PI.search(normalized):
        return _raspberry_pi(model.strip() if model else model)
    return _unknown(model)


def read_device_tree_model() -> str | None:
    """Device-tree model, or ``None``. Delegates to the existing reader."""
    return read_pi_model()


def get_board_profile() -> BoardProfile:
    """Profile for the running board, from the device-tree model."""
    return profile_for_model(read_device_tree_model())


class UnconfiguredBoardError(RuntimeError):
    """A driver asked for a device the current board profile does not define."""

    def __init__(self, profile_id: str) -> None:
        """Name the profile that has no UART, without guessing a Pi node."""
        super().__init__(
            f"board profile {profile_id!r} has no UART device; refusing to guess /dev/serial0"
        )


def require_uart_device(profile: BoardProfile | None = None) -> str:
    """Return the profile UART node, or raise rather than guessing a Pi alias.

    ``/dev/serial0`` exists only as a Raspberry Pi udev name. Using it as a
    fallback for an unknown or Orange Pi profile would either fail to open or
    talk to the wrong SoC UART.
    """
    chosen = get_board_profile() if profile is None else profile
    if chosen.uart_device is None:
        raise UnconfiguredBoardError(chosen.id)
    return chosen.uart_device
