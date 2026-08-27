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
from .epd2in9d import EPDTimeoutError, RefreshInterrupted, mask_bw_with_red
from .waveform_profiles import (
    CONTROLLER_SSD16XX,
    DRIVER_DKE_SSD1680,
    DRIVER_IL3820,
    WaveformProfile,
    get_profile,
)

log = logging.getLogger(__name__)

# Display resolution (identical to the V2 panel: 128 x 296).
EPD_WIDTH = 128
EPD_HEIGHT = 296

# --- Three-color (red/white/black) mode ------------------------------------
# Some 2.9" SSD1680 panels are tri-color BWR. The RAM-to-LUT mapping the panel
# applies depends on the ACTIVATION waveform, not on init (init is identical in
# mono and three-color):
#   - Full color activation (0x22 = 0xF7, OTP color waveform) selects Table 6-4:
#     any bit set in 0x26 develops RED. display_color uses this, so 0x26 is the
#     RED plane there. Writing a B/W frame to 0x26 under this waveform is the
#     bleed that paints the board red.
#   - B/W partial activation (0x22 = 0x0F, register partial LUT) selects Table
#     6-5: the red-RAM bit is IGNORED and 0x26 is the differential OLD (B/W)
#     baseline. So a B/W partial re-seeds 0x26 with the previous B/W frame (as
#     the mono path always has) WITHOUT developing red, and unchanged pixels --
#     including masked-white pixels sitting over developed red -- get the LUT
#     hold phase, leaving the bistable red undisturbed.
#
# Red channel polarity. getbuffer_red packs a red pixel as a CLEARED bit (0) with
# a 0xFF (no-red) baseline, matching the black=0 convention of the B/W plane. The
# RED RAM (0x26) on this panel uses the OPPOSITE convention: bit 1 = red, bit 0 =
# no red. Waveshare's official driver for this exact panel (SKU 13276,
# epd2in9b_V4.py) makes this explicit -- before writing the red plane to 0x26 it
# inverts every byte (``ryimage[i] = ~ryimage[i]``), and Clear() writes 0x00 to
# 0x26 for "no red". So the packed red buffer MUST be inverted before it reaches
# the panel: a no-red mask byte (0xFF) -> 0x00 (no red), a red bit (0) -> 1 (red).
#
# This is why a blank red plane previously filled the whole screen red: with the
# buffer sent un-inverted, 0xFF (all bits set) reached 0x26 = every pixel red.
SSD1680_BWR_RED_INVERTED = True

# 0x22 (display update control 2) payload for a tri-color FULL refresh. Matches
# Waveshare epd2in9b_V4 TurnOnDisplay() for this panel: 0xF7 loads temperature and
# runs the OTP color waveform that drives both the B/W (0x24) and RED (0x26)
# electrodes. The mono profile bytes (0xC7 fast, 0xCF/full register LUT) are B/W
# waveforms and do not develop the red plane correctly.
SSD1680_BWR_COLOR_ACTIVATION = 0xF7

# Deadline for a post-activation BUSY wait (after 0x20 master activation). The
# panel's spec full-refresh time is 14s (10s fast), so the short init-handshake
# timeout (BUSY_TIMEOUT_SECONDS, 5s) must NOT gate a real refresh -- doing so
# aborts a healthy full refresh mid-cycle and blanks the panel (observed: Clear()'s
# 0xF7 activation timed out at 5s, so the content draw after it never ran). 30s
# covers a cold/low-temperature full refresh with margin while still bounding a
# genuinely hung panel.
REFRESH_TIMEOUT_SECONDS = 30.0


class EPD:
    """SSD1680 panel driver exposing the framework's EPD interface."""

    # Controller family (waveform_profiles.CONTROLLER_*). The live profile-apply
    # path (main._process_pending_display_profile) reads this to resolve the
    # stored key against the RIGHT family. Without it the lookup fell back to the
    # UC8151D default, so get_profile() treated every SSD16xx key as a
    # cross-controller miss and returned the UC8151D default (empty full_lut) --
    # which then crashed _init_ssd1680()'s SetLut() with "tuple index out of
    # range" on the next re-init, killing the panel until reboot.
    CONTROLLER = CONTROLLER_SSD16XX

    def __init__(self, profile: WaveformProfile = None, high_contrast: bool = False,
                 three_color: bool = False):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        # Three-color (red/white/black) mode switch. Off by default so a mono V1
        # panel is byte-for-byte unchanged. When on, full refreshes go through
        # display_color (B/W -> 0x24, red -> 0x26, 0xF7 color waveform) and fast
        # B/W updates go through _display_bw_fast, which runs the same differential
        # B/W partial as the mono path (0x0F waveform) with red pixels masked
        # white so developed red is held, not faded.
        self.three_color = three_color
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
        # Last RED frame sent to the panel (three-color mode), in getbuffer_red
        # mask space (red = 0 bit, no-red = 0xFF byte). Re-sent to 0x26 so a fast
        # B/W refresh leaves the red layer defined. Polarity to the panel is
        # applied at send time via _red_for_panel.
        self.red_buffer = self._red_blank()
        # True when the most recent init() failed specifically on a BUSY timeout.
        # main reads this to populate the cross-process display-status record.
        self.busy_timeout_occurred = False
        # True after a successful init() that observed BUSY leave idle. Later
        # inits on this instance skip the activity check (see UC8151D).
        self._busy_activity_confirmed = False
        self.init_error = None

    def _red_blank(self) -> list:
        """The 'no red' red-channel buffer in getbuffer_red mask space."""
        return [0xFF] * int(self.width * self.height / 8)

    def _red_for_panel(self, red_buf) -> list:
        """Apply the configured red polarity to a packed red buffer."""
        if not SSD1680_BWR_RED_INVERTED:
            return list(red_buf)
        return [(~b) & 0xFF for b in red_buf]

    def apply_three_color(self, enabled: bool) -> None:
        """Enable/disable three-color mode live (no-reboot toggle path).

        Mirrors apply_profile: the caller sets the mode here, then re-runs init()
        (which selects the tri-color OTP waveform) and forces a full refresh so
        the panel adopts the change without restarting the board.
        """
        self.three_color = enabled

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

    def ReadBusy(self, timeout_seconds=None, should_abort=None, require_activity=False):
        """Poll BUSY until idle, bounded by a timeout.

        SSD1680 BUSY polarity is the INVERSE of the UC8151D V2 driver: the line
        reads HIGH (1) while busy and LOW (0) when idle. Without a deadline an
        unresponsive/absent panel wedges the display thread, so the wait is
        bounded and raises EPDTimeoutError on expiry (init() turns that into -1).

        Two timeout regimes (the default, ``BUSY_TIMEOUT_SECONDS``, is the SHORT
        one used for init/SWRESET handshakes where BUSY clears almost instantly --
        a long wait there would just delay detecting a wrong/absent panel):
          - init/handshake waits: pass nothing -> short BUSY_TIMEOUT_SECONDS.
          - post-activation refresh waits: pass REFRESH_TIMEOUT_SECONDS. A real
            full refresh on this panel takes ~14s (spec), so the 5s handshake
            timeout would abort a perfectly healthy refresh mid-cycle -- which is
            exactly what blanked the panel (Clear()'s 0xF7 activation timed out at
            5s, so the content draw after it never ran).

        ``require_activity`` is the empty-connector probe: SWRESET (0x12) makes a
        fitted SSD1680 drive BUSY high, then low. A disconnected pin with
        pull-down sits LOW (already idle) and never moves, so "already idle"
        is not success. Tight-polls for BUSY_ACTIVITY_WINDOW_SECONDS looking
        for the busy edge. Refresh waits leave this False.

        Args:
            timeout_seconds: deadline override; defaults to the short
                BUSY_TIMEOUT_SECONDS read from the V2 module at call time (single
                source of truth, patchable in tests).
            should_abort: optional zero-arg predicate polled each tick. When it
                returns True (newer frame queued, or shutdown) the wait raises
                RefreshInterrupted so the caller can abort the in-flight refresh
                and restart with the new data. Distinct from EPDTimeoutError,
                which means the panel is unresponsive.
            require_activity: if True, BUSY must be observed busy before idle
                is accepted. Used only by the first init() on an instance.

        Raises:
            EPDTimeoutError: if BUSY does not reach idle within the timeout,
                or (when require_activity) never left idle.
            RefreshInterrupted: if should_abort() returns True during the wait.
        """
        timeout = timeout_seconds if timeout_seconds is not None else epd2in9d.BUSY_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        activity_deadline = time.monotonic() + epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS
        saw_busy = False
        while True:
            is_busy = epdconfig.digital_read(self.busy_pin) == 1  # HIGH: busy
            if is_busy:
                saw_busy = True
            elif saw_busy or not require_activity:
                return
            elif time.monotonic() >= activity_deadline:
                raise EPDTimeoutError(
                    "SSD1680 BUSY stayed idle for "
                    f"{epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS}s after SWRESET; "
                    "no panel on the connector"
                )
            if should_abort is not None and should_abort():
                raise RefreshInterrupted(
                    "SSD1680 BUSY wait aborted: newer frame pending")
            if saw_busy and time.monotonic() >= deadline:
                raise EPDTimeoutError(
                    f"SSD1680 BUSY not released within {timeout}s"
                )
            if saw_busy:
                epdconfig.delay_ms(10)

    def _full_activation_byte(self) -> int:
        """Return the 0x22 (display update control 2) payload for a full refresh.

        The payload selects the waveform source, and it differs per driver:
          - SSD1680 register LUT: 0xC7 (run the LUT written by SetLut()).
          - SSD1680 OTP / DEPG0290BS: 0xF7 (load temperature + the OTP waveform).
          - IL3820: 0xC4 (per the IL3820 reference update sequence).

        This returns the MONO full-refresh byte per profile. The tri-color full
        refresh does NOT use it: display_color activates with
        SSD1680_BWR_COLOR_ACTIVATION (0xF7), matching Waveshare's epd2in9b_V4
        TurnOnDisplay() for this panel, because only that OTP color waveform drives
        the red electrode correctly.
        """
        if self.profile.driver == DRIVER_IL3820:
            return 0xC4
        if self.profile.driver == DRIVER_DKE_SSD1680 or self.profile.use_otp:
            return 0xF7
        return 0xC7

    def TurnOnDisplay(self, should_abort=None):
        """Trigger a full refresh (load LUT/temp + activate) for this driver."""
        self.send_command(0x22)  # display update control 2
        self.send_data(self._full_activation_byte())
        self.send_command(0x20)  # master activation
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS, should_abort=should_abort)

    def TurnOnDisplayPart(self):
        """Trigger a partial refresh (activation only, partial LUT preloaded)."""
        self.send_command(0x22)  # display update control 2
        self.send_data(0x0F)
        self.send_command(0x20)  # master activation
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS)

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
        self.init_error = None
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
            # panel, or BUSY never left idle (no panel). Report -1 so
            # Manager.initialize() disables the display rather than hanging;
            # main records the timeout for the status card.
            self.busy_timeout_occurred = True
            self.init_error = str(e)
            log.error(f"[EPD SSD1680] init aborted: {e}")
            return -1
        self._busy_activity_confirmed = True
        log.info(
            "[EPD SSD1680] init ok: profile=%s driver=%s use_otp=%s "
            "full_activation=0x%02X high_contrast=%s three_color=%s",
            self.profile.key, self.profile.driver, self.profile.use_otp,
            self._full_activation_byte(), self.high_contrast, self.three_color,
        )
        return 0

    def _init_ssd1680(self):
        """Waveshare SSD1680 init (epd2in9_V2 / epd2in9b_V4 sequence).

        Loads the profile's 159-byte full LUT via SetLut() unless ``use_otp`` is
        set (then the panel's OTP waveform drives the full refresh at activation,
        TurnOnDisplay()'s 0xF7).

        Sets the border waveform (0x3C=0x05) and selects the INTERNAL temperature
        sensor (0x18=0x80), exactly as Waveshare's official drivers do. The temp
        sensor is REQUIRED for the OTP activation (0xF7 = "load temperature + run
        OTP waveform"): without it the OTP/tri-color full refresh does not develop
        correctly. These two registers were previously omitted, which is why the
        tri-color (0xF7) refresh blanked the panel.
        """
        self.ReadBusy()
        self.send_command(0x12)  # SWRESET
        self.ReadBusy(require_activity=not self._busy_activity_confirmed)

        self.send_command(0x01)  # driver output control
        self.send_data(0x27)
        self.send_data(0x01)
        self.send_data(0x00)

        self.send_command(0x11)  # data entry mode
        self.send_data(0x03)

        self.SetWindow(0, 0, self.width - 1, self.height - 1)

        self.send_command(0x3C)  # border waveform
        self.send_data(0x05)

        self.send_command(0x21)  # display update control 1
        self.send_data(0x00)
        self.send_data(0x80)

        self.send_command(0x18)  # temperature sensor: internal (required by 0xF7)
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
        self.ReadBusy(require_activity=not self._busy_activity_confirmed)

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
        self.ReadBusy(require_activity=not self._busy_activity_confirmed)

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

    def getbuffer_red(self, image):
        """Pack the red-plane PIL image into the 1bpp red buffer (red bit = 0).

        Uses the same packer as the B/W plane: a drawn (black) pixel in the red
        sprite becomes a cleared bit, i.e. red. Panel polarity is applied later by
        _red_for_panel at send time, so this buffer is stored in a fixed
        mask-space convention (0 = red, 0xFF byte = no red).
        """
        return epd2in9d.pack_image_to_buffer(image, self.width, self.height)

    def display_color(self, bw_buf, red_buf, should_abort=None):
        """Full tri-color refresh: B/W -> 0x24, red -> 0x26, profile's full waveform.

        Matches Waveshare's official epd2in9b_V4 driver for this panel (SKU 13276):
          - B/W plane -> 0x24 (1=white, 0=black).
          - red plane -> 0x26, inverted to panel polarity by _red_for_panel
            (Waveshare's ``ryimage[i] = ~ryimage[i]``); on 0x26, bit 1 = red.
          - activation 0xF7 (SSD1680_BWR_COLOR_ACTIVATION), the OTP color waveform.
        B/W pixels are forced white wherever red is set (mask_bw_with_red) so a
        pixel is never driven both black and red (which renders muddy). Both planes
        are stored so a subsequent fast B/W partial can leave 0x26 defined.
        """
        bw_masked = mask_bw_with_red(bw_buf, red_buf)
        red_set = sum(1 for b in red_buf if b != 0xFF)
        log.info(
            "[EPD SSD1680] THREE-COLOR full refresh: profile=%s activation=0x%02X "
            "red_polarity_inverted=%s red_bytes_with_red=%d",
            self.profile.key, SSD1680_BWR_COLOR_ACTIVATION,
            SSD1680_BWR_RED_INVERTED, red_set,
        )
        self.send_command(0x24)  # B/W RAM
        self.send_data2(bw_masked)
        self.send_command(0x26)  # RED RAM
        self.send_data2(self._red_for_panel(red_buf))
        # Drive the tri-color full waveform. Waveshare's epd2in9b_V4 TurnOnDisplay
        # (the color full refresh for this panel) activates with 0xF7 (load
        # temperature + OTP color waveform), NOT the profile's mono byte (0xC7 is
        # that driver's FAST B/W path). The red electrode only develops correctly
        # under this OTP color waveform, so it is used regardless of profile.
        self.send_command(0x22)  # display update control 2
        self.send_data(SSD1680_BWR_COLOR_ACTIVATION)
        self.send_command(0x20)  # master activation
        # On abort this raises RefreshInterrupted BEFORE the buffers are stored,
        # so red_buffer keeps the last fully-rendered red rather than this
        # never-developed frame; the scheduler also forgets _last_red_buffer.
        self.ReadBusy(REFRESH_TIMEOUT_SECONDS, should_abort=should_abort)
        self.buffer = list(bw_masked)
        self.red_buffer = list(red_buf)

    def _display_bw_fast(self, image):
        """Fast B/W update in three-color mode that does not fade the red plane.

        The hybrid scheduler only calls this when no NEW red is on screen; any red
        already developed by the last display_color must survive untouched. Runs
        the SAME differential B/W partial as the mono path (see
        _display_partial_register_lut): the previous shown frame -> 0x26, the new
        frame -> 0x24, B/W partial waveform (0x22 = 0x0F). Under the B/W waveform
        the panel uses Table 6-5 mapping -- the red-RAM bit is ignored and 0x26 is
        the differential OLD baseline, NOT the red plane -- so re-seeding 0x26 with
        the previous B/W frame does not develop red. A pixel showing red is masked
        white (mask_bw_with_red) and is unchanged between the previous and new
        frames, so it gets the LUT's hold phase and its bistable red particles are
        left undisturbed. Leaving the red mask in 0x26 (the previous behaviour)
        gave unchanged pixels no clean hold, so every partial pulsed them and the
        red faded tick by tick; the red is re-driven only by a full display_color.

        For a use_otp profile there is no register partial LUT, so fall back to a
        full tri-color refresh. The fallback must re-send the CURRENT red plane
        (self.red_buffer), NOT a blank: this path is the "red unchanged" case, so
        blanking 0x26 would erase on-screen red the scheduler still believes is
        present (it leaves _last_red_buffer unchanged), desyncing panel and
        bookkeeping. Re-sending the held red keeps the screen correct -- only the
        speed is lost on use_otp profiles, which have no partial waveform anyway.
        """
        if self.profile.use_otp or not self.profile.partial_lut:
            log.info("[EPD SSD1680] three-color fast path: no register partial LUT "
                     "(use_otp=%s) -> full display_color fallback (red preserved)",
                     self.profile.use_otp)
            self.display_color(image, self.red_buffer)
            return

        log.debug("[EPD SSD1680] three-color fast B/W differential partial "
                  "(prev->0x26, new->0x24; white->white hold, red undisturbed)")
        # Force the B/W plane white wherever red ink persists so the partial never
        # drives a red pixel black. self.buffer (the previous shown frame) is
        # already masked, so a still-red pixel is white in both old and new frames
        # -> the white->white transition (LUT3).
        masked = mask_bw_with_red(image, self.red_buffer)
        self._display_partial_register_lut(masked, self._red_safe_partial_lut())

    def _red_safe_partial_lut(self):
        """The profile partial LUT with the white->white touch-up removed.

        A masked red pixel is white in both the old (0x26) and new (0x24) frames,
        so it always takes the white->white transition (SSD1680 LUT3, bytes
        [36:48] of the 153-byte waveform). WF_PARTIAL_2IN9 leaves a 1-frame VSL
        (drive-white) touch-up there (byte 37 = 0x80). VSL pulls white particles
        to the surface and pushes the bistable red back a little EVERY partial --
        the observed per-tick red fade. Zeroing LUT3 makes white->white a true 0V
        hold, so a still-red pixel is never pulsed; only pixels that actually
        change (0<->1, e.g. clock digits) are driven, via the untouched LUT1/LUT2.
        Black->black (LUT0) keeps its touch-up: red pixels are masked white, never
        black, so LUT0 never covers red, and the touch-up keeps black crisp.

        Returns the profile LUT unchanged if it is too short to carry LUT3 (guards
        non-SSD1680 formats), so the caller never mangles an unexpected waveform.
        """
        lut = list(self.profile.partial_lut)
        if len(lut) < 48:
            return self.profile.partial_lut
        for i in range(36, 48):  # LUT3 = white->white
            lut[i] = 0x00
        return tuple(lut)

    def _display_partial_register_lut(self, image, partial_lut=None):
        """Register-LUT differential partial: arm partial mode, diff prev->new, run.

        Shared by the mono partial and the three-color fast B/W partial. Re-arms
        partial mode (soft-reset pulse + partial LUT + border), loads the
        differential frame (previous shown -> 0x26, new -> 0x24; see
        _write_partial_rams for why re-seeding 0x26 every call is mandatory), then
        runs the B/W partial activation (0x22 = 0x0F via TurnOnDisplayPart).

        On a tri-color panel this same waveform is what lets a B/W update leave the
        red plane alone: the B/W partial waveform selects Table 6-5 mapping, under
        which the red-RAM bit is ignored (0x26 is read as the differential B/W
        baseline, not as red). Do NOT re-route this through the 0xF7 OTP color
        activation -- that selects Table 6-4, where any 0x26 bit develops red and
        the previous B/W frame would bleed red across the board.

        ``partial_lut`` overrides the profile's partial waveform (the three-color
        path passes a white->white-hold variant; see _red_safe_partial_lut). The
        mono path passes None and uses the profile LUT unchanged.
        """
        if partial_lut is None:
            partial_lut = self.profile.partial_lut
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(2)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(2)

        self.SetLut(partial_lut)
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

    def display(self, image, should_abort=None):
        """Full refresh, and set the partial-refresh baseline.

        Writes the image to both the current (0x24) and "old" (0x26) RAM so a
        subsequent DisplayPartial() diffs against this frame. Mirrors Waveshare's
        ``display_Base``; the framework only calls this on full-refresh cycles.

        should_abort is forwarded to the refresh wait so a newer queued frame can
        interrupt this full refresh (the scheduler then restarts with new data).
        """
        log.info("[EPD SSD1680] mono FULL refresh: profile=%s activation=0x%02X",
                 self.profile.key, self._full_activation_byte())
        self.send_command(0x24)  # write RAM (current)
        self.send_data2(image)
        self.send_command(0x26)  # write RAM (baseline for partial diff)
        self.send_data2(image)
        self.TurnOnDisplay(should_abort=should_abort)
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

        In three-color mode the scheduler routes here only for frames whose red
        plane is unchanged; _display_bw_fast runs this same differential B/W
        partial with red pixels masked white, so their bistable red is held by the
        LUT hold phase (full tri-color refreshes go through display_color).
        """
        if self.three_color:
            self._display_bw_fast(image)
            return
        if self.profile.driver == DRIVER_IL3820:
            self._display_partial_il3820(image)
            return
        if self.profile.driver == DRIVER_DKE_SSD1680:
            self._display_partial_dke(image)
            return
        if self.profile.use_otp:
            log.info("[EPD SSD1680] partial requested but profile use_otp=True "
                     "(no register partial LUT) -> routing to mono FULL refresh")
            self.display(image)
            return
        log.debug("[EPD SSD1680] mono partial refresh (profile=%s)", self.profile.key)
        self._display_partial_register_lut(image)

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
        """Clear the panel to white (full refresh), reseating both RAM banks.

        In three-color mode 0x26 is the RED RAM, so it is cleared to the no-red
        blank (panel polarity) rather than the B/W blank, and a single tri-color
        OTP activation clears both planes -- writing the B/W blank to 0x26 here
        would paint the whole panel red.
        """
        activation = SSD1680_BWR_COLOR_ACTIVATION if self.three_color else self._full_activation_byte()
        log.info("[EPD SSD1680] Clear: three_color=%s profile=%s activation=0x%02X",
                 self.three_color, self.profile.key, activation)
        linewidth = int(self.width / 8) if self.width % 8 == 0 else int(self.width / 8) + 1
        blank = [color] * int(self.height * linewidth)
        if self.three_color:
            self.send_command(0x24)  # B/W RAM -> white
            self.send_data2(blank)
            self.send_command(0x26)  # RED RAM -> no red
            self.send_data2(self._red_for_panel(self._red_blank()))
            # OTP color waveform (0xF7), as Waveshare's epd2in9b_V4 Clear() uses.
            self.send_command(0x22)  # display update control 2
            self.send_data(SSD1680_BWR_COLOR_ACTIVATION)
            self.send_command(0x20)  # master activation
            self.ReadBusy(REFRESH_TIMEOUT_SECONDS)
            self.buffer = [0xFF] * int(self.width * self.height / 8)
            self.red_buffer = self._red_blank()
            return
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
