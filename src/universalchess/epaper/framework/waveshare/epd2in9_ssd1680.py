"""Waveshare 2.9" SSD1680 e-paper driver (DGT Centaur V1 / IL3820-family panel).

This is the *alternate* panel driver, selected at startup only when the operator
has enabled the ``[display] il3820`` opt-in AND the primary UC8151D driver tripped
its BUSY timeout (the V1-panel signature). The working V2 driver
(``epd2in9d.py``) is never modified or replaced -- this lives alongside it.

Relationship to the V2 (UC8151D) driver:
  - BUSY polarity is INVERTED: SSD1680 drives BUSY HIGH while busy, LOW when
    idle (the V2 UC8151D is the opposite). ReadBusy() waits while HIGH.
  - Entirely different command set (RAM-window addressing, 0x24 write-RAM, LUT
    written to registers via 0x32) -- see the per-method comments.
  - Same 128x296 geometry and the same public interface the framework calls:
    ``width``/``height``/``buffer`` attributes and ``init``/``getbuffer``/
    ``display``/``DisplayPartial``/``Clear``/``sleep``/``idle_sleep``.

Provenance: the command sequence and the ``WS_20_30`` (full) / ``WF_PARTIAL_2IN9``
(partial) waveform LUTs are ported verbatim from Waveshare's reference
``epd2in9_V2.py`` (SSD1680 / GDEM029T94 V2). These bytes are panel-specific and
cannot be verified without the physical panel; they are the part most likely to
need tuning during on-hardware bring-up.

The bounded BUSY wait reuses ``EPDTimeoutError`` / ``BUSY_TIMEOUT_SECONDS`` from
the V2 driver so callers (Manager/main) catch a single timeout type and the same
init()-returns-(-1) contract holds for both drivers.
"""

# This file is part of the DGTCentaur Mods open source software
# ( https://github.com/EdNekebno/DGTCentaur )
#
# DGTCentaur Mods is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# The Waveshare-derived portions retain the BSD-style permission notice from the
# upstream e-Paper project they are ported from.

import logging
import time

from . import epd2in9d, epdconfig
from .epd2in9d import EPDTimeoutError
from .waveform_profiles import (
    DRIVER_DKE_SSD1680,
    DRIVER_IL3820,
    WaveformProfile,
    get_profile,
)

log = logging.getLogger(__name__)

# Display resolution (identical to the V2 panel: 128 x 296).
EPD_WIDTH = 128
EPD_HEIGHT = 296


class EPD:
    """SSD1680 panel driver exposing the framework's EPD interface."""

    def __init__(self, profile: WaveformProfile = None, high_contrast: bool = False):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        # Selected waveform profile -- the recipe for how the panel moves pixels:
        # a register full/partial LUT, the panel's own OTP waveform, and/or the
        # IL3820 analog additions. None selects the verified GDEM029T94 default so
        # the working bench panel is unchanged when nothing is configured. See
        # waveform_profiles for the registry and the provenance of each table.
        self.profile = profile if profile is not None else get_profile("")
        # Experimental drive-voltage override applied on top of whatever the
        # profile programs (see _apply_high_contrast). Off by default; the one
        # knob without datasheet backing, surfaced separately in the UI as
        # experimental.
        self.high_contrast = high_contrast
        # Last frame shown on the panel. Re-sent to the OLD-RAM bank (0x26) before
        # every partial refresh (see _write_partial_rams) so the differential
        # waveform diffs (currently-shown -> new) and fully clears the prior
        # frame. Initialized white to match the cold-start Clear(). Not optional:
        # init()'s SWRESET wipes 0x26, so a partial cannot rely on a baseline the
        # controller "remembers".
        self.buffer = [0xFF] * int(self.width * self.height / 8)
        # True when the most recent init() failed specifically on a BUSY timeout.
        # main reads this to populate the cross-process display-status record.
        self.busy_timeout_occurred = False

    def apply_profile(self, profile: WaveformProfile, high_contrast: bool) -> None:
        """Select a new waveform profile/override for the next init().

        Backs the live (no-reboot) profile change: the caller sets the new
        selection here, then re-runs init() followed by a full refresh so the
        panel adopts the new waveform and voltages without restarting the board
        process. None selects the verified default, matching the constructor.
        """
        self.profile = profile if profile is not None else get_profile("")
        self.high_contrast = high_contrast

    # --- "high contrast" drive voltages (experimental override) ---------------
    # Pushed harder than the GDEM029T94 trailing voltage bytes (VSH 0x41, VSL
    # 0x32, VCOM 0x36): a larger VSH darkens black pixels and a more-negative
    # VSL/VCOM raises contrast on a faint panel. An on-hardware tuning starting
    # point, not a datasheet guarantee -- the reason it is surfaced as a separate
    # experimental toggle, applied over whatever profile is selected.
    HIGH_CONTRAST_VSH1 = 0x4A  # source high (profile default 0x41)
    HIGH_CONTRAST_VSH2 = 0x00  # second source high (unused on this panel)
    HIGH_CONTRAST_VSL = 0x3A   # source low / more negative (profile default 0x32)
    HIGH_CONTRAST_VCOM = 0x44  # VCOM (profile default 0x36)

    # --- low-level SPI / GPIO (identical wiring to the V2 driver) -------------
    def reset(self):
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

    def ReadBusy(self):
        """Poll BUSY until idle, bounded by BUSY_TIMEOUT_SECONDS.

        SSD1680 BUSY polarity is the INVERSE of the UC8151D V2 driver: the line
        reads HIGH (1) while busy and LOW (0) when idle. Without a deadline an
        unresponsive/absent panel wedges the display thread, so the wait is
        bounded and raises EPDTimeoutError on expiry (init() turns that into -1).

        Raises:
            EPDTimeoutError: if BUSY does not reach idle within the timeout.
        """
        # Read the timeout from the V2 module at call time so it stays a single
        # source of truth (and remains patchable in tests) rather than a value
        # frozen at import.
        timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        while epdconfig.digital_read(self.busy_pin) == 1:  # HIGH: busy, LOW: idle
            epdconfig.delay_ms(10)
            if time.monotonic() >= deadline:
                raise EPDTimeoutError(
                    f"SSD1680 BUSY not released within {timeout}s; "
                    "panel unresponsive or not an SSD1680/IL3820-family panel"
                )

    def _full_activation_byte(self) -> int:
        """Return the 0x22 (display update control 2) payload for a full refresh.

        The payload selects the waveform source, and it differs per driver:
          - SSD1680 register LUT: 0xC7 (run the LUT written by SetLut()).
          - SSD1680 OTP / DEPG0290BS: 0xF7 (load temperature + the OTP waveform).
          - IL3820: 0xC4 (per the IL3820 reference update sequence).
        """
        if self.profile.driver == DRIVER_IL3820:
            return 0xC4
        if self.profile.driver == DRIVER_DKE_SSD1680 or self.profile.use_otp:
            return 0xF7
        return 0xC7

    def TurnOnDisplay(self):
        """Trigger a full refresh (load LUT/temp + activate) for this driver."""
        self.send_command(0x22)  # display update control 2
        self.send_data(self._full_activation_byte())
        self.send_command(0x20)  # master activation
        self.ReadBusy()

    def TurnOnDisplayPart(self):
        """Trigger a partial refresh (activation only, partial LUT preloaded)."""
        self.send_command(0x22)  # display update control 2
        self.send_data(0x0F)
        self.send_command(0x20)  # master activation
        self.ReadBusy()

    def _lut(self, lut):
        self.send_command(0x32)  # write LUT register
        for i in range(0, 153):
            self.send_data(lut[i])
        self.ReadBusy()

    def SetLut(self, lut):
        """Write the 153-byte waveform LUT plus the 6 trailing voltage bytes.

        SSD1680 (unlike the UC8151D, which uses OTP/panel-setting waveforms) is
        driven here with a register-loaded LUT; the trailing bytes program the
        gate (0x03), source (0x04) and VCOM (0x2C) voltages the waveform needs.
        """
        self._lut(lut)
        self.send_command(0x3F)
        self.send_data(lut[153])
        self.send_command(0x03)  # gate voltage
        self.send_data(lut[154])
        self.send_command(0x04)  # source voltage
        self.send_data(lut[155])  # VSH
        self.send_data(lut[156])  # VSH2
        self.send_data(lut[157])  # VSL
        self.send_command(0x2C)  # VCOM
        self.send_data(lut[158])

    def SetWindow(self, x_start, y_start, x_end, y_end):
        self.send_command(0x44)  # RAM X start/end (byte-addressed; low 3 bits ignored)
        self.send_data((x_start >> 3) & 0xFF)
        self.send_data((x_end >> 3) & 0xFF)
        self.send_command(0x45)  # RAM Y start/end
        self.send_data(y_start & 0xFF)
        self.send_data((y_start >> 8) & 0xFF)
        self.send_data(y_end & 0xFF)
        self.send_data((y_end >> 8) & 0xFF)

    def SetCursor(self, x, y):
        self.send_command(0x4E)  # RAM X address counter
        self.send_data(x & 0xFF)
        self.send_command(0x4F)  # RAM Y address counter
        self.send_data(y & 0xFF)
        self.send_data((y >> 8) & 0xFF)

    def _write_lut_raw(self, lut):
        """Write a raw register LUT via 0x32 with no trailing voltage registers.

        Used by the IL3820 (30-byte) and DEPG0290BS (153-byte) drivers, whose
        waveform tables carry no appended gate/source/VCOM bytes -- unlike the
        SSD1680 SetLut(), which writes 153 LUT bytes plus 6 voltage bytes.
        """
        self.send_command(0x32)  # write LUT register
        for b in lut:
            self.send_data(b)
        self.ReadBusy()

    def init(self):
        """Initialize the panel for the selected profile's driver strategy.

        Returns 0 on success, -1 on failure. A BUSY timeout (panel absent or not
        an SSD16xx/IL3820-family panel) is caught and reported as -1 -- the same
        contract Manager.initialize() expects from the V2 driver -- and recorded
        in ``busy_timeout_occurred`` for the startup status report.

        The init sequence, LUT format and refresh activation bytes are selected
        by ``profile.driver`` (see waveform_profiles): the V1 panel family does
        not share one protocol, so SSD1680 (Waveshare), IL3820 and DEPG0290BS
        each get their own faithful sequence.
        """
        self.busy_timeout_occurred = False
        if epdconfig.module_init() != 0:
            return -1
        try:
            self.reset()
            if self.profile.driver == DRIVER_IL3820:
                self._init_il3820()
            elif self.profile.driver == DRIVER_DKE_SSD1680:
                self._init_dke()
            else:
                self._init_ssd1680()
        except EPDTimeoutError as e:
            # Panel never signaled idle: unresponsive or not an SSD16xx-family
            # panel. Report -1 so Manager.initialize() disables the display
            # rather than hanging; main records the timeout for the status card.
            self.busy_timeout_occurred = True
            log.error(f"[EPD SSD1680] init aborted: {e}")
            return -1
        return 0

    def _init_ssd1680(self):
        """Waveshare epd2in9_V2-style SSD1680 init (the default, verified path).

        Loads the profile's 159-byte full LUT via SetLut() unless ``use_otp`` is
        set (then the panel's OTP waveform drives the full refresh at activation,
        TurnOnDisplay()'s 0xF7). Unchanged from the original SSD1680 driver so the
        working bench panel behaves exactly as before.
        """
        self.ReadBusy()
        self.send_command(0x12)  # SWRESET
        self.ReadBusy()

        self.send_command(0x01)  # driver output control
        self.send_data(0x27)
        self.send_data(0x01)
        self.send_data(0x00)

        self.send_command(0x11)  # data entry mode
        self.send_data(0x03)

        self.SetWindow(0, 0, self.width - 1, self.height - 1)

        self.send_command(0x21)  # display update control 1
        self.send_data(0x00)
        self.send_data(0x80)

        self.SetCursor(0, 0)
        self.ReadBusy()

        if not self.profile.use_otp:
            self.SetLut(self.profile.full_lut)

        # high_contrast runs LAST so its harder source/VCOM voltages override
        # whatever SetLut() just wrote.
        if self.high_contrast:
            self._apply_high_contrast()

    def _init_dke(self):
        """DEPG0290BS (SSD1680) init: OTP full refresh, register partial LUT.

        Transcribed from GxEPD2 ``GxEPD2_290_BS::_InitDisplay``. Differs from the
        Waveshare path by the border-waveform select (0x3C=0x05) and internal
        temperature-sensor select (0x18=0x80), and it loads NO full-refresh LUT
        (the panel drives full from OTP, activation 0xF7). The partial LUT is
        loaded per partial refresh by DisplayPartial().
        """
        self.send_command(0x12)  # SWRESET
        self.ReadBusy()

        self.send_command(0x01)  # driver output control
        self.send_data(0x27)
        self.send_data(0x01)
        self.send_data(0x00)

        self.send_command(0x11)  # data entry mode
        self.send_data(0x03)

        self.send_command(0x3C)  # border waveform
        self.send_data(0x05)

        self.send_command(0x21)  # display update control 1
        self.send_data(0x00)
        self.send_data(0x80)

        self.send_command(0x18)  # temperature sensor: internal
        self.send_data(0x80)

        self.SetWindow(0, 0, self.width - 1, self.height - 1)
        self.SetCursor(0, 0)
        self.ReadBusy()

        if self.high_contrast:
            self._apply_high_contrast()

    def _init_il3820(self):
        """IL3820 (GDEH029A1) init + 30-byte full LUT load.

        Transcribed from GxEPD2 ``GxEPD2_290::_InitDisplay`` / ``_Init_Full``.
        IL3820 has no SWRESET; drive voltages are programmed here (booster 0x0C,
        VCOM 0x2C, dummy-line 0x3A, gate-width 0x3B) rather than appended to the
        LUT, and the waveform is a 30-byte register LUT written via 0x32. A
        power-on (0x22=0xC0, enable clock+analog only -- no display) settles the
        panel before the first image write; the full-refresh activation byte is
        0xC4 (see TurnOnDisplay).

        high_contrast raises VCOM here (0x2C=0x44 vs 0xA8); IL3820 has no separate
        source-voltage register, so the SSD1680 0x04 override does not apply.
        """
        self.send_command(0x01)  # driver output / gate config
        self.send_data((self.height - 1) % 256)
        self.send_data((self.height - 1) // 256)
        self.send_data(0x00)

        self.send_command(0x0C)  # booster soft start
        self.send_data(0xD7)
        self.send_data(0xD6)
        self.send_data(0x9D)

        self.send_command(0x2C)  # VCOM
        self.send_data(0x44 if self.high_contrast else 0xA8)

        self.send_command(0x3A)  # dummy line period
        self.send_data(0x1A)

        self.send_command(0x3B)  # gate line width
        self.send_data(0x08)

        self.send_command(0x11)  # data entry mode
        self.send_data(0x03)

        self.SetWindow(0, 0, self.width - 1, self.height - 1)
        self.SetCursor(0, 0)
        self.ReadBusy()

        self._write_lut_raw(self.profile.full_lut)  # 30-byte IL3820 full LUT

        self.send_command(0x22)  # power on: enable clock + analog (no display)
        self.send_data(0xC0)
        self.send_command(0x20)  # master activation
        self.ReadBusy()

    def _apply_high_contrast(self):
        """Rewrite source (0x04) and VCOM (0x2C) with higher-contrast voltages.

        Invoked from init() last (after the waveform LUT and any IL3820
        additions) when ``high_contrast`` is set, so these values are the final
        word on drive voltage. Targets the "draws but faint" symptom: a higher
        VSH and more-negative VSL/VCOM push more charge into the ink. The bytes
        are an on-hardware tuning starting point, not a datasheet guarantee --
        hence the experimental toggle. If the panel still renders faint, these
        constants are the values to adjust next.
        """
        self.send_command(0x04)  # source driving voltage
        self.send_data(self.HIGH_CONTRAST_VSH1)  # VSH1
        self.send_data(self.HIGH_CONTRAST_VSH2)  # VSH2
        self.send_data(self.HIGH_CONTRAST_VSL)   # VSL
        self.send_command(0x2C)  # VCOM
        self.send_data(self.HIGH_CONTRAST_VCOM)

    def getbuffer(self, image):
        """Pack a PIL image into the 1bpp panel buffer (white=1, black=0).

        Delegates to the shared vectorized packer in the V2 driver so the
        framebuffer/scheduler produce the same byte layout regardless of which
        driver is active (the two drivers must never disagree on packing).
        """
        return epd2in9d.pack_image_to_buffer(image, self.width, self.height)

    def display(self, image):
        """Full refresh, and set the partial-refresh baseline.

        Writes the image to both the current (0x24) and "old" (0x26) RAM so a
        subsequent DisplayPartial() diffs against this frame. Mirrors Waveshare's
        ``display_Base``; the framework only calls this on full-refresh cycles.
        """
        self.send_command(0x24)  # write RAM (current)
        self.send_data2(image)
        self.send_command(0x26)  # write RAM (baseline for partial diff)
        self.send_data2(image)
        self.TurnOnDisplay()
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)

    def _write_partial_rams(self, image):
        """Load a differential partial frame: previous shown -> 0x26, new -> 0x24.

        The SSD16xx/IL3820 partial waveform transitions each pixel from its OLD
        value (RAM 0x26) to its NEW value (RAM 0x24), so the OLD RAM must hold the
        frame *currently on the panel*. Two things corrupt that if 0x26 is not
        re-loaded here: init()'s SWRESET wipes both RAM banks on every
        full->partial / deep-sleep-wake transition, and a partial otherwise never
        re-seeds 0x26. Without this, the panel diffs every partial against a stale
        (or blank) baseline and never clears the previous frame -- stacking
        content, e.g. a clock's digits drawn on top of each other.

        ``self.buffer`` is the last shown frame; writing it to 0x26 before the new
        frame to 0x24 mirrors GxEPD2's writeImageAgain (set previous = currently
        displayed). The cursor is reset between banks because a full-frame write
        advances the shared RAM address counter.

        Do NOT "optimize" this back to a single 0x24 write: dropping the 0x26
        re-seed reintroduces the partial-refresh ghosting this fixes.
        """
        previous = self.buffer
        self.SetWindow(0, 0, self.width - 1, self.height - 1)
        if previous is not None:
            self.SetCursor(0, 0)
            self.send_command(0x26)  # OLD RAM = frame currently on the panel
            self.send_data2(previous)
        self.SetCursor(0, 0)
        self.send_command(0x24)  # NEW RAM = target frame
        self.send_data2(image)

    def DisplayPartial(self, image):
        """Partial refresh diffing the previously shown frame against the new one.

        Re-arms partial mode (soft reset pulse + partial LUT + border), loads the
        differential frame (previous -> 0x26, new -> 0x24; see _write_partial_rams
        for why re-seeding 0x26 every call is mandatory), and triggers a partial
        activation.

        IL3820 and DEPG0290BS use their own partial LUT + activation (see
        _display_partial_il3820 / _display_partial_dke). For a use_otp SSD1680
        profile there is no register partial LUT to run, so a partial activation
        would have no waveform; route through the full-refresh path instead
        (correctness over speed -- every update still renders with the OTP
        waveform).
        """
        if self.profile.driver == DRIVER_IL3820:
            self._display_partial_il3820(image)
            return
        if self.profile.driver == DRIVER_DKE_SSD1680:
            self._display_partial_dke(image)
            return
        if self.profile.use_otp:
            self.display(image)
            return

        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(2)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(2)

        self.SetLut(self.profile.partial_lut)
        self.send_command(0x37)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x40)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)

        self.send_command(0x3C)  # border waveform
        self.send_data(0x80)

        self.send_command(0x22)
        self.send_data(0xC0)
        self.send_command(0x20)
        self.ReadBusy()

        self._write_partial_rams(image)
        self.TurnOnDisplayPart()
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)

    def _display_partial_il3820(self, image):
        """IL3820 partial refresh (load 30-byte partial LUT, write, activate 0x04).

        Transcribed from GxEPD2 ``GxEPD2_290::_Init_Part`` / ``_Update_Part``:
        load the partial waveform LUT, load the differential frame (previous ->
        0x26, new -> 0x24; see _write_partial_rams), then run the partial
        activation (0x22=0x04).
        """
        self._write_lut_raw(self.profile.partial_lut)
        self._write_partial_rams(image)
        self.send_command(0x22)  # display update control 2
        self.send_data(0x04)     # IL3820 partial activation
        self.send_command(0x20)  # master activation
        self.ReadBusy()
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)

    def _display_partial_dke(self, image):
        """DEPG0290BS partial refresh (load 153-byte partial LUT, write, 0xCC).

        Transcribed from GxEPD2 ``GxEPD2_290_BS::_Init_Part`` / ``_Update_Part``:
        load the register partial LUT (no voltage bytes), load the differential
        frame (previous -> 0x26, new -> 0x24; see _write_partial_rams), then run
        the partial activation (0x22=0xCC).
        """
        self._write_lut_raw(self.profile.partial_lut)
        self._write_partial_rams(image)
        self.send_command(0x22)  # display update control 2
        self.send_data(0xCC)     # DEPG0290BS partial activation
        self.send_command(0x20)  # master activation
        self.ReadBusy()
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)

    def Clear(self, color=0xFF):
        """Clear the panel to white (full refresh), reseating both RAM banks."""
        linewidth = int(self.width / 8) if self.width % 8 == 0 else int(self.width / 8) + 1
        blank = [color] * int(self.height * linewidth)
        self.send_command(0x24)
        self.send_data2(blank)
        self.TurnOnDisplay()
        self.send_command(0x26)
        self.send_data2(blank)
        self.TurnOnDisplay()
        self.buffer = [0xFF] * int(self.width * self.height / 8)

    def sleep(self):
        """Enter deep sleep and release the SPI/GPIO handles (shutdown path)."""
        self.send_command(0x10)  # deep sleep mode
        self.send_data(0x01)
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()

    def idle_sleep(self):
        """Enter deep sleep WITHOUT releasing SPI/GPIO (inactivity park).

        Mirrors the V2 driver's idle_sleep intent: settle the panel into deep
        sleep so it resists light-induced darkening, but keep the bus open so the
        scheduler can wake it via init() (which performs the hardware reset that
        exits deep sleep). The shorter delay suffices since power is not removed.
        """
        self.send_command(0x10)  # deep sleep mode
        self.send_data(0x01)
        epdconfig.delay_ms(100)
