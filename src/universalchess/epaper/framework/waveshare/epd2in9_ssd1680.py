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
from .waveform_profiles import WaveformProfile, get_profile

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
        # Last image sent, kept for parity with the V2 driver's interface. The
        # SSD1680 itself holds the partial-refresh baseline in its 0x26 RAM (set
        # by display()), so this is not re-sent to the controller per partial.
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

    def TurnOnDisplay(self):
        """Trigger a full refresh (load LUT/temp + activate).

        The display-update-control-2 (0x22) payload selects the waveform source:
        0xC7 runs the register-loaded LUT written by SetLut(); 0xF7 additionally
        loads temperature and the panel's OTP waveform. The OTP byte is used only
        for a use_otp profile (init() then skips SetLut()).
        """
        self.send_command(0x22)  # display update control 2
        self.send_data(0xF7 if self.profile.use_otp else 0xC7)
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

    def init(self):
        """Initialize the SSD1680 panel.

        Returns 0 on success, -1 on failure. A BUSY timeout (panel absent or not
        an SSD1680/IL3820-family panel) is caught and reported as -1 -- the same
        contract Manager.initialize() expects from the V2 driver -- and recorded
        in ``busy_timeout_occurred`` for the startup status report.
        """
        self.busy_timeout_occurred = False
        if epdconfig.module_init() != 0:
            return -1
        try:
            self.reset()

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

            # use_otp profile: skip the register LUT so the panel uses its
            # built-in (OTP) waveform, loaded at activation by TurnOnDisplay()'s
            # 0xF7. Otherwise load this profile's full-refresh table.
            if not self.profile.use_otp:
                self.SetLut(self.profile.full_lut)

            if self.profile.il3820_additions:
                self._apply_il3820_additions()

            # high_contrast runs LAST so its harder source/VCOM voltages override
            # whatever SetLut() or the IL3820 additions just wrote.
            if self.high_contrast:
                self._apply_high_contrast()
        except EPDTimeoutError as e:
            # Panel never signaled idle: unresponsive or not an SSD1680-family
            # panel. Report -1 so Manager.initialize() disables the display
            # rather than hanging; main records the timeout for the status card.
            self.busy_timeout_occurred = True
            log.error(f"[EPD SSD1680] init aborted: {e}")
            return -1
        return 0

    def _apply_il3820_additions(self):
        """Apply the optional IL3820/SSD1608-specific analog + waveform setup.

        Invoked from init() only when ``il3820_additions`` is set. A true IL3820
        panel (the period-appropriate V1 controller) has no usable OTP waveform
        and relies on explicit analog programming the SSD1680 path skips:

          - 0x0C booster soft start
          - 0x3A dummy-line period, 0x3B gate-line width
          - 0x03 gate driving voltage, 0x04 source driving voltage, 0x2C VCOM

        These bytes are the IL3820 datasheet defaults and are the primary
        on-hardware tuning point: if a genuine V1 panel renders blank/ghosted
        with the base SSD1680 path, enabling this opt-in and adjusting these
        values is the intended path to a clean image. Kept additive and isolated
        so the verified SSD1680 fallback is untouched when the opt-in is off.
        """
        self.send_command(0x0C)  # booster soft start control
        self.send_data(0xD7)
        self.send_data(0xD6)
        self.send_data(0x9D)

        self.send_command(0x2C)  # write VCOM register
        self.send_data(0xA8)

        self.send_command(0x3A)  # set dummy line period
        self.send_data(0x1A)

        self.send_command(0x3B)  # set gate line width
        self.send_data(0x08)

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

    def DisplayPartial(self, image):
        """Partial refresh against the baseline set by the last display().

        Re-arms partial mode (soft reset pulse + partial LUT + border), writes
        the new frame to 0x24, and triggers a partial activation. The baseline in
        0x26 is left intact, so the scheduler should perform a periodic full
        refresh (display()) to re-seat the baseline and avoid cumulative ghosting.

        For a use_otp profile there is no register-loaded partial LUT to run, so
        a partial activation would have no waveform; route through the
        full-refresh path instead (correctness over speed -- every update still
        renders with the OTP waveform).
        """
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

        self.SetWindow(0, 0, self.width - 1, self.height - 1)
        self.SetCursor(0, 0)

        self.send_command(0x24)  # write RAM (current)
        self.send_data2(image)
        self.TurnOnDisplayPart()
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
