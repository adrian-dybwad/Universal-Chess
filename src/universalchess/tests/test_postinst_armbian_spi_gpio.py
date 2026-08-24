"""Tests for the postinst helper that loads spi-gpio on Orange Pi.

Why these tests exist:
    Centaur e-paper is on the Pi SPI1 header pins (PI2/PI3/PI4/PI1), not the
    SoC SPI0/SPI1 pinmux (PC flash / PH Pi-SPI0 header). postinst must add the
    spi-gpio user overlay on Orange Pi and must not enable ``spidev0_0`` /
    ``spidev1_0``. Raspberry Pi boards keep
    dtoverlay=spi1-1cs in config.txt and must not get this overlay.

How a regression manifests:
    - ``overlays=spidev0_0``: clocks onboard NOR, not the panel.
    - Missing ``user_overlays=uc-centaur-spi-gpio`` on Orange Pi: no spidev
      after reboot, libgpiod backend cannot find spi-gpio.
    - Helper running for a Raspberry Pi model: Armbian overlay applied on Pi.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import universalchess.services.update_service as um

POSTINST = (
    Path(um.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

HELPER = "configure_armbian_spi_gpio"

STOCK = """verbosity=1
bootlogo=false
console=none
disp_mode=1920x1080p60
overlay_prefix=sun50i-h616
rootdev=UUID=e3393778-406f-4935-a4f1-58d4f7a05f78
rootfstype=ext4
extraargs=console=tty1
"""


@pytest.fixture
def configure_armbian_spi_gpio(tmp_path):
    """Run the shipped helper against a temp armbianEnv.txt."""
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    text = POSTINST.read_text()
    match = re.search(rf"(?sm)^{HELPER}\(\) \{{.*?^\}}", text)
    assert match, f"{HELPER} not found in postinst"
    source = match.group(0)
    dts = tmp_path / "uc-centaur-spi-gpio.dts"
    dts.write_text("/dts-v1/;\n/plugin/;\n")
    (tmp_path / "uc-centaur-spi-gpio-h3.dts").write_text("/dts-v1/;\n/plugin/;\n")
    counter = {"n": 0}

    def run(env_text: str, model: str) -> str:
        counter["n"] += 1
        target = tmp_path / f"armbianEnv{counter['n']}.txt"
        target.write_text(env_text)
        proc = subprocess.run(  # noqa: S603 - runs the postinst's own function
            [
                "/bin/sh",
                "-c",
                f'{source}\n{HELPER} "$1" "$2" "$3"',
                "sh",
                str(target),
                str(dts),
                model,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
        return target.read_text()

    return run


def test_h616_zero2w_adds_h616_spi_gpio_user_overlay(configure_armbian_spi_gpio):
    # Why: Zero 2W is the measured H618 40-pin board. Manifests as no
    # user_overlays=uc-centaur-spi-gpio.
    result = configure_armbian_spi_gpio(STOCK, "OrangePi Zero 2W")
    assert re.search(r"^user_overlays=.*uc-centaur-spi-gpio(\s|$)", result, re.MULTILINE)
    assert "uc-centaur-spi-gpio-h3" not in result
    assert "spidev0_0" not in result
    assert "spidev1_0" not in result


@pytest.mark.parametrize(
    "model",
    ["Orange Pi PC", "OrangePi One", "Orange Pi Plus 2E", "Orange Pi Plus", "Orange Pi Plus 2"],
)
def test_h3_40pin_adds_h3_spi_gpio_user_overlay(configure_armbian_spi_gpio, model):
    # Why: H3/H5 40-pin boards need the PD/PA/PG overlay, not PI-bank H616.
    result = configure_armbian_spi_gpio(STOCK, model)
    assert re.search(r"^user_overlays=.*uc-centaur-spi-gpio-h3", result, re.MULTILINE)
    assert re.search(r"^overlays=.*uart3(\s|$)", result, re.MULTILINE)


def test_h616_zero2w_does_not_enable_h3_uart3(configure_armbian_spi_gpio):
    # Why: H616 header UART is uart0/ttyS0. overlays=uart3 would mux the
    # wrong pads. Manifests as uart3 appearing in overlays=.
    result = configure_armbian_spi_gpio(STOCK, "OrangePi Zero 2W")
    assert "uart3" not in result


@pytest.mark.parametrize("model", ["Orange Pi 5", "OrangePi-5", "Orange Pi 5 Plus", "OrangePi Zero", "Orange Pi Zero 3", "Orange Pi 3B"])
def test_orangepi_without_40pin_map_does_not_add_spi_gpio_overlay(
    configure_armbian_spi_gpio, model
):
    # Why: RK3588 and 26-pin Allwinner headers are not the H616 40-pin map.
    # Loading PI-bank spi-gpio would claim the wrong pads.
    result = configure_armbian_spi_gpio(STOCK, model)
    assert "uc-centaur-spi-gpio" not in result


def test_raspberry_pi_model_does_not_get_sunxi_spi_gpio(configure_armbian_spi_gpio):
    # Why: Pi e-paper stays on dtoverlay=spi1-1cs. Manifests as
    # user_overlays=uc-centaur-spi-gpio on a Pi armbianEnv.
    result = configure_armbian_spi_gpio(STOCK, "Raspberry Pi Zero 2 W Rev 1.0")
    assert "uc-centaur-spi-gpio" not in result


def test_second_run_does_not_duplicate_user_overlay(configure_armbian_spi_gpio):
    # Why: postinst runs on every upgrade. Duplicating the overlay name
    # would apply it twice. Manifests as uc-centaur-spi-gpio appearing twice.
    once = configure_armbian_spi_gpio(STOCK, "OrangePi Zero 2W")
    twice = configure_armbian_spi_gpio(once, "OrangePi Zero 2W")
    assert twice.count("uc-centaur-spi-gpio") == 1
