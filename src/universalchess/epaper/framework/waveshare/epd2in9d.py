#!/usr/bin/python
# -*- coding:utf-8 -*-

# *****************************************************************************
# * | File        :   epd2in9d.py
# * | Author      :   Waveshare team
# * | Function    :   Electronic paper driver
# * | Info        :
# *----------------
# * | This version:   V2.1
# * | Date        :   2022-08-10
# # | Info        :   python demo
# -----------------------------------------------------------------------------
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import logging
import time
from . import epdconfig
from .waveform_profiles import (
    CONTROLLER_UC8151D,
    WaveformProfile,
    get_profile,
)
from PIL import Image
import numpy as np

log = logging.getLogger(__name__)

# Experimental "high contrast" override for UC8151D: add this to the profile's
# VCOM_DC byte (register 0x82), clamped to the 6-bit field max. A more-negative
# VCOM_DC darkens black and lifts a faint partial image. Not datasheet-backed --
# the one tuning knob without provenance, surfaced as experimental in the UI and
# mirrors the SSD1680 driver's high_contrast voltage push.
UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA = 0x08
UC8151D_VCOM_DC_MAX = 0x3F

# --- Three-color (red/white/black) mode ------------------------------------
# The same UC8151D controller drives the tri-color BWR panels (GDEH029Z13 /
# GDEW029Z13). On a tri-color panel command 0x10 is the BLACK/WHITE data channel
# and 0x13 is the RED data channel (the mono partial path instead uses them as
# OLD/NEW B/W RAM, which is why the mono driver bleeds black into red here).
#
# Panel-setting byte (register 0x00) selecting the BWR OTP waveform. The init
# comment enumerates the options: "KW-BF 1f, KWR-AF, BWROTP 0f, BWOTP 1f"; 0x0f
# is the BWR-from-OTP setting. Surfaced as a named constant because it (and the
# red polarity below) are the bytes most likely to be tuned during on-hardware
# bring-up on the actual panel.
UC8151D_BWR_PANEL_SETTING = 0x0F

# Red channel polarity. getbuffer_red packs red pixels as a cleared bit (0),
# matching the black=0 convention of the B/W plane. If the physical panel treats
# a SET bit as red instead, flip this during bring-up and the driver inverts the
# red buffer (and the no-red blank) in one place.
UC8151D_BWR_RED_INVERTED = False

# Maximum time to wait for the panel BUSY line to signal idle before giving up.
# A legitimate full-refresh waveform holds BUSY low for well under a second, so
# this ceiling is only reached when the panel never releases BUSY -- e.g. a DGT
# Centaur V1 panel whose BUSY polarity is inverted, or no panel attached. Left
# unbounded, ReadBusy() spins forever and wedges the display thread during
# startup (the "startup LED circles never stop" symptom). On timeout ReadBusy()
# raises EPDTimeoutError, which init() converts into a -1 result so the caller
# disables the display instead of hanging.
BUSY_TIMEOUT_SECONDS = 5.0


class EPDTimeoutError(RuntimeError):
    """Raised when the panel BUSY line never reaches idle within the timeout."""


class RefreshInterrupted(RuntimeError):
    """Raised by a full-refresh BUSY wait when newer frame data is pending.

    Distinct from EPDTimeoutError so the scheduler can tell a deliberate abort
    ("newer data arrived, restart with it") from a genuinely unresponsive panel.
    The driver does not recover the panel itself; the scheduler re-inits before
    the next refresh, which resets the panel and halts the aborted waveform.
    """


# Display resolution
EPD_WIDTH       = 128
EPD_HEIGHT      = 296

# Debug flag for buffer diagnostics in DisplayPartial
# Set to True to print buffer statistics on each partial refresh
DEBUG_DISPLAY_PARTIAL = False


def mask_bw_with_red(bw_buf, red_buf):
    """Force B/W pixels white wherever red is set (shared by both BWR drivers).

    A tri-color pixel must be driven red OR black, never both, or the red renders
    muddy/dark. getbuffer_red marks red as a cleared bit (0), so ``bw | ~red``
    sets those positions white (1) in the B/W buffer while leaving every non-red
    position untouched. Returns a plain list (the buffers are stored/re-sent as
    lists by both drivers).
    """
    bw = np.frombuffer(bytes(bw_buf), dtype=np.uint8)
    red = np.frombuffer(bytes(red_buf), dtype=np.uint8)
    return (bw | (~red & 0xFF)).astype(np.uint8).tolist()


def pack_image_to_buffer(image, width, height):
    """Pack a PIL image into the panel's 1bpp byte buffer (white=1, black=0).

    Vectorized replacement for the historical per-pixel nested loop. On the
    single-core Pi Zero W that loop ran ~38k interpreted iterations on every
    refresh (pure display latency); ``np.packbits`` does the same packing as a
    single C call. The output is byte-identical to the old loop for both panel
    orientations, so the rendered image is unchanged regardless of which driver
    is active.

    Layout: ``(width // 8) * height`` bytes, MSB-first within each byte. White
    pixels leave the bit set (buffer baseline is all-0xFF); black pixels clear
    it. Two orientations are accepted:
      - upright  (image size == ``width`` x ``height``): row-major pack.
      - rotated  (image size == ``height`` x ``width``): the panel is mounted
        180-rotated and the framebuffer hands over a transposed frame, so each
        source pixel ``(x, y)`` maps to ``(newx=y, newy=height-1-x)`` before
        packing -- expressed here as transpose + vertical flip.

    An image matching neither orientation returns an all-0xFF (white) buffer,
    matching the original loop, which left the buffer untouched in that case
    rather than guessing a layout.

    Args:
        image: a PIL image; any mode is accepted and thresholded via
            ``convert('1')`` exactly as the original implementation did.
        width: panel width in pixels (must be a multiple of 8).
        height: panel height in pixels.

    Returns:
        A list of ``(width // 8) * height`` ints (0-255). A list (not an
        ndarray) is returned because callers store it as the partial-refresh
        baseline and re-send it to the controller.
    """
    # mode '1' -> uint8 array shape (rows, cols) with white=1, black=0.
    mono = np.array(image.convert('1'), dtype=np.uint8)
    imheight, imwidth = mono.shape
    if imwidth == width and imheight == height:
        return np.packbits(mono, axis=1).reshape(-1).tolist()
    if imwidth == height and imheight == width:
        # (x, y) -> (newx=y, newy=height-1-x): transpose, then flip rows so row
        # newy = height-1-x. Column index becomes newx=y, matching the loop's
        # 0x80>>(y%8) bit selection.
        rotated = mono.T[::-1, :]
        return np.packbits(rotated, axis=1).reshape(-1).tolist()
    return [0xFF] * ((width // 8) * height)


class EPD:
    # Controller family (waveform_profiles.CONTROLLER_*). Lets ``main`` resolve
    # the correct profile for whichever driver actually drove the panel.
    CONTROLLER = CONTROLLER_UC8151D

    def __init__(self, profile: WaveformProfile = None, high_contrast: bool = False,
                 three_color: bool = False):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        # Three-color (red/white/black) mode switch. Off by default so a mono V2
        # panel is byte-for-byte unchanged. When on, init() selects the BWR OTP
        # waveform and the refresh paths route B/W -> 0x10 and red -> 0x13 (see
        # display_color / DisplayPartial).
        self.three_color = three_color
        # Selected waveform profile. The full refresh is OTP for every UC8151D
        # profile; the profile only chooses the partial-refresh register LUTs and
        # analog bytes (see SetPartReg). None selects the verified Waveshare
        # default so a working V2 panel is byte-for-byte unchanged. Variants exist
        # for replacement panels (GDEW029I6FD/T5D/M06) that ghost or render faint
        # on the stock partial waveform.
        self.profile = profile if profile is not None else get_profile("", CONTROLLER_UC8151D)
        # Experimental drive-voltage override (VCOM_DC bump). Off by default; the
        # one knob without datasheet backing, surfaced separately in the UI.
        self.high_contrast = high_contrast
        # Set True by init() when it returns -1 specifically because the BUSY
        # line never reached idle within the timeout (the inverted-polarity V1
        # panel signature). Lets the startup selector distinguish that case --
        # which warrants the SSD1680 fallback -- from other init failures.
        self.busy_timeout_occurred = False
        # Store the last image sent for partial refresh
        self.buffer = [0xFF] * int(self.width * self.height / 8)
        # Last RED frame sent to the panel (three-color mode). All-0xFF = no red,
        # matching getbuffer_red's polarity. Re-sent to the red channel so a fast
        # B/W refresh leaves the red layer in a defined (cleared) state.
        self.red_buffer = self._red_blank()

    def _red_blank(self) -> list:
        """The 'no red' red-channel buffer for the current polarity."""
        fill = 0x00 if UC8151D_BWR_RED_INVERTED else 0xFF
        return [fill] * int(self.width * self.height / 8)

    def apply_three_color(self, enabled: bool) -> None:
        """Enable/disable three-color mode live (no-reboot toggle path).

        Mirrors apply_profile: the caller sets the new mode here, then re-runs
        init() (which selects the matching panel-setting waveform) and forces a
        full refresh so the panel adopts the change without restarting the board.
        """
        self.three_color = enabled

    def apply_profile(self, profile: WaveformProfile, high_contrast: bool) -> None:
        """Select a new waveform profile/override for the next refresh.

        Backs the live (no-reboot) profile change: the caller sets the new
        selection here, then re-runs init() followed by a full refresh so the
        panel adopts the new partial LUTs and voltages without restarting the
        board process. None selects the verified default, matching the
        constructor. The full refresh is OTP regardless, so this only changes how
        subsequent partial refreshes drive the panel.
        """
        self.profile = profile if profile is not None else get_profile("", CONTROLLER_UC8151D)
        self.high_contrast = high_contrast

    def _effective_vcom_dc(self) -> int:
        """VCOM_DC byte (0x82) for the active profile, with high_contrast applied.

        Returns the profile's nominal VCOM_DC, or -- when high_contrast is on --
        that value pushed harder by UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA, clamped
        to the register's 6-bit range so the experimental boost can never write
        an out-of-range value.
        """
        base = self.profile.uc8151d.vcom_dc
        if not self.high_contrast:
            return base
        return min(base + UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA, UC8151D_VCOM_DC_MAX)
        
    # Hardware reset
    def reset(self):
        print("Resetting display")
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20) 
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(5)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20)  
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(5)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20)  
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(5)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20)  

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

    # send a lot of data   
    def send_data2(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte2(data)
        epdconfig.digital_write(self.cs_pin, 1)
        
    def ReadBusy(self, should_abort=None):
        """Poll the panel BUSY line until idle, bounded by BUSY_TIMEOUT_SECONDS.

        Waits while the pin reads LOW (busy) and returns once it reads HIGH
        (idle). An unresponsive or incompatible panel -- notably a V1 panel with
        inverted BUSY polarity, or no panel at all -- never drives the expected
        idle level, so without a deadline this loop never returns and the
        display thread hangs. The bounded wait converts that hang into an
        EPDTimeoutError; init() catches it and returns -1.

        Args:
            should_abort: optional zero-arg predicate polled each tick. When it
                returns True (newer frame queued, or shutdown), the wait raises
                RefreshInterrupted so the caller can abort and restart with the
                new data. Distinct from EPDTimeoutError (a dead panel).

        Raises:
            EPDTimeoutError: if BUSY does not reach idle within the timeout.
            RefreshInterrupted: if should_abort() returns True during the wait.
        """
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while(epdconfig.digital_read(self.busy_pin) == 0):  # LOW: busy, HIGH: idle
            if should_abort is not None and should_abort():
                raise RefreshInterrupted("BUSY wait aborted: newer frame pending")
            self.send_command(0x71)
            epdconfig.delay_ms(10)
            if time.monotonic() >= deadline:
                raise EPDTimeoutError(
                    f"BUSY not released within {BUSY_TIMEOUT_SECONDS}s; panel "
                    "unresponsive or incompatible (e.g. inverted BUSY polarity)"
                )
        
    def TurnOnDisplay(self, should_abort=None):
        self.send_command(0x12)
        epdconfig.delay_ms(10)
        self.ReadBusy(should_abort=should_abort)
        # Park the panel after the refresh settles: power off the DC-DC booster
        # and source/gate drivers (0x02). The e-ink image is bistable and holds
        # without active bias, and an unpowered panel is not disturbed by bright
        # or IR-rich light (phone flashlight, sunlight) which otherwise photo-
        # induces leakage in the panel TFTs and darkens the image. Every refresh
        # path re-powers the panel (0x04) before driving, so this is reversible
        # without a hardware reset (unlike deep sleep 0x07/0xA5).
        self.send_command(0x02)
        self.ReadBusy()
        
    def init(self):
        self.busy_timeout_occurred = False
        if (epdconfig.module_init() != 0):
            return -1
        # EPD hardware init start
        self.reset()
        
        self.send_command(0x04)
        try:
            self.ReadBusy() #waiting for the electronic paper IC to release the idle signal
        except EPDTimeoutError as e:
            # Panel never signaled idle: unresponsive or incompatible hardware
            # (e.g. a V1 panel). Report failure via the documented -1 result so
            # Manager.initialize() disables the display rather than hanging the
            # board at startup. This is the one-time startup detection; runtime
            # refreshes re-call init() and are themselves bounded by the same
            # timeout, so a display that fails later keeps being retried.
            self.busy_timeout_occurred = True
            log.error(f"[EPD] init aborted: {e}")
            return -1

        self.send_command(0x00)     #panel setting
        if self.three_color:
            # BWR-from-OTP waveform: drives the black/white (0x10) and red (0x13)
            # channels from the panel's on-chip tri-color waveform.
            self.send_data(UC8151D_BWR_PANEL_SETTING)
        else:
            self.send_data(0x1f)    # LUT from OTP，KW-BF   KWR-AF    BWROTP 0f   BWOTP 1f

        self.send_command(0x61)     #resolution setting
        self.send_data (0x80)       
        self.send_data (0x01)       
        self.send_data (0x28)       

        self.send_command(0X50)     #VCOM AND DATA INTERVAL SETTING
        self.send_data(0x97)        #WBmode  VBDF 17|D7 VBDW 97  VBDB 57   WBRmode  VBDF F7 VBDW 77  VBDB 37  VBDR B7

        return 0
    
    def SetPartReg(self):
        """Program the partial-refresh registers from the active profile.

        Same command sequence as the Waveshare reference; the LUTs (0x20-0x24)
        and analog bytes (0x30 PLL, 0x82 VCOM_DC, 0x50 interval) come from
        ``self.profile.uc8151d`` so a replacement panel variant
        (GDEW029I6FD/T5D/M06) can be driven without code changes. With the
        Waveshare default profile and high_contrast off, the bytes emitted are
        identical to the stock driver. PLL is skipped when the profile leaves it
        ``None`` (the controller default), matching GxEPD2's I6FD/T5D partial init.
        """
        wf = self.profile.uc8151d

        self.send_command(0x01)
        self.send_data(0x03)
        self.send_data(0x00)
        self.send_data(0x2b)
        self.send_data(0x2b)
        self.send_data(0x03)

        self.send_command(0x06) #boost soft start
        self.send_data(0x17)     #A
        self.send_data(0x17)     #B
        self.send_data(0x17)     #C

        self.send_command(0x04)
        self.ReadBusy()

        self.send_command(0x00) #panel setting
        self.send_data(0xbf)     #LUT from register, 128x296

        if wf.pll is not None:
            self.send_command(0x30) #PLL setting
            self.send_data(wf.pll)   # 3a 100HZ   29 150Hz 39 200HZ 31 171HZ

        self.send_command(0x61) #resolution setting
        self.send_data(self.width)
        self.send_data((self.height >> 8) & 0xff)
        self.send_data(self.height & 0xff)

        self.send_command(0x82) #vcom_DC setting
        self.send_data(self._effective_vcom_dc())

        self.send_command(0X50)     #VCOM AND DATA INTERVAL SETTING
        self.send_data(wf.interval)

        self.send_command(0x20)         #vcom
        self.send_data2(list(wf.vcom))
        self.send_command(0x21)         # ww --
        self.send_data2(list(wf.ww))
        self.send_command(0x22)         # bw r
        self.send_data2(list(wf.bw))
        self.send_command(0x23)         # wb w
        self.send_data2(list(wf.wb))
        self.send_command(0x24)         # bb b
        self.send_data2(list(wf.bb))

    def getbuffer(self, image):
        return pack_image_to_buffer(image, self.width, self.height)

    def getbuffer_red(self, image):
        """Pack a red-mask image into the panel's red-channel byte buffer.

        The red mask is a 1-bit image where 0 = red and 255 = not red, so it
        packs with the SAME polarity as the B/W plane (a red pixel clears its
        bit, exactly as a black pixel does). Delegates to the shared vectorized
        packer so red and B/W never disagree on byte layout/orientation. Polarity
        to the panel is applied at send time (display_color) via
        UC8151D_BWR_RED_INVERTED, keeping this a pure packing step.
        """
        return pack_image_to_buffer(image, self.width, self.height)

    def _red_for_panel(self, red_buf):
        """Apply the configured red polarity to a packed red buffer."""
        if not UC8151D_BWR_RED_INVERTED:
            return list(red_buf)
        return [(~b) & 0xFF for b in red_buf]

    def _bw_with_red_removed(self, bw_buf, red_buf):
        """Force B/W pixels white wherever red is set (see mask_bw_with_red)."""
        return mask_bw_with_red(bw_buf, red_buf)

    def display_color(self, bw_buf, red_buf, should_abort=None):
        """Full three-color refresh: B/W -> 0x10, red -> 0x13, then refresh.

        This is the only path that can change the red layer (the red waveform is
        OTP and runs the full ~12-15s tri-color refresh). The B/W buffer has its
        red pixels forced white first so no pixel is driven both black and red.
        Records both channels as the live baselines.

        Args:
            bw_buf: packed black/white buffer (white=1, black=0), as from getbuffer.
            red_buf: packed red buffer (red=0), as from getbuffer_red.
            should_abort: optional predicate; when it returns True during the
                final refresh wait, TurnOnDisplay raises RefreshInterrupted so the
                scheduler can restart with newer data.
        """
        # Wake the panel in case it was parked after a prior refresh.
        self.send_command(0x04)
        self.ReadBusy()
        # Re-assert the BWR OTP waveform: a preceding fast B/W refresh switches the
        # panel-setting to the register LUT, so select it again before driving red.
        self.send_command(0x00)
        self.send_data(UC8151D_BWR_PANEL_SETTING)

        bw_masked = self._bw_with_red_removed(bw_buf, red_buf)
        red_on_panel = self._red_for_panel(red_buf)

        self.send_command(0x10)
        self.send_data2(bw_masked)
        epdconfig.delay_ms(10)
        self.send_command(0x13)
        self.send_data2(red_on_panel)
        epdconfig.delay_ms(10)

        self.buffer = list(bw_masked)
        self.red_buffer = list(red_buf)
        self.TurnOnDisplay(should_abort=should_abort)

    def display(self, image, should_abort=None):
        # Wake the panel in case it was parked (powered off) after a prior
        # refresh. Harmless if init() already powered it on this cycle.
        self.send_command(0x04)
        self.ReadBusy()
        self.send_command(0x10)
        self.send_data2([0x00] * int(self.width * self.height / 8))
        epdconfig.delay_ms(10)
        self.send_command(0x13)
        self.send_data2(image)
        epdconfig.delay_ms(10)
        # Record the image just shown as the partial-refresh baseline. The next
        # DisplayPartial() re-sends this to the controller's old-RAM (0x10) to
        # compute the diff, so it must match what is physically on the panel.
        # (Previously reset to all-white, which forced a Clear() flash on the
        # following partial to reconcile the mismatch.)
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)
        self.TurnOnDisplay(should_abort=should_abort)
        
    def _dump_buffer(self, label, buf):
        """Debug helper: print buffer statistics.
        
        Prints byte counts for black (0x00), white (0xFF), and other (gray) values,
        plus first 16 bytes as hex. Useful for diagnosing partial refresh issues.
        """
        buf_bytes = bytes(buf) if not isinstance(buf, bytes) else buf
        black = sum(1 for b in buf_bytes if b == 0x00)
        white = sum(1 for b in buf_bytes if b == 0xFF)
        other = len(buf_bytes) - black - white
        sample = ' '.join(f'{b:02x}' for b in buf_bytes[:16])
        print(f"EPD [{label}] len={len(buf_bytes)} black_bytes={black} white_bytes={white} other={other}")
        print(f"EPD [{label}] first 16: {sample}")
    
    def _display_bw_fast(self, image):
        """Fast black/white-only refresh on a tri-color panel (three_color mode).

        On a BWR panel the mono OLD/NEW differential cannot be used: 0x10 is the
        B/W channel and 0x13 is the RED channel, so the mono path's "new image ->
        0x13" would paint the board red (the reported bleed). Instead this writes
        the new B/W frame to the B/W channel (0x10) and the no-red blank to the
        red channel (0x13), then runs the register B/W LUTs (SetPartReg selects
        0x00=0xbf and loads the ww/bw/wb/bb tables). The red LUT is never loaded,
        so the red layer is left muted -- the u8g2-documented "B/W mode on a BWR
        panel" technique.

        The hybrid scheduler only takes this path when NO red is on screen, so
        emitting the no-red blank to 0x13 is correct (it keeps the red layer
        clear). Changing red goes through display_color instead.

        The exact LUT/timing that makes this genuinely fast on the physical panel
        is finalized during on-hardware bring-up; the channel routing asserted
        here (B/W -> 0x10, never the B/W image -> 0x13) is the correctness
        contract that fixes the bleed.
        """
        self.SetPartReg()
        self.send_command(0x91)             # partial in
        self.send_command(0x90)             # partial window
        self.send_data(0)
        self.send_data(self.width - 1)
        self.send_data(0)
        self.send_data(0)
        self.send_data((self.height - 1) >> 8)
        self.send_data((self.height - 1) & 0xFF)
        self.send_data(0x28)

        red_blank = self._red_for_panel(self._red_blank())
        self.send_command(0x10)             # B/W channel <- new frame
        self.send_data2(image)
        epdconfig.delay_ms(10)
        self.send_command(0x13)             # RED channel <- no-red blank (never the B/W image)
        self.send_data2(red_blank)
        epdconfig.delay_ms(10)

        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)
        self.red_buffer = self._red_blank()
        self.TurnOnDisplay()

    def DisplayPartial(self, image):
        """
        Display partial refresh following Waveshare pattern.
        
        Args:
            image: Buffer containing the new/current content (sent to 0x13)
        """
        if self.three_color:
            # Tri-color panel: route the B/W frame to the B/W channel and keep
            # the red channel blank (see _display_bw_fast). Never reuse the mono
            # OLD/NEW-on-0x13 scheme, which bleeds black into red here.
            self._display_bw_fast(image)
            return

        if DEBUG_DISPLAY_PARTIAL:
            self._dump_buffer("OLD_BUFFER_0x10", self.buffer)
            self._dump_buffer("NEW_IMAGE_0x13", image)
        
        self.SetPartReg()
        self.send_command(0x91)
        self.send_command(0x90)
        self.send_data(0)
        self.send_data(self.width - 1)

        self.send_data(0)
        self.send_data(0)
        self.send_data((self.height - 1) >> 8)      # High byte of (height - 1)
        self.send_data((self.height - 1) & 0xFF)    # Low byte of (height - 1)
        self.send_data(0x28)
        
        # Send old/previous content to 0x10
        self.send_command(0x10)
        self.send_data2(self.buffer)
        epdconfig.delay_ms(10)
        
        # Send new/current content to 0x13
        self.send_command(0x13)
        self.send_data2(image)
        epdconfig.delay_ms(10)
          
        # Store image as buffer for next partial refresh
        self.buffer = image.copy() if hasattr(image, 'copy') else list(image)

        self.TurnOnDisplay()

    def Clear(self):
        print("Clearing display")
        # Wake the panel in case it was parked (powered off) after a prior
        # refresh. Harmless if init() already powered it on this cycle.
        self.send_command(0x04)
        self.ReadBusy()
        if self.three_color:
            # Tri-color: 0x10 is the B/W channel (white = 0xFF) and 0x13 is the
            # red channel (no-red blank). The mono values (0x10 <- 0x00) would
            # clear the panel to black on a BWR panel.
            self.send_command(0x10)
            self.send_data2([0xFF] * int(self.width * self.height / 8))
            epdconfig.delay_ms(10)
            self.send_command(0x13)
            self.send_data2(self._red_for_panel(self._red_blank()))
            epdconfig.delay_ms(10)
            self.TurnOnDisplay()
            self.buffer = [0xFF] * int(self.width * self.height / 8)
            self.red_buffer = self._red_blank()
            return
        self.send_command(0x10)
        self.send_data2([0x00] * int(self.width * self.height / 8))
        epdconfig.delay_ms(10)
        self.send_command(0x13)
        self.send_data2([0xFF] * int(self.width * self.height / 8))
        epdconfig.delay_ms(10)
        self.TurnOnDisplay()
        
        # Update internal buffer to match cleared display state (all white).
        # Without this, the next DisplayPartial() would use stale buffer content
        # as the "old" image, causing ghosting artifacts.
        self.buffer = [0xFF] * int(self.width * self.height / 8)

    def sleep(self):
        self.send_command(0X50)
        self.send_data(0xf7)
        self.send_command(0X02)
        self.send_command(0X07)
        self.send_data(0xA5)
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()

    def idle_sleep(self):
        """Park the panel in the fully-settled deep-sleep state during inactivity.

        Emits the same panel sequence as sleep() -- VCOM/border settle (0x50/0xf7),
        power off (0x02), deep sleep (0x07/0xA5) -- which settles the pixels into a
        stable bistable state. Unlike sleep(), this does NOT call module_exit(): the
        SPI/GPIO handles stay open so the scheduler can wake the panel via the
        existing init() transition (init() calls reset(), which is what exits deep
        sleep). Keeping SPI open also avoids a double-close at shutdown.

        This hardens the display against the light-induced darkening that occurs
        when an e-ink panel is left un-settled: a bright/IR source (phone flashlight,
        sunlight) photo-discharges un-settled pixels, and with no controller running
        the image drifts dark. The settled deep-sleep state resists this even after a
        hard power cut. The shorter delay (vs sleep()'s 2000 ms) is sufficient because
        power is not being removed.
        """
        self.send_command(0X50)
        self.send_data(0xf7)
        self.send_command(0X02)
        self.send_command(0X07)
        self.send_data(0xA5)
        epdconfig.delay_ms(100)
