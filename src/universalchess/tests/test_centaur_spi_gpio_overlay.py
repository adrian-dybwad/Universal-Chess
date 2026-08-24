"""Centaur e-paper spi-gpio overlays, one per Allwinner 40-pin family.

Why these tests exist:
    Hardware SPI overlays mux the wrong pads. The Centaur panel is the Pi SPI1
    header (35/38/40/12). H616/H618 Zero 2W uses PI2/PI4/PI3/PI1; H3/H5 40-pin
    boards use PA10/PG6/PG7/PD14. One overlay cannot cover both.

How a regression manifests:
    - H616 overlay using PH6 or PC0: clocks flash or the Pi SPI0 header.
    - H3 overlay using PI bank 8: those pins are not on the H3 header.
    - H616 overlay listing H6 / Zero 3 / Rockchip: wrong phandles apply.
"""

from __future__ import annotations

from pathlib import Path

import universalchess.board.profile as profile_module

OVERLAY_DIR = Path(profile_module.__file__).resolve().parent / "overlays"
OVERLAY_H616 = OVERLAY_DIR / "uc-centaur-spi-gpio.dts"
OVERLAY_H3 = OVERLAY_DIR / "uc-centaur-spi-gpio-h3.dts"


def test_overlay_files_are_shipped_next_to_the_board_profile():
    # Why: postinst compiles these files on the board. A missing path means
    # armbian-add-overlay never runs and /dev/spidev never appears.
    assert OVERLAY_H616.is_file(), f"missing overlay: {OVERLAY_H616}"
    assert OVERLAY_H3.is_file(), f"missing overlay: {OVERLAY_H3}"


def test_h616_overlay_is_spi_gpio_on_measured_pi_spi1_header_pins():
    # Why: live pinctrl PI1=257 PI2=258 PI3=259 PI4=260. Allwinner gpio-cells
    # are <bank pin flags> with PI = bank 8. Header 12/35/38/40.
    text = OVERLAY_H616.read_text()
    assert 'compatible = "spi-gpio"' in text
    assert "sck-gpios = <&pio 8 3 0>" in text  # PI3, header 40, SCLK
    assert "mosi-gpios = <&pio 8 4 0>" in text  # PI4, header 38, MOSI
    assert "miso-gpios = <&pio 8 2 0>" in text  # PI2, header 35, MISO
    assert "cs-gpios = <&pio 8 1 1>" in text  # PI1, header 12, CS active-low


def test_h616_overlay_does_not_enable_stock_hardware_spi_pinmux():
    # Why: sun50i-h616-spidev0_0 / spidev1_0 are the wrong header pins.
    text = OVERLAY_H616.read_text()
    assert "spidev0_0" not in text
    assert "spidev1_0" not in text
    assert "PH6" not in text
    assert "PH7" not in text
    assert "PH8" not in text
    assert "PC0" not in text
    assert "PC2" not in text
    assert "PC4" not in text


def test_h616_overlay_compatible_is_h616_family_not_h6_or_rockchip():
    # Why: PI-bank phandles and CCU clock 27 are H616/H618. H6 and RK3588
    # do not have those. Manifests as sun50i-h6 or orangepi-5 in this file.
    text = OVERLAY_H616.read_text()
    assert "xunlong,orangepi-zero2w" in text
    assert 'allwinner,sun50i-h618' in text
    assert 'allwinner,sun50i-h616' in text
    assert "sun50i-h6\"" not in text.replace("sun50i-h618", "").replace("sun50i-h616", "")
    assert "rockchip,rk3588" not in text
    assert "xunlong,orangepi-5" not in text


def test_h3_overlay_is_spi_gpio_on_sunxi_40pin_header_pins():
    # Why: linux-sunxi H3/H5 40-pin table: header 12 PD14, 35 PA10, 38 PG6,
    # 40 PG7. Banks PD=3 PA=0 PG=6. H616 PI banks would miss the header.
    text = OVERLAY_H3.read_text()
    assert 'compatible = "spi-gpio"' in text
    assert "sck-gpios = <&pio 6 7 0>" in text  # PG7, header 40
    assert "mosi-gpios = <&pio 6 6 0>" in text  # PG6, header 38
    assert "miso-gpios = <&pio 0 10 0>" in text  # PA10, header 35
    assert "cs-gpios = <&pio 3 14 1>" in text  # PD14, header 12, active-low
    assert "xunlong,orangepi-pc" in text
    assert "allwinner,sun8i-h3" in text
    assert "allwinner,sun50i-h5" in text
    assert "assigned-clocks = <&ccu 27>" not in text
    assert "xunlong,orangepi-zero2w" not in text
    assert "xunlong,orangepi-plus2" in text
    assert "target = <&uart3>" in text
    assert "status = \"okay\"" in text.split("target = <&uart3>")[1]


def test_h616_overlay_sets_apb2_so_uart0_can_make_1mbps():
    # Why: APB2 defaults to OSC24M. 24e6/(16*1e6)=1.5, so 8250 rounds to
    # divisor 2 = 750 kbaud. The Centaur MCU is 1 Mbps; discovery then
    # shows tx climbing and rx staying 0. CLK_APB2 is 27; CLK_PLL_PERIPH0
    # is 4. 300 MHz is 600 MHz / 2.
    text = OVERLAY_H616.read_text()
    assert "target = <&uart0>" in text
    assert "assigned-clocks = <&ccu 27>" in text
    assert "assigned-clock-parents = <&ccu 4>" in text
    assert "assigned-clock-rates = <300000000>" in text
