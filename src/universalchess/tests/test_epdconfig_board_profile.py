"""e-paper backend follows the board profile instead of always constructing gpiozero.

Why these tests exist:
    epdconfig binds RaspberryPi() at import, which claims BCM 12/16/7/18 via
    gpiozero. On an Orange Pi Zero 2W those numbers are the wrong H618 lines,
    and SPI1.0 is not the Centaur e-paper bus. The profile's gpiozero backend
    must keep today's Pi pins and SPI 1.0; the libgpiod backend must use the
    measured gpiochip1 offsets and the spi-gpio master, not gpiozero or SPI 1.0.

How a regression manifests:
    - Orange Pi still constructing RaspberryPi: gpiozero wiggles H618 pins
      that are not the e-paper RST/DC/BUSY/CS.
    - Orange Pi module_init opening SPI 1.0: talks to the wrong controller
      (onboard flash / Pi SPI0 header), not the Centaur panel.
    - Pi backend losing BCM 12/16/7/18 or SPI 1.0: every shipping panel dies.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from universalchess.board.profile import (
    GPIO_BACKEND_LIBGPIOD,
    BoardProfile,
    profile_for_model,
)
from universalchess.epaper.framework.waveshare import epdconfig

MODEL_PI = "Raspberry Pi Zero 2 W Rev 1.0"
MODEL_ORANGEPI = "OrangePi Zero 2W"


def test_pi_backend_is_raspberry_pi_with_shipping_pins_and_spi_1_0():
    # Why: Pi panels are wired RST=12 DC=16 BUSY=7 CS=18 on SPI1.0. A profile
    # backend that returned UnconfiguredEpaper or different pins would brick
    # every shipping Centaur display.
    profile = profile_for_model(MODEL_PI)
    backend = epdconfig.backend_for_profile(profile)
    assert isinstance(backend, epdconfig.RaspberryPi)
    assert backend.RST_PIN == 12
    assert backend.DC_PIN == 16
    assert backend.BUSY_PIN == 7
    assert backend.CS_PIN == 18
    backend.SPI.reset_mock()
    assert backend.module_init() == 0
    backend.SPI.open.assert_called_with(1, 0)


def test_orangepi_backend_is_libgpiod_with_measured_pins_without_gpiozero():
    # Why: gpiochip1 offsets were read from live pinctrl (PI11/PC12/PH9/PI1).
    # Constructing RaspberryPi would still drive BCM numbers. Manifests as
    # gpiozero.LED called, or RST_PIN==12.
    gpiozero = sys.modules["gpiozero"]
    gpiozero.reset_mock()
    profile = profile_for_model(MODEL_ORANGEPI)
    backend = epdconfig.backend_for_profile(profile)
    assert isinstance(backend, epdconfig.LibgpiodEpaper)
    assert backend.RST_PIN == 267
    assert backend.DC_PIN == 76
    assert backend.BUSY_PIN == 233
    assert backend.CS_PIN == 257
    gpiozero.LED.assert_not_called()
    gpiozero.InputDevice.assert_not_called()


def test_libgpiod_module_init_opens_spi_gpio_not_onboard_flash(tmp_path, monkeypatch):
    # Why: hardware SPI0 is the 16MB NOR. Opening spidev0.0 or SPI 1.0 talks
    # to flash / the Pi SPI0 header. Manifests as SPI.open(0, 0) or (1, 0).
    _fake_spi_gpio_sysfs(tmp_path, bus=2)
    monkeypatch.setattr(
        epdconfig, "SPI_MASTER_SYSFS", str(tmp_path / "spi_master")
    )
    fake_gpiod = MagicMock()
    monkeypatch.setitem(sys.modules, "gpiod", fake_gpiod)
    monkeypatch.setitem(sys.modules, "gpiod.line", MagicMock())
    profile = profile_for_model(MODEL_ORANGEPI)
    backend = epdconfig.backend_for_profile(profile)
    backend.SPI = MagicMock()
    assert backend.module_init() == 0
    backend.SPI.open.assert_called_once_with(2, 0)


def test_libgpiod_module_init_releases_prior_request_before_claiming_again(
    tmp_path, monkeypatch
):
    # Why: display_boot initialize() and board.init_display() both call
    # module_init on the singleton. A second request_lines without releasing
    # raises OSError 16 (EBUSY) and crash-loops the service. Measured on
    # the Orange Pi: lines 76/233/267 stayed consumer=universalchess-epaper.
    _fake_spi_gpio_sysfs(tmp_path, bus=2)
    monkeypatch.setattr(
        epdconfig, "SPI_MASTER_SYSFS", str(tmp_path / "spi_master")
    )
    first_request = MagicMock()
    second_request = MagicMock()
    fake_gpiod = MagicMock()
    fake_gpiod.request_lines.side_effect = [first_request, second_request]
    monkeypatch.setitem(sys.modules, "gpiod", fake_gpiod)
    monkeypatch.setitem(sys.modules, "gpiod.line", MagicMock())
    profile = profile_for_model(MODEL_ORANGEPI)
    backend = epdconfig.backend_for_profile(profile)
    backend.SPI = MagicMock()
    assert backend.module_init() == 0
    assert backend.module_init() == 0
    first_request.release.assert_called_once()
    assert fake_gpiod.request_lines.call_count == 2
    assert backend._request is second_request


def test_libgpiod_module_init_refuses_when_spi_gpio_overlay_is_missing(
    tmp_path, monkeypatch
):
    # Why: without spi-gpio there is no Centaur SPI master. Opening any
    # leftover spidev would hit flash. Manifests as SPI.open succeeding.
    monkeypatch.setattr(
        epdconfig, "SPI_MASTER_SYSFS", str(tmp_path / "empty_spi_master")
    )
    (tmp_path / "empty_spi_master").mkdir()
    profile = profile_for_model(MODEL_ORANGEPI)
    backend = epdconfig.backend_for_profile(profile)
    backend.SPI = MagicMock()
    with pytest.raises(RuntimeError, match="spi-gpio"):
        backend.module_init()
    backend.SPI.open.assert_not_called()


def test_libgpiod_profile_without_pins_stays_unconfigured():
    # Why: a future libgpiod board with unmeasured lines must still fail
    # closed. Manifests as LibgpiodEpaper claiming offset 0.
    profile = BoardProfile(
        id="future-board",
        model="Future",
        uart_device="/dev/ttyS0",
        gpio_backend=GPIO_BACKEND_LIBGPIOD,
        spi_bus=None,
        spi_device=None,
        epaper_rst=None,
        epaper_dc=None,
        epaper_busy=None,
        epaper_cs=None,
        gpiochip=None,
    )
    backend = epdconfig.backend_for_profile(profile)
    assert isinstance(backend, epdconfig.UnconfiguredEpaper)
    with pytest.raises(RuntimeError, match="not configured"):
        backend.module_init()


def test_unknown_backend_stays_raspberry_pi_so_dev_hosts_keep_importing():
    # Why: CI and laptops have no device-tree model. Switching unknown to a
    # refusing backend would break every e-paper test that imports epdconfig
    # on those hosts. Manifests as UnconfiguredEpaper at profile_for_model(None).
    backend = epdconfig.backend_for_profile(profile_for_model(None))
    assert isinstance(backend, epdconfig.RaspberryPi)


def test_find_spi_gpio_bus_matches_kernel_spi_gpio_driver_directory(tmp_path):
    # Why: Linux registers the platform driver as spi_gpio (underscore).
    # Matching only the DT compatible spi-gpio misses the live master on
    # Orange Pi Zero 2W (/sys/bus/platform/drivers/spi_gpio). Manifests as
    # find_spi_gpio_bus returning None while spi0 is the overlay.
    _fake_spi_gpio_sysfs(tmp_path, bus=2, driver_name="spi_gpio")
    assert epdconfig.find_spi_gpio_bus(tmp_path / "spi_master") == (2, 0)


def _fake_spi_gpio_sysfs(tmp_path, bus: int, driver_name: str = "spi-gpio") -> None:
    masters = tmp_path / "spi_master"
    flash = masters / "spi0"
    flash.mkdir(parents=True)
    (flash / "device").mkdir()
    sunxi = tmp_path / "drivers" / "spi-sunxi"
    sunxi.mkdir(parents=True)
    (flash / "device" / "driver").symlink_to(sunxi)
    gpio = masters / f"spi{bus}"
    gpio.mkdir(parents=True)
    (gpio / "device").mkdir()
    spi_gpio = tmp_path / "drivers" / driver_name
    spi_gpio.mkdir(parents=True)
    (gpio / "device" / "driver").symlink_to(spi_gpio)
