"""epd2in9d must import on boards that do not ship RPi.GPIO.

Why these tests exist:
    The Waveshare driver still executed ``import RPi.GPIO as GPIO`` at module
    load even though every pin and SPI call goes through epdconfig. Raspberry
    Pi OS has that package; Armbian on the Orange Pi Zero 2W does not. The
    board service then crash-looped with ``ModuleNotFoundError: No module
    named 'RPi'`` before the libgpiod backend could run.

How a regression manifests:
    ``universal-chess.service`` exits 1 on every start and
    ``/var/log/modmenuoutput.log`` fills with that ModuleNotFoundError.
"""

from __future__ import annotations

import re
from pathlib import Path

import universalchess.epaper.framework.waveshare.epd2in9d as epd2in9d

SOURCE = Path(epd2in9d.__file__).resolve()


def test_epd2in9d_source_does_not_import_rpi_gpio():
    # Why: a leftover top-level import is enough to kill Armbian. Manifests
    # as ``import RPi.GPIO`` or ``from RPi`` returning to this file.
    text = SOURCE.read_text()
    assert not re.search(r"^\s*import RPi(\\.GPIO)?\b", text, re.MULTILINE)
    assert not re.search(r"^\s*from RPi(\\.GPIO)?\b", text, re.MULTILINE)
