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
from PIL import Image
import numpy as np
import RPi.GPIO as GPIO

log = logging.getLogger(__name__)

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


# Display resolution
EPD_WIDTH       = 128
EPD_HEIGHT      = 296

# Debug flag for buffer diagnostics in DisplayPartial
# Set to True to print buffer statistics on each partial refresh
DEBUG_DISPLAY_PARTIAL = False


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
    def __init__(self):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        # Set True by init() when it returns -1 specifically because the BUSY
        # line never reached idle within the timeout (the inverted-polarity V1
        # panel signature). Lets the startup selector distinguish that case --
        # which warrants the SSD1680 fallback -- from other init failures.
        self.busy_timeout_occurred = False
        # Store the last image sent for partial refresh
        self.buffer = [0xFF] * int(self.width * self.height / 8)
         
    lut_vcom1 = [  
        0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00,
    ]

    lut_ww1 = [  
        0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]

    lut_bw1 = [  
        0x80, 0x19, 0x01, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]

    lut_wb1 = [
        0x40, 0x19, 0x01, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]

    lut_bb1 = [ 
        0x00, 0x19, 0x01, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]
        
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
        
    def ReadBusy(self):
        """Poll the panel BUSY line until idle, bounded by BUSY_TIMEOUT_SECONDS.

        Waits while the pin reads LOW (busy) and returns once it reads HIGH
        (idle). An unresponsive or incompatible panel -- notably a V1 panel with
        inverted BUSY polarity, or no panel at all -- never drives the expected
        idle level, so without a deadline this loop never returns and the
        display thread hangs. The bounded wait converts that hang into an
        EPDTimeoutError; init() catches it and returns -1.

        Raises:
            EPDTimeoutError: if BUSY does not reach idle within the timeout.
        """
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while(epdconfig.digital_read(self.busy_pin) == 0):  # LOW: busy, HIGH: idle
            self.send_command(0x71)
            epdconfig.delay_ms(10)
            if time.monotonic() >= deadline:
                raise EPDTimeoutError(
                    f"BUSY not released within {BUSY_TIMEOUT_SECONDS}s; panel "
                    "unresponsive or incompatible (e.g. inverted BUSY polarity)"
                )
        
    def TurnOnDisplay(self):
        self.send_command(0x12)
        epdconfig.delay_ms(10)
        self.ReadBusy()
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
        self.send_data(0x1f)        # LUT from OTP，KW-BF   KWR-AF    BWROTP 0f   BWOTP 1f

        self.send_command(0x61)     #resolution setting
        self.send_data (0x80)       
        self.send_data (0x01)       
        self.send_data (0x28)       

        self.send_command(0X50)     #VCOM AND DATA INTERVAL SETTING
        self.send_data(0x97)        #WBmode  VBDF 17|D7 VBDW 97  VBDB 57   WBRmode  VBDF F7 VBDW 77  VBDB 37  VBDR B7

        return 0
    
    def SetPartReg(self):
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
        self.send_data(0xbf)     #LUT from OTP，128x296

        self.send_command(0x30) #PLL setting
        self.send_data(0x3a)     # 3a 100HZ   29 150Hz 39 200HZ 31 171HZ

        self.send_command(0x61) #resolution setting
        self.send_data(self.width)
        self.send_data((self.height >> 8) & 0xff)
        self.send_data(self.height & 0xff)

        self.send_command(0x82) #vcom_DC setting
        self.send_data(0x12)

        self.send_command(0X50)     #VCOM AND DATA INTERVAL SETTING
        self.send_data(0x97)

        self.send_command(0x20)         #vcom
        self.send_data2(self.lut_vcom1)
        self.send_command(0x21)         # ww --
        self.send_data2(self.lut_ww1)
        self.send_command(0x22)         # bw r
        self.send_data2(self.lut_bw1)
        self.send_command(0x23)         # wb w
        self.send_data2(self.lut_wb1)
        self.send_command(0x24)         # bb b
        self.send_data2(self.lut_bb1)

    def getbuffer(self, image):
        return pack_image_to_buffer(image, self.width, self.height)

    def display(self, image):
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
        self.TurnOnDisplay()
        
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
    
    def DisplayPartial(self, image):
        """
        Display partial refresh following Waveshare pattern.
        
        Args:
            image: Buffer containing the new/current content (sent to 0x13)
        """
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
