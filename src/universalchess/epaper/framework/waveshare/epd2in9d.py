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
from . import epdconfig
from PIL import Image
import RPi.GPIO as GPIO

# Display resolution
EPD_WIDTH       = 128
EPD_HEIGHT      = 296

# Debug flag for buffer diagnostics in DisplayPartial
# Set to True to print buffer statistics on each partial refresh
DEBUG_DISPLAY_PARTIAL = False

class EPD:
    def __init__(self):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
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
        while(epdconfig.digital_read(self.busy_pin) == 0):      # 0: idle, 1: busy
            self.send_command(0x71)
            epdconfig.delay_ms(10)  
        
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
        if (epdconfig.module_init() != 0):
            return -1
        # EPD hardware init start
        self.reset()
        
        self.send_command(0x04)
        self.ReadBusy() #waiting for the electronic paper IC to release the idle signal

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
        buf = [0xFF] * (int(self.width/8) * self.height)
        image_monocolor = image.convert('1')
        imwidth, imheight = image_monocolor.size
        pixels = image_monocolor.load()
        if(imwidth == self.width and imheight == self.height):
            for y in range(imheight):
                for x in range(imwidth):
                    if pixels[x, y] == 0:
                        buf[int((x + y * self.width) / 8)] &= ~(0x80 >> (x % 8))
        elif(imwidth == self.height and imheight == self.width):
            for y in range(imheight):
                for x in range(imwidth):
                    newx = y
                    newy = self.height - x - 1
                    if pixels[x, y] == 0:
                        buf[int((newx + newy*self.width) / 8)] &= ~(0x80 >> (y % 8))
        return buf

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
        self.buffer = [0xFF] * int(self.width * self.height / 8)    
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
