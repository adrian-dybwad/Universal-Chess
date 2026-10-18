"""SPI command helpers shared by the replacement-panel drivers.

The Centaur reset pulse (high, 2 ms low, high) is the one the stock drivers
use on this board's reset wiring. Vendor demos that hold reset low for 40 ms
assume a directly wired pin; that pulse is not used here.
"""

import time

from . import epd2in9d, epdconfig
from .epd2in9d import EPDTimeoutError, RefreshInterrupted
from .panel_models import PanelDriver, PanelModel


class SpiPanel(PanelDriver):
    """Command/data/BUSY transport. Subclasses set ``busy_level`` and ``name``."""

    # Level of the BUSY pin while the controller is busy. SSD1680/SSD1685 are
    # busy-high; UC8151D and the JD7966x color controllers are busy-low.
    busy_level = 1
    name = "panel"

    def __init__(self, panel: PanelModel):
        self._adopt_panel(panel)
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.busy_timeout_occurred = False
        self._busy_activity_confirmed = False
        self.init_error = None

    def reset(self):
        """Pulse reset the way the stock Centaur drivers do."""
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(50)
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(2)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(50)

    def send_command(self, command):
        epdconfig.digital_write(self.dc_pin, 0)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte([command])
        epdconfig.digital_write(self.cs_pin, 1)

    def send_data(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte([data])
        epdconfig.digital_write(self.cs_pin, 1)

    def send_data2(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte2(data)
        epdconfig.digital_write(self.cs_pin, 1)

    def commands(self, pairs):
        """Send ``(command, data_bytes)`` pairs in order."""
        for command, data in pairs:
            self.send_command(command)
            for byte in data:
                self.send_data(byte)

    def ReadBusy(self, timeout_seconds=None, should_abort=None, require_activity=False):
        """Poll BUSY until idle, bounded by ``timeout_seconds``.

        ``busy_level`` is the pin value while the controller is busy. A missing
        panel whose pin sits at the idle level never looks busy, so the first
        successful init on an instance can require the line to leave idle
        first (``require_activity``). Later waits accept an already-idle line,
        because a command that does not pulse BUSY is not a missing panel.

        Raises:
            EPDTimeoutError: BUSY did not reach idle, or never left idle when
                activity was required.
            RefreshInterrupted: ``should_abort`` returned True. The refresh
                did not finish; the caller must not store this frame as the
                baseline.
        """
        timeout = (
            timeout_seconds if timeout_seconds is not None
            else epd2in9d.BUSY_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + timeout
        activity_deadline = time.monotonic() + epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS
        saw_busy = False
        while True:
            is_busy = epdconfig.digital_read(self.busy_pin) == self.busy_level
            if is_busy:
                saw_busy = True
            elif saw_busy or not require_activity:
                return
            elif time.monotonic() >= activity_deadline:
                raise EPDTimeoutError(
                    f"{self.name} BUSY stayed idle for "
                    f"{epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS}s; "
                    "no panel on the connector"
                )
            if should_abort is not None and should_abort():
                raise RefreshInterrupted(
                    f"{self.name} BUSY wait aborted: newer frame pending"
                )
            if saw_busy and time.monotonic() >= deadline:
                raise EPDTimeoutError(
                    f"{self.name} BUSY not released within {timeout}s"
                )
            if saw_busy:
                epdconfig.delay_ms(10)
