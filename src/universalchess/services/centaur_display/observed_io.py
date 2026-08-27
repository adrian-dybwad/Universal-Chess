"""Last Translate Mode GPIO/SPI observations from the display shim.

The original Centaur process talks to a virtual ``/dev/gpiomem``. The
``LD_PRELOAD`` shim records which BCM pins that process wrote and which
``/dev/spidev`` node it opened; the gateway writes them here. Universal Chess
restarts after Centaur exits, so the Settings card reads this file rather than
live process state.

Direct Mode never loads the shim, so a Direct Mode launch leaves this file
unchanged. The numbers are what that Centaur build actually opened, not
Universal Chess's epdconfig map.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping

from universalchess.paths import TMP_DIR

DEFAULT_PATH = f"{TMP_DIR}/centaur_io_observed.json"

_BCM_PIN_MAX = 27
_SPI_PATH_PREFIX = "/dev/spidev"
_SPI_PATH_MAX_LEN = 63


def empty_observed_io() -> dict[str, list]:
    """JSON-serializable empty observation (no Translate Mode run recorded)."""
    return {"gpio_pins": [], "spi_devices": []}


def write_observed_io(
    gpio_pins: Iterable[int],
    spi_devices: Iterable[str],
    *,
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Atomically persist the pins and SPI nodes seen in the current stream.

    Overwrites the previous session as soon as this launch sends observations.
    A later launch that sends nothing leaves the last successful file in place.
    """
    target = os.fspath(path) if path is not None else DEFAULT_PATH
    payload = {
        "gpio_pins": _sanitize_pins(gpio_pins),
        "spi_devices": _sanitize_spi(spi_devices),
    }
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{target}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, target)


def read_observed_io(*, path: str | os.PathLike[str] | None = None) -> dict[str, list]:
    """Return sanitized observations, or empty lists if the file is missing or corrupt."""
    target = os.fspath(path) if path is not None else DEFAULT_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, TypeError):
        return empty_observed_io()
    if not isinstance(raw, Mapping):
        return empty_observed_io()
    return {
        "gpio_pins": _sanitize_pins(raw.get("gpio_pins")),
        "spi_devices": _sanitize_spi(raw.get("spi_devices")),
    }


def _sanitize_pins(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    pins: set[int] = set()
    for item in raw:
        # bool is a subclass of int; accepting True would invent pin 1.
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if 0 <= item <= _BCM_PIN_MAX:
            pins.add(item)
    return sorted(pins)


def _sanitize_spi(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    devices: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        if not item.startswith(_SPI_PATH_PREFIX):
            continue
        if ".." in item or "\x00" in item or len(item) > _SPI_PATH_MAX_LEN:
            continue
        if item not in devices:
            devices.append(item)
    return devices
