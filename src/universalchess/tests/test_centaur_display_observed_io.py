"""Persistence of GPIO/SPI lines observed from a Translate Mode Centaur run.

Why these tests exist: the Settings card must show the pins the imported
Centaur process actually opened. Universal Chess restarts after that process
exits, so the gateway writes a JSON file the web process reads later. Filling
the file from Universal Chess's epdconfig, or accepting a hand-edited path
outside ``/dev/spidev``, would describe the wrong program.

How a regression manifests: round-trip pins/SPI paths differ; a missing or
corrupt file invents BCM 12/16/7/18; a ``/etc/passwd`` path survives sanitization.
"""

from __future__ import annotations

import json

from universalchess.services.centaur_display.observed_io import (
    read_observed_io,
    write_observed_io,
)


def test_write_then_read_round_trips_sorted_unique_pins_and_spi_paths(tmp_path):
    """A gateway flush must be what the diagnostics endpoint later returns.

    Failure: order/duplication changes, or the read misses a pin the shim sent.
    """
    path = tmp_path / "centaur_io_observed.json"
    write_observed_io([18, 7, 7, 12, 16], ["/dev/spidev1.0"], path=path)

    assert read_observed_io(path=path) == {
        "gpio_pins": [7, 12, 16, 18],
        "spi_devices": ["/dev/spidev1.0"],
    }


def test_missing_file_is_empty_not_universal_chess_pins(tmp_path):
    """No Translate Mode run yet must not invent UC's RST/DC/BUSY/CS map.

    Failure: gpio_pins comes back as [7, 12, 16, 18] with no file behind it.
    """
    assert read_observed_io(path=tmp_path / "absent.json") == {
        "gpio_pins": [],
        "spi_devices": [],
    }


def test_corrupt_file_is_empty_not_partial_or_invented(tmp_path):
    """A truncated JSON file must not become a plausible pin map.

    Failure: json.load raises into the endpoint, or a default 12/16/7/18 is used.
    """
    path = tmp_path / "centaur_io_observed.json"
    path.write_text("{not-json", encoding="utf-8")

    assert read_observed_io(path=path) == {"gpio_pins": [], "spi_devices": []}


def test_read_drops_out_of_range_pins_and_non_spidev_paths(tmp_path):
    """A hand-edited file must not inject header-impossible pins or other paths.

    Failure: pin 99 or ``/etc/passwd`` appears in the card payload.
    """
    path = tmp_path / "centaur_io_observed.json"
    path.write_text(
        json.dumps(
            {
                "gpio_pins": [7, 99, True, "12", 12],
                "spi_devices": ["/dev/spidev1.0", "/etc/passwd", "/dev/gpiomem"],
            }
        ),
        encoding="utf-8",
    )

    assert read_observed_io(path=path) == {
        "gpio_pins": [7, 12],
        "spi_devices": ["/dev/spidev1.0"],
    }


def test_write_sanitizes_before_the_file_lands(tmp_path):
    """The gateway must not persist values the card would then have to distrust.

    Failure: the file on disk contains pin 99 or a non-spidev path.
    """
    path = tmp_path / "centaur_io_observed.json"
    write_observed_io([7, 99], ["/dev/spidev1.0", "/home/centaur/not-spidev"], path=path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"gpio_pins": [7], "spi_devices": ["/dev/spidev1.0"]}
