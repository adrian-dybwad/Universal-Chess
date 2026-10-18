"""Four-color 2.9\" drivers: GDEY029F51H (JD79667) and GDEY029F52 (JD79661).

Both controllers take one RAM (command 0x10) of 2-bit pixels: black 00, white
01, yellow 10, red 11. The chess UI is 1-bit. Black and white are packed
directly. When three-color mode is on, the red mask becomes red ink. Yellow
is a real code on these panels and is not produced, because the UI has no
yellow plane.

GDEY029F51H init is GxEPD2 ``GxEPD2_290c_GDEY029F51H::_InitDisplay`` (the Good
Display demo). Resolution is 168x384, so the 128x296 chess frame is scaled.
A refresh is the full ~25 s waveform; there is no fast partial.

GDEY029F52 init is the manufacturer's Arduino demo ``EPD_init_Fast`` (11 s).
Resolution is 128x296, the same frame the UI already draws. The 16 s full
init in that demo is not used.

BUSY is active-low on both (the vendor waits until the pin reads 1).
"""

import logging
from dataclasses import dataclass
from typing import Tuple

from . import epd2in9d, epdconfig
from .epd2in9d import EPDTimeoutError
from .epd_transport import SpiPanel
from .frame_codec import pack_bwry, scale_1bpp
from .panel_models import LOGICAL_HEIGHT, LOGICAL_WIDTH, get_model

log = logging.getLogger(__name__)

# Power on (PON), the last step of each init.
_POWER_ON = 0x04

# A cold four-color refresh is specified at 25 s (F51H) and 11 s (F52 fast).
# 40 s bounds a cold panel without waiting out a hung controller.
REFRESH_TIMEOUT_SECONDS = 40.0

# Shared voltage/timing block. F52's fast path replaces the booster (0x06)
# and inserts extra commands; the bytes that are shared are the same in the
# Good Display F52 demo and in GxEPD2's F51H init.
_COMMON_HEAD = (
    (0x4D, (0x78,)),
    (0x00, (0x0F, 0x29)),
    (0x01, (0x07, 0x00)),
    (0x03, (0x10, 0x54, 0x44)),
)


def _tres(width: int, height: int):
    """TRES (0x61): source width, then gate height, each as big-endian."""
    return (0x61, (width // 256, width % 256, height // 256, height % 256))


def _common_tail(width: int, height: int):
    return (
        (0x50, (0x37,)),
        (0x60, (0x02, 0x02)),
        _tres(width, height),
        (0xE7, (0x1C,)),
        (0xE3, (0x22,)),
        (0xB4, (0xD0,)),
        (0xB5, (0x03,)),
        (0xE9, (0x01,)),
        (0x30, (0x08,)),
    )


@dataclass(frozen=True)
class ColorPanelSpec:
    """Manufacturer init for one four-color glass.

    ``phases`` is a tuple of command-pair tuples. The driver waits for BUSY
    after each phase. F52's fast demo waits after 0xA5 and again after the
    0x04 power-on; F51H waits once, after 0x04.
    """

    key: str
    name: str
    controller: str
    width: int
    height: int
    phases: Tuple[Tuple[Tuple[int, Tuple[int, ...]], ...], ...]
    refresh_pairs: Tuple[Tuple[int, Tuple[int, ...]], ...]


def f51h_spec() -> ColorPanelSpec:
    """GDEY029F51H, JD79667, 168x384. GxEPD2 init, one BUSY wait after power-on."""
    phase = (
        _COMMON_HEAD
        + ((0x06, (0x05, 0x00, 0x3F, 0x0A, 0x25, 0x12, 0x1A)),)
        + _common_tail(168, 384)
        + ((0x04, ()),)
    )
    return ColorPanelSpec(
        key="gdey029f51h",
        name="JD79667",
        controller="JD79667",
        width=168,
        height=384,
        phases=(phase,),
        # GxEPD2 _refresh writes the border waveform before the update.
        refresh_pairs=((0x50, (0x37,)), (0x12, (0x00,))),
    )


def f52_spec() -> ColorPanelSpec:
    """GDEY029F52, JD79661, 128x296. Vendor ``EPD_init_Fast``.

    Command order follows that demo, including 0x65 immediately after TRES.
    The shared tail used for F51H is a different order and is not reused here.
    """
    phase_load = (
        _COMMON_HEAD
        + (
            (0x06, (0x0F, 0x0A, 0x2F, 0x25, 0x22, 0x2E, 0x21)),
            (0x41, (0x00,)),
            (0x50, (0x37,)),
            (0x60, (0x02, 0x02)),
            _tres(128, 296),
            (0x65, (0x00, 0x00, 0x00, 0x00)),
            (0xE7, (0x1C,)),
            (0xE3, (0x22,)),
            (0xB4, (0xD0,)),
            (0xB5, (0x03,)),
            (0xE9, (0x01,)),
            (0x30, (0x08,)),
            (0xE0, (0x02,)),
            (0xE6, (0x5A,)),
            (0xA5, ()),
        )
    )
    return ColorPanelSpec(
        key="gdey029f52",
        name="JD79661",
        controller="JD79661",
        width=128,
        height=296,
        phases=(phase_load, ((0x04, ()),)),
        refresh_pairs=((0x12, (0x00,)),),
    )


_SPECS = {
    "gdey029f51h": f51h_spec,
    "gdey029f52": f52_spec,
}


def spec_for(key: str) -> ColorPanelSpec:
    """Return the init spec for a four-color panel key."""
    return _SPECS[key]()


class EPD(SpiPanel):
    """JD7966x four-color panel. ``width``/``height`` stay 128x296.

    The register block is re-sent before every frame after the first one that
    follows init(). GxEPD2 ``GxEPD2_290c_GDEY029F51H::_refresh`` clears
    ``_init_display_done`` after each refresh ("needed"), so its next write runs
    ``_InitDisplay`` again, without a reset, and that block's ``_PowerOn`` sends
    0x04 only while the power is off. ``_refresh`` leaves the power on; it goes
    off in idle_sleep() and sleep().
    """

    # Vendor code waits until BUSY reads 1, so busy is 0.
    busy_level = 0

    def __init__(self, spec: ColorPanelSpec, three_color: bool = False):
        super().__init__(get_model(spec.key))
        self.spec = spec
        self.name = spec.name
        self.three_color = three_color
        self.width = LOGICAL_WIDTH
        self.height = LOGICAL_HEIGHT
        self.buffer = [0xFF] * (LOGICAL_WIDTH * LOGICAL_HEIGHT // 8)
        self.red_buffer = [0xFF] * (LOGICAL_WIDTH * LOGICAL_HEIGHT // 8)
        # False after each refresh, so the next frame re-sends the registers.
        self._registers_loaded = False
        self._power_on = False

    def apply_three_color(self, enabled: bool) -> None:
        """Map the chess red mask onto red ink on the next refresh."""
        self.three_color = enabled

    def init(self):
        """Run the manufacturer init. Returns 0, or -1 on a BUSY timeout."""
        self.busy_timeout_occurred = False
        self.init_error = None
        if epdconfig.module_init() != 0:
            return -1
        try:
            self.reset()
            self._power_on = False
            self._load_registers(require_activity=not self._busy_activity_confirmed)
        except EPDTimeoutError as exc:
            self.busy_timeout_occurred = True
            self.init_error = str(exc)
            log.error("[EPD %s] init aborted: %s", self.name, exc)
            return -1
        self._busy_activity_confirmed = True
        log.info("[EPD %s] init ok (%s)", self.name, self.spec.key)
        return 0

    def _load_registers(self, require_activity: bool = False) -> None:
        """Send the init phases, waiting for BUSY after each; power on only if off."""
        for index, phase in enumerate(self.spec.phases):
            pairs = tuple(
                pair for pair in phase
                if not (pair[0] == _POWER_ON and self._power_on)
            )
            if not pairs:
                continue
            self.commands(pairs)
            self.ReadBusy(require_activity=(index == 0 and require_activity))
        self._power_on = True
        self._registers_loaded = True

    def getbuffer(self, image):
        """Pack the logical 1-bit frame. Color packing happens at transmit."""
        return epd2in9d.pack_image_to_buffer(image, self.width, self.height)

    def getbuffer_red(self, image):
        """Pack the red mask (0 = red), same layout as the black/white plane."""
        return epd2in9d.pack_image_to_buffer(image, self.width, self.height)

    def _to_native(self, logical):
        return scale_1bpp(
            logical, LOGICAL_WIDTH, LOGICAL_HEIGHT, self.spec.width, self.spec.height,
        )

    def _transmit(self, black_white, red, should_abort=None):
        native_bw = self._to_native(black_white)
        native_red = self._to_native(red) if red is not None else None
        packed = pack_bwry(native_bw, self.spec.width, self.spec.height, native_red)
        if not self._registers_loaded:
            self._load_registers()
        self.send_command(0x10)
        self.send_data2(packed)
        self.commands(self.spec.refresh_pairs)
        self._registers_loaded = False
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS, should_abort=should_abort)
        self.buffer = list(black_white)
        if red is not None:
            self.red_buffer = list(red)

    def display(self, image, should_abort=None):
        """Full refresh of a black/white frame. No red, no yellow."""
        self._transmit(image, None, should_abort=should_abort)

    def display_color(self, bw_buf, red_buf, should_abort=None):
        """Full refresh with the red mask packed as red ink."""
        self._transmit(bw_buf, red_buf, should_abort=should_abort)

    def DisplayPartial(self, image):
        """There is no fast partial. A partial request is a full refresh."""
        log.info("[EPD %s] partial requested; running a full four-color refresh", self.name)
        self.display(image)

    def Clear(self, color=0xFF):
        """Full-refresh to white (every 2-bit pixel ``01``, byte ``0x55``)."""
        blank = [0xFF] * (LOGICAL_WIDTH * LOGICAL_HEIGHT // 8)
        self.display(blank)

    def _power_off_and_sleep(self):
        self.send_command(0x02)
        self.send_data(0x00)
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS)
        self.send_command(0x07)
        self.send_data(0xA5)
        self._power_on = False
        self._registers_loaded = False

    def sleep(self):
        """Power off, deep sleep, and release SPI/GPIO."""
        self._power_off_and_sleep()
        epdconfig.delay_ms(200)
        epdconfig.module_exit()

    def idle_sleep(self):
        """Deep sleep with the bus left open so init() can wake the panel."""
        self._power_off_and_sleep()
        epdconfig.delay_ms(100)
