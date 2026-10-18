"""Good Display GDEY029T71H driver (SSD1685, 168x384).

The chess UI composites a 128x296 frame. This driver scales that frame to the
panel's 168x384 and addresses RAM with the 8-pixel source shift GxEPD2
documents (sources S8..S175 are the ones bonded to the glass).

Command bytes are transcribed from GxEPD2 ``GxEPD2_290_GDEY029T71H``
(Jean-Marc Zingg), itself from the Good Display demo. BUSY is active-high,
the same polarity as the SSD1680.

Partial refresh is differential: the previous frame is written to 0x26 and
the new frame to 0x24 before activation 0xDC, for the same reason the SSD1680
driver re-seeds old RAM on every partial.
"""

import logging

from . import epd2in9d, epdconfig
from .epd2in9d import EPDTimeoutError
from .epd_transport import SpiPanel
from .frame_codec import scale_1bpp
from .panel_models import LOGICAL_HEIGHT, LOGICAL_WIDTH, PanelModel, get_model

log = logging.getLogger(__name__)

PANEL_KEY_T71H = "gdey029t71h"

NATIVE_WIDTH = 168
NATIVE_HEIGHT = 384
# GxEPD2 SOURCE_SHIFT: the glass is wired to sources S8..S175, not S0.
SOURCE_SHIFT = 8
REFRESH_TIMEOUT_SECONDS = 30.0

# Fast-full temperature byte and activation, from GxEPD2 _Update_Full with
# useFastFullUpdate left at its default true.
_FAST_TEMPERATURE = (0x6E, 0x00)
_FAST_LOAD_TEMPERATURE = 0x91
_FAST_ACTIVATION = 0xC7
_PARTIAL_ACTIVATION = 0xDC


def ram_window_pairs():
    """RAM window commands for the shifted 168x384 visible area.

    x starts at ``SOURCE_SHIFT`` (8). The data written afterwards is exactly
    the visible pixels; the shift is applied in the window, not by padding
    the buffer.
    """
    x = SOURCE_SHIFT
    y = 0
    w = NATIVE_WIDTH
    h = NATIVE_HEIGHT
    x_end = x + w - 1
    y_end = y + h - 1
    return (
        (0x11, (0x03,)),
        (0x44, (x // 8, x_end // 8)),
        (0x45, (y % 256, y // 256, y_end % 256, y_end // 256)),
        (0x4E, (x // 8,)),
        (0x4F, (y % 256, y // 256)),
    )


def init_pairs():
    """Init commands after SWRESET, from GxEPD2 ``_InitDisplay``."""
    gate = NATIVE_HEIGHT - 1
    return (
        (0x01, (gate % 256, gate // 256, 0x00)),
        (0x3C, (0x05,)),
        (0x18, (0x80,)),
        # 0x21 must precede the window: OTP is 168x384 but the bonded sources
        # are shifted, and GxEPD2 writes 0x00, 0x00 here ("200x384").
        (0x21, (0x00, 0x00)),
    ) + ram_window_pairs()


def full_update_pairs():
    """Fast full-refresh activation, from GxEPD2 ``_Update_Full``."""
    return (
        (0x21, (0x40, 0x00)),
        (0x1A, _FAST_TEMPERATURE),
        (0x22, (_FAST_LOAD_TEMPERATURE,)),
        (0x20, ()),
    )


def full_update_activation_pairs():
    """Second half of the fast full update, after the 2 ms temperature load."""
    return (
        (0x22, (_FAST_ACTIVATION,)),
        (0x20, ()),
    )


def partial_update_pairs():
    """Partial activation, from GxEPD2 ``_Update_Part``."""
    return (
        (0x21, (0x00, 0x00)),
        (0x22, (_PARTIAL_ACTIVATION,)),
        (0x20, ()),
    )


class EPD(SpiPanel):
    """SSD1685 panel. ``width``/``height`` stay 128x296 for the chess UI."""

    busy_level = 1
    name = "SSD1685"

    def __init__(self, panel: PanelModel = get_model(PANEL_KEY_T71H)):
        super().__init__(panel)
        # The published T71H sequence is black/white, with no red plane and
        # no waveform table: the manufacturer init is the whole recipe.
        self.three_color = False
        self.width = LOGICAL_WIDTH
        self.height = LOGICAL_HEIGHT
        self.buffer = [0xFF] * (LOGICAL_WIDTH * LOGICAL_HEIGHT // 8)

    def apply_three_color(self, enabled: bool) -> None:
        """Ignored. This glass has no red channel in the published sequence."""
        self.three_color = False

    def init(self):
        """Initialize the panel. Returns 0, or -1 on a BUSY timeout."""
        self.busy_timeout_occurred = False
        self.init_error = None
        if epdconfig.module_init() != 0:
            return -1
        try:
            self.reset()
            self.send_command(0x12)  # SWRESET
            self.ReadBusy(require_activity=not self._busy_activity_confirmed)
            self.commands(init_pairs())
        except EPDTimeoutError as exc:
            self.busy_timeout_occurred = True
            self.init_error = str(exc)
            log.error("[EPD SSD1685] init aborted: %s", exc)
            return -1
        self._busy_activity_confirmed = True
        log.info("[EPD SSD1685] init ok (GDEY029T71H)")
        return 0

    def getbuffer(self, image):
        """Pack the logical 128x296 frame. Scaling happens at transmit time."""
        return epd2in9d.pack_image_to_buffer(image, self.width, self.height)

    def _native(self, logical):
        return scale_1bpp(
            logical, LOGICAL_WIDTH, LOGICAL_HEIGHT, NATIVE_WIDTH, NATIVE_HEIGHT,
        )

    def _write_ram(self, command, native):
        self.commands(ram_window_pairs())
        self.send_command(command)
        self.send_data2(native)

    def _activate_full(self, should_abort=None):
        self.commands(full_update_pairs())
        epdconfig.delay_ms(2)
        self.commands(full_update_activation_pairs())
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS, should_abort=should_abort)

    def display(self, image, should_abort=None):
        """Full refresh of a logical 1-bit frame, scaled onto the glass."""
        native = self._native(image)
        self._write_ram(0x26, native)
        self._write_ram(0x24, native)
        self._activate_full(should_abort=should_abort)
        self.buffer = list(image)

    def DisplayPartial(self, image):
        """Partial refresh diffing the previous logical frame against this one."""
        previous = self._native(self.buffer)
        native = self._native(image)
        self._write_ram(0x26, previous)
        self._write_ram(0x24, native)
        self.commands(partial_update_pairs())
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS)
        self.buffer = list(image)

    def display_color(self, bw_buf, red_buf, should_abort=None):
        """No red channel. Draws the black/white plane as a full refresh."""
        self.display(bw_buf, should_abort=should_abort)

    def Clear(self, color=0xFF):
        """Full-refresh both RAM banks to white."""
        blank = [color] * (LOGICAL_WIDTH * LOGICAL_HEIGHT // 8)
        self.display(blank)

    def sleep(self):
        """Deep sleep and release SPI/GPIO."""
        self.idle_sleep()
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()

    def idle_sleep(self):
        """Deep sleep with the bus left open so init() can wake the panel."""
        self.send_command(0x10)
        self.send_data(0x01)
        epdconfig.delay_ms(100)
