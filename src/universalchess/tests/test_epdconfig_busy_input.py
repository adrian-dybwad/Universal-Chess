"""Tests that the e-paper BUSY line is a polled input, never a gpiozero.Button.

Why these tests exist:
    The BUSY line is only ever read synchronously (``epdconfig.digital_read`` ->
    ``GPIO_BUSY_PIN.value``); no edge/hold callback is ever attached. Creating it
    as a ``gpiozero.Button`` is a performance regression: Button enables the
    lgpio edge-alert machinery, whose alert thread (``lgPthAlert``) ``ppoll``s
    with a hardcoded 0.5 ms timeout -- a ~2000 Hz wake loop that consumes ~10% of
    a single armv6 core continuously, whether or not the panel is refreshing. A
    plain ``InputDevice`` claims the line for value reads only (a single ioctl),
    so the alert thread stays parked and that CPU is reclaimed with no behavior
    change (``.value`` semantics are identical for ``pull_up=False``: 1 == HIGH).

How a regression manifests:
    If BUSY is reverted to ``gpiozero.Button`` (or any event/hold device),
    ``test_busy_pin_is_polled_input_device`` sees ``Button`` constructed (or
    ``InputDevice`` not constructed) and fails -- catching the reintroduction of
    the 2000 Hz alert-thread CPU cost before it ships.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

# Mock the hardware libraries the driver imports at module load (spidev/gpiozero
# are not installed in the test environment). Mirrors the pattern used by the
# other e-paper tests in this package.
for _mod in ('spidev', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.framework.waveshare import epdconfig


class BusyPinPolledInputTests(unittest.TestCase):
    """The BUSY pin must be constructed as a value-only input device."""

    def test_busy_pin_is_polled_input_device(self):
        # Instantiate RaspberryPi() against a fresh gpiozero mock so the
        # assertions reflect only this construction, not the module-load instance
        # or any device created by other tests sharing the global gpiozero mock.
        fake_gpiozero = MagicMock()
        with patch.dict(sys.modules, {'gpiozero': fake_gpiozero}):
            epdconfig.RaspberryPi()

        # The BUSY line must be a plain InputDevice claimed for value reads only,
        # with pull_up=False preserved so 1 == HIGH == busy (the polarity the
        # driver's ReadBusy() depends on).
        fake_gpiozero.InputDevice.assert_called_once_with(
            epdconfig.RaspberryPi.BUSY_PIN, pull_up=False
        )
        # Button (event/hold device) must never back BUSY: it would restart the
        # lgpio alert thread's ~2000 Hz ppoll loop that this guards against.
        fake_gpiozero.Button.assert_not_called()


if __name__ == '__main__':
    unittest.main()
