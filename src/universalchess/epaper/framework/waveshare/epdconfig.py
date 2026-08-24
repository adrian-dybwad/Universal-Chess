# /*****************************************************************************
# * | File        :	  epdconfig.py
# * | Author      :   Waveshare team
# * | Function    :   Hardware underlying interface
# * | Info        :
# *----------------
# * | This version:   V1.2
# * | Date        :   2022-10-29
# * | Info        :   
# ******************************************************************************
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

import contextlib
import logging
import os
import struct
import sys
import time
from pathlib import Path

from ctypes import *

from universalchess.board.profile import (
    GPIO_BACKEND_LIBGPIOD,
    get_board_profile,
)

logger = logging.getLogger(__name__)


class RaspberryPi:
    # Pin definition
    RST_PIN  = 12 #GPIO 12
    DC_PIN   = 16 #GPIO 16
    CS_PIN   = 18 #GPIO 18
    BUSY_PIN = 7 #GPIO 7
    PWR_PIN  = 18 # Probably permanently connected to power
    MOSI_PIN = 10
    SCLK_PIN = 11
    
    # Display rotation (0, 90, 180, or 270 degrees)
    ROTATION = 180

    def __init__(self):
        import spidev
        import gpiozero
        
        self.SPI = spidev.SpiDev()
        self.GPIO_RST_PIN    = gpiozero.LED(self.RST_PIN)
        self.GPIO_DC_PIN     = gpiozero.LED(self.DC_PIN)
        # self.GPIO_CS_PIN     = gpiozero.LED(self.CS_PIN)
        # self.GPIO_PWR_PIN    = gpiozero.LED(self.PWR_PIN)
        # BUSY is read synchronously via digital_read (.value); no edge/hold
        # callback is ever attached. A plain InputDevice claims the line for
        # value reads only, so the lgpio alert thread (lgPthAlert) -- which
        # ppolls at a hardcoded 0.5 ms timeout (~2000 Hz) whenever any alert is
        # claimed -- stays parked. Using gpiozero.Button here armed that loop and
        # cost ~10% of a single armv6 core continuously. .value semantics are
        # identical for pull_up=False (1 == HIGH == busy).
        self.GPIO_BUSY_PIN   = gpiozero.InputDevice(self.BUSY_PIN, pull_up = False)

        

    def digital_write(self, pin, value):
        if pin == self.RST_PIN:
            if value:
                self.GPIO_RST_PIN.on()
            else:
                self.GPIO_RST_PIN.off()
        elif pin == self.DC_PIN:
            if value:
                self.GPIO_DC_PIN.on()
            else:
                self.GPIO_DC_PIN.off()
        # elif pin == self.CS_PIN:
        #     if value:
        #         self.GPIO_CS_PIN.on()
        #     else:
        #         self.GPIO_CS_PIN.off()
        # elif pin == self.PWR_PIN:
        #     if value:
        #         self.GPIO_PWR_PIN.on()
        #     else:
        #         self.GPIO_PWR_PIN.off()

    def digital_read(self, pin):
        if pin == self.BUSY_PIN:
            return self.GPIO_BUSY_PIN.value
        elif pin == self.RST_PIN:
            return self.RST_PIN.value
        elif pin == self.DC_PIN:
            return self.DC_PIN.value
        # elif pin == self.CS_PIN:
        #     return self.CS_PIN.value
        elif pin == self.PWR_PIN:
            return self.PWR_PIN.value

    def delay_ms(self, delaytime):
        time.sleep(delaytime / 1000.0)

    def spi_writebyte(self, data):
        self.SPI.writebytes(data)

    def spi_writebyte2(self, data):
        self.SPI.writebytes2(data)

    def DEV_SPI_write(self, data):
        self.DEV_SPI.DEV_SPI_SendData(data)

    def DEV_SPI_nwrite(self, data):
        self.DEV_SPI.DEV_SPI_SendnData(data)

    def DEV_SPI_read(self):
        return self.DEV_SPI.DEV_SPI_ReadData()

    def module_init(self, cleanup=False):
        # self.GPIO_PWR_PIN.on()
        
        if cleanup:
            find_dirs = [
                os.path.dirname(os.path.realpath(__file__)),
                '/usr/local/lib',
                '/usr/lib',
            ]
            self.DEV_SPI = None
            for find_dir in find_dirs:
                # Pointer width of the running interpreter selects the matching
                # native lib. This is more correct than shelling to `getconf
                # LONG_BIT` (the OS default), which can mismatch a 32-bit Python
                # running on a 64-bit OS, and avoids a shell subprocess entirely.
                val = struct.calcsize("P") * 8
                logging.debug("System is %d bit"%val)
                if val == 64:
                    so_filename = os.path.join(find_dir, 'DEV_Config_64.so')
                else:
                    so_filename = os.path.join(find_dir, 'DEV_Config_32.so')
                if os.path.exists(so_filename):
                    self.DEV_SPI = CDLL(so_filename)
                    break
            if self.DEV_SPI is None:
                RuntimeError('Cannot find DEV_Config.so')

            self.DEV_SPI.DEV_Module_Init()

        else:
            # SPI device, bus = 0, device = 0.
            # Close any handle left open by a previous module_init() before
            # re-opening. spidev.open() does not release the prior fd, so calling
            # module_init() repeatedly (every live profile/mode switch and every
            # partial->full re-init) leaked a /dev/spidev fd each time and
            # eventually failed with OSError [Errno 24] Too many open files,
            # wedging the panel until the process/board was power-cycled. close()
            # on a not-open handle is a no-op-or-raises, so it is guarded.
            with contextlib.suppress(Exception):
                self.SPI.close()
            self.SPI.open(1, 0)
            self.SPI.max_speed_hz = 4000000
            self.SPI.mode = 0b00
        return 0

    def module_exit(self, cleanup=False):
        logger.debug("spi end")
        self.SPI.close()

        self.GPIO_RST_PIN.off()
        self.GPIO_DC_PIN.off()
        # self.GPIO_PWR_PIN.off()
        logger.debug("close 5V, Module enters 0 power consumption ...")
        
        if cleanup:
            self.GPIO_RST_PIN.close()
            self.GPIO_DC_PIN.close()
            # self.GPIO_CS_PIN.close()
            # self.GPIO_PWR_PIN.close()
            self.GPIO_BUSY_PIN.close()

        

class UnconfiguredEpaper:
    """libgpiod stand-in used when e-paper SPI/GPIO have not been measured.

    Constructing :class:`RaspberryPi` here would claim BCM 12/16/7/18 through
    gpiozero, which are the wrong H618 lines. module_init must fail closed
    rather than opening SPI 1.0 (onboard flash / Pi SPI0 header).
    """

    RST_PIN = None
    DC_PIN = None
    CS_PIN = None
    BUSY_PIN = None
    PWR_PIN = None
    MOSI_PIN = None
    SCLK_PIN = None
    ROTATION = 180

    def __init__(self, profile):
        self.profile = profile
        self.SPI = None

    def _refuse(self):
        raise RuntimeError(
            "e-paper SPI/GPIO is not configured for this board "
            f"(profile {self.profile.id}); refusing gpiozero BCM pins"
        )

    def digital_write(self, pin, value):
        self._refuse()

    def digital_read(self, pin):
        self._refuse()

    def delay_ms(self, delaytime):
        time.sleep(delaytime / 1000.0)

    def spi_writebyte(self, data):
        self._refuse()

    def spi_writebyte2(self, data):
        self._refuse()

    def module_init(self, cleanup=False):
        self._refuse()

    def module_exit(self, cleanup=False):
        return None




SPI_MASTER_SYSFS = "/sys/class/spi_master"


def epaper_pins_configured(profile):
    """True when libgpiod e-paper line offsets and gpiochip are all set."""
    return None not in (
        profile.gpiochip,
        profile.epaper_rst,
        profile.epaper_dc,
        profile.epaper_busy,
        profile.epaper_cs,
    )


SPI_GPIO_DRIVERS = frozenset({"spi-gpio", "spi_gpio"})


def find_spi_gpio_bus(sysfs_root=None):
    """Return (bus, device) for the spi-gpio master, or None.

    Hardware SPI0 on this board is the onboard NOR. Opening that spidev
    talks to flash, not the Centaur panel. Only a master whose driver is
    spi-gpio (DT compatible) or spi_gpio (the kernel platform driver
    directory on this Armbian kernel) is the overlay bus.
    """
    root = Path(sysfs_root or SPI_MASTER_SYSFS)
    if not root.is_dir():
        return None
    buses = []
    for master in sorted(root.iterdir()):
        name = master.name
        if not name.startswith("spi") or not name[3:].isdigit():
            continue
        driver = master / "device" / "driver"
        if not driver.exists():
            continue
        if driver.resolve().name in SPI_GPIO_DRIVERS:
            buses.append(int(name[3:]))
    if not buses:
        return None
    return buses[0], 0


class LibgpiodEpaper:
    """e-paper GPIO via libgpiod and SPI via the spi-gpio overlay.

    Pin numbers are gpiochip line offsets from the board profile. CS is
    owned by spi-gpio (active-low), matching RaspberryPi leaving CS to the
    SPI controller. SPI.open uses the spi-gpio master, never bus 0 (NOR)
    or bus 1 (Pi SPI1 / H618 SPI1 on the wrong header).
    """

    PWR_PIN = None
    MOSI_PIN = None
    SCLK_PIN = None
    ROTATION = 180

    def __init__(self, profile):
        import spidev

        self.profile = profile
        self.RST_PIN = profile.epaper_rst
        self.DC_PIN = profile.epaper_dc
        self.CS_PIN = profile.epaper_cs
        self.BUSY_PIN = profile.epaper_busy
        self.SPI = spidev.SpiDev()
        self._request = None

    def digital_write(self, pin, value):
        if self._request is None or pin == self.CS_PIN:
            return
        from gpiod.line import Value

        self._request.set_value(
            pin, Value.ACTIVE if value else Value.INACTIVE
        )

    def digital_read(self, pin):
        if self._request is None:
            return 0
        from gpiod.line import Value

        return int(self._request.get_value(pin) == Value.ACTIVE)

    def delay_ms(self, delaytime):
        time.sleep(delaytime / 1000.0)

    def spi_writebyte(self, data):
        self.SPI.writebytes(data)

    def spi_writebyte2(self, data):
        self.SPI.writebytes2(data)

    def module_init(self, cleanup=False):
        found = find_spi_gpio_bus()
        if found is None:
            raise RuntimeError(
                "e-paper SPI is spi-gpio; overlay not loaded "
                "(no spi-gpio SPI master)"
            )
        bus, device = found
        import gpiod
        from gpiod.line import Direction, Value

        chip = self.profile.gpiochip or "gpiochip1"
        path = chip if str(chip).startswith("/") else f"/dev/{chip}"
        config = {
            self.RST_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.INACTIVE
            ),
            self.DC_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.INACTIVE
            ),
            self.BUSY_PIN: gpiod.LineSettings(direction=Direction.INPUT),
        }
        # display_boot initialize() and board.init_display() both call this
        # on the singleton. Holding the first request_lines makes the second
        # raise EBUSY and crash-loop the service.
        self._release_line_request()
        self._request = gpiod.request_lines(
            path, consumer="universalchess-epaper", config=config
        )
        with contextlib.suppress(Exception):
            self.SPI.close()
        self.SPI.open(bus, device)
        self.SPI.max_speed_hz = 4000000
        self.SPI.mode = 0b00
        return 0

    def _release_line_request(self):
        if self._request is not None:
            with contextlib.suppress(Exception):
                self._request.release()
            self._request = None

    def module_exit(self, cleanup=False):
        with contextlib.suppress(Exception):
            self.SPI.close()
        self._release_line_request()
        return None


def backend_for_profile(profile):
    """Return the e-paper GPIO/SPI backend for a board profile.

    gpiozero (Raspberry Pi, and unknown hosts with no device-tree model) keeps
    the shipping RaspberryPi driver so CI and laptops still import epdconfig.
    libgpiod boards with measured RST/DC/BUSY/CS and a gpiochip use
    :class:`LibgpiodEpaper`. libgpiod boards whose lines are still unset get
    :class:`UnconfiguredEpaper` instead of gpiozero BCM numbers.
    """
    if profile.gpio_backend == GPIO_BACKEND_LIBGPIOD:
        if epaper_pins_configured(profile):
            return LibgpiodEpaper(profile)
        return UnconfiguredEpaper(profile)
    return RaspberryPi()


implementation = backend_for_profile(get_board_profile())

for func in [x for x in dir(implementation) if not x.startswith('_')]:
    setattr(sys.modules[__name__], func, getattr(implementation, func))

### END OF FILE ###
