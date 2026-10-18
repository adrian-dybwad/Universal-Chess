"""Replacement Good Display 2.9\" panels: packing, init bytes, and selection.

Why these tests exist:
    Five modules share the Centaur connector and do not share a command set.
    A wrong init drives the glass with another controller's voltages, so each
    explicit panel must emit its published sequence and the automatic probe
    must not run when one is selected. The chess frame stays 128x296; larger
    glass is scaled, and four-color RAM is 2-bit.

How a regression manifests:
    - A reordered F52 init (0x65 after the tail instead of after TRES) fails
      the command-order assertion and the panel comes up blank or garbled.
    - Scaling that letterboxes instead of filling leaves a white margin, and
      the all-white scale test stops returning every bit set.
    - Four-color white packed as 0xFF (the 1-bit white byte) paints the panel
      red, because 11 is red. The pack test expects 0x55.
    - An explicit panel that still calls build_epd probes the Centaur
      controllers and can power the wrong glass.
"""

import sys
import unittest
from unittest.mock import MagicMock

for _mod in ("spidev", "RPi", "RPi.GPIO", "gpiozero"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.app import display_boot
from universalchess.board import display_settings
from universalchess.board.display_selection import DisplayAttempt
from universalchess.epaper.framework.scheduler import Scheduler
from universalchess.epaper.framework.waveshare import epd2in9_jd7966x as jd
from universalchess.epaper.framework.waveshare import epd2in9_ssd1685 as ssd1685
from universalchess.epaper.framework.waveshare import epdconfig
from universalchess.epaper.framework.waveshare import frame_codec
from universalchess.epaper.framework.waveshare import panel_models as panels
from universalchess.epaper.framework.waveshare import waveform_profiles as wp


LOGICAL_BYTES = 128 * 296 // 8


def _payloads_of(epd, action):
    """Record every bulk ``send_data2`` payload, in order, while ``action`` runs.

    On the UC8151D refresh paths the first five payloads are the LUTs (0x20
    VCOM first, whose second byte names the waveform), then the 0x10 old
    plane and the 0x13 new plane.
    """
    payloads = []
    epd.send_command = MagicMock()
    epd.send_data = MagicMock()
    epd.send_data2 = lambda data: payloads.append(list(data))
    action()
    return payloads


# Second byte of the 0x20 VCOM LUT: 0x0A is the GxEPD2_4G gray waveform, 0x10
# is the GDEW029I6FD fast partial.
GRAY4_VCOM_PHASE = 0x0A
PARTIAL_VCOM_PHASE = 0x10
OLD_PLANE, NEW_PLANE = 5, 6


def _i6fd_driver():
    """The UC8151D driver constructed for the GDEW029I6FD glass."""
    from universalchess.epaper.framework.waveshare import epd2in9d

    return epd2in9d.EPD(
        profile=wp.get_profile("uc8151d_gdew029i6fd", wp.CONTROLLER_UC8151D),
        panel=panels.get_model("gdew029i6fd"),
    )


def _built_panel(key, stored_waveform=""):
    """The driver the boot path constructs for replacement panel ``key``."""
    return display_boot.build_panel_epd(panels.get_model(key), stored_waveform, False, False)


def _commands_of(epd, action):
    """Record (command, data bytes) in call order while ``action`` runs."""
    trace = []
    current = {"cmd": None, "data": []}

    def send_command(command):
        if current["cmd"] is not None:
            trace.append((current["cmd"], tuple(current["data"])))
        current["cmd"] = command
        current["data"] = []

    def send_data(data):
        current["data"].append(data)

    epd.send_command = send_command
    epd.send_data = send_data
    epd.send_data2 = MagicMock()
    epd.reset = MagicMock()
    epd.ReadBusy = MagicMock()
    action()
    if current["cmd"] is not None:
        trace.append((current["cmd"], tuple(current["data"])))
    return trace


class FrameCodecTests(unittest.TestCase):
    def test_scale_identity_copies_bytes(self):
        # Same size must not resample. A regression that always resamples can
        # flip edge pixels on the 128x296 panels.
        source = [0xA5] * LOGICAL_BYTES
        self.assertEqual(frame_codec.scale_1bpp(source, 128, 296, 128, 296), source)

    def test_scale_keeps_solid_white_and_black(self):
        # Nearest-neighbor of a solid frame stays solid. A regression that
        # letterboxes writes zeros (black) into the margin of a white frame.
        white = [0xFF] * LOGICAL_BYTES
        black = [0x00] * LOGICAL_BYTES
        scaled_white = frame_codec.scale_1bpp(white, 128, 296, 168, 384)
        scaled_black = frame_codec.scale_1bpp(black, 128, 296, 168, 384)
        self.assertEqual(len(scaled_white), 168 * 384 // 8)
        self.assertTrue(all(byte == 0xFF for byte in scaled_white))
        self.assertTrue(all(byte == 0x00 for byte in scaled_black))

    def test_scale_maps_last_column_from_last_source_column(self):
        # dest x 167 -> 167*128//168 = 127, so the right edge comes from the
        # source's right edge. A regression using round-to-nearest can pull
        # column 126 and the assertion on the top bit of the last dest byte fails.
        source = [0x00] * LOGICAL_BYTES
        source[(127) // 8] = 0x01  # pixel x=127 of row 0 is the low bit of byte 15
        # byte index for x=127: 127//8 = 15, bit 7-(127%8) = bit 0. Set it.
        source[15] = 0x01
        scaled = frame_codec.scale_1bpp(source, 128, 296, 168, 384)
        # dest x=167 is bit 0 of byte 20 (167//8 = 20, 167%8 = 7, shift 0).
        self.assertEqual(scaled[20] & 0x01, 0x01)

    def test_pack_bwry_color_bytes(self):
        # Vendor demo: white is 0x55, black is 0x00, red is 0xFF. Packing white
        # as 0xFF paints the panel red (11 is red).
        white = [0xFF] * 4  # 32 white pixels -> 8 packed bytes
        black = [0x00] * 4
        red = [0x00] * 4
        self.assertEqual(frame_codec.pack_bwry(white, 32, 1), [0x55] * 8)
        self.assertEqual(frame_codec.pack_bwry(black, 32, 1), [0x00] * 8)
        self.assertEqual(frame_codec.pack_bwry(white, 32, 1, red), [0xFF] * 8)


class PanelCatalogTests(unittest.TestCase):
    def test_five_skus_and_automatic(self):
        # Each purchased module is a choice, and Automatic remains so a Centaur
        # panel is unchanged. A missing key drops that glass from the dropdown.
        keys = [panel.key for panel in panels.all_panels()]
        self.assertEqual(keys, [
            "auto",
            "gdew029i6fd",
            "gdey029t94",
            "gdey029z95",
            "gdey029t71h",
            "gdey029f51h",
            "gdey029f52",
        ])

    def test_native_sizes_and_partial_flags(self):
        # 168x384 is the driver orientation (short side first), matching the
        # 128x296 chess frame. Marketing copy says 384x168. Swapping the axes
        # scales the board onto the wrong edge.
        self.assertEqual(
            (panels.get_model("gdey029t71h").native_width,
             panels.get_model("gdey029t71h").native_height),
            (168, 384),
        )
        self.assertEqual(
            (panels.get_model("gdey029f51h").native_width,
             panels.get_model("gdey029f51h").native_height),
            (168, 384),
        )
        self.assertFalse(panels.get_model("gdey029f51h").supports_partial)
        self.assertFalse(panels.get_model("gdey029f52").supports_partial)
        self.assertTrue(panels.get_model("gdey029t94").supports_partial)
        self.assertEqual(panels.get_model("gdey029t94").pinned_profile, "gdey029t94")
        self.assertEqual(
            panels.get_model("gdew029i6fd").pinned_profile, "uc8151d_gdew029i6fd",
        )

    def test_unknown_key_is_automatic(self):
        # A stale stored key must keep the V1/V2 probe. Raising would boot
        # with no display.
        self.assertEqual(panels.get_model("not-a-panel").key, "auto")
        self.assertFalse(panels.is_known_panel("not-a-panel"))


class Gdey029T94Tests(unittest.TestCase):
    def setUp(self):
        self._orig = (epdconfig.digital_read, epdconfig.delay_ms, epdconfig.module_init)
        epdconfig.digital_read = MagicMock(return_value=0)
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read, epdconfig.delay_ms, epdconfig.module_init = self._orig

    def test_profile_has_no_register_lut(self):
        # GxEPD2 loads no 0x32 LUT for this panel. A 159-byte LUT here would
        # be the GDEM029T94 table, which is the wrong waveform for GDEY029T94.
        profile = wp.get_profile("gdey029t94", wp.CONTROLLER_SSD16XX, panel="gdey029t94")
        self.assertEqual(profile.driver, wp.DRIVER_GDEY029T94)
        self.assertEqual(profile.full_lut, ())
        self.assertEqual(profile.partial_lut, ())

    def test_init_skips_register_lut(self):
        epd = _built_panel("gdey029t94")
        trace = _commands_of(epd, epd.init)
        commands = [command for command, _data in trace]
        self.assertIn(0x12, commands)
        self.assertNotIn(0x32, commands)

    def test_full_refresh_uses_fast_otp_sequence(self):
        # Temperature 0x64 then activation 0xD7. Falling through to 0xC7 runs
        # the GDEM029T94 register LUT, which this profile never loaded.
        epd = _built_panel("gdey029t94")
        # The activity probe is a hardware check. These tests assert command
        # bytes, and a mocked idle BUSY pin would fail that probe.
        epd._busy_activity_confirmed = True
        epd.init()
        image = [0xFF] * LOGICAL_BYTES
        trace = _commands_of(epd, lambda: epd.display(image))
        self.assertIn((0x1A, (0x64,)), trace)
        self.assertIn((0x22, (0xD7,)), trace)
        self.assertNotIn((0x22, (0xC7,)), trace)

    def test_partial_activates_0xfc(self):
        # OTP partial is 0xFC, not the register partial 0x0F and not a full
        # refresh. Routing it to full makes every clock tick flash.
        epd = _built_panel("gdey029t94")
        epd._busy_activity_confirmed = True
        epd.init()
        image = [0xFF] * LOGICAL_BYTES
        trace = _commands_of(epd, lambda: epd.DisplayPartial(image))
        self.assertIn((0x22, (0xFC,)), trace)
        self.assertNotIn((0x22, (0xD7,)), trace)


class Gdey029Z95Tests(unittest.TestCase):
    def setUp(self):
        self._orig = (epdconfig.digital_read, epdconfig.delay_ms, epdconfig.module_init)
        epdconfig.digital_read = MagicMock(return_value=0)
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read, epdconfig.delay_ms, epdconfig.module_init = self._orig

    def _epd(self):
        epd = _built_panel("gdey029z95")
        epd._busy_activity_confirmed = True
        return epd

    def test_catalog_is_bwr_partial_with_the_ssd1680_waveforms(self):
        # The glass is tri-color with a 1.5 s black/white partial, driven by
        # an SSD1680. The Centaur SSD1680 waveforms also drive it (the Waveshare
        # GDEM029T94 table gives the steadiest clock), so the dropdown offers
        # them beside the vendor recipe, which stays the default. How a
        # regression manifests: waveform_applicable False (no dropdown), a
        # family other than ssd16xx, or the card losing its bwr copy.
        model = panels.get_model("gdey029z95")
        self.assertEqual(model.controller, panels.CONTROLLER_SSD1680)
        self.assertEqual(model.pinned_profile, "gdey029z95")
        self.assertTrue(model.supports_partial)
        self.assertTrue(model.three_color_supported)
        self.assertFalse(model.high_contrast_supported)
        self.assertTrue(model.waveform_applicable)
        self.assertEqual(model.waveform_controller, wp.CONTROLLER_SSD16XX)
        self.assertEqual(model.panel_mode, "bwr")
        self.assertEqual((model.native_width, model.native_height), (128, 296))

    def test_stored_waveform_selects_the_sequence(self):
        # Why: the SSD1680 driver picks its command sequence from the waveform,
        # so a Centaur waveform on this panel runs the same path as Automatic
        # did on this glass: register LUT 0x32 and the Y-increment window. The
        # GDEY029T94 recipe belongs to other glass and falls back to the
        # vendor recipe. How a regression manifests: gdem029t94 is replaced by
        # the vendor recipe (no 0x32), or gdey029t94 is loaded here.
        cases = (
            ("gdem029t94", "gdem029t94", True),
            ("", "gdey029z95", False),
            ("gdey029t94", "gdey029z95", False),
        )
        for stored, expected_key, loads_register_lut in cases:
            with self.subTest(stored=stored):
                epd = _built_panel("gdey029z95", stored)
                epd._busy_activity_confirmed = True
                self.assertEqual(epd.profile.key, expected_key)
                commands = [command for command, _data in _commands_of(epd, epd.init)]
                self.assertEqual(0x32 in commands, loads_register_lut)

    def test_hidden_high_contrast_is_not_driven(self):
        # Why: high contrast is one setting shared by every panel, and the
        # card hides it where the panel does not offer it. A value left on
        # from Automatic must not raise the voltages on this glass. How a
        # regression manifests: epd.high_contrast True on GDEY029Z95, or
        # False on GDEY029T94, which offers the switch.
        for key, expected in (("gdey029z95", False), ("gdey029t94", True)):
            with self.subTest(panel=key):
                epd = display_boot.build_panel_epd(
                    panels.get_model(key), "gdem029t94", True, False,
                )
                self.assertEqual(epd.high_contrast, expected)

    def test_init_uses_vendor_y_decrement_and_no_lut(self):
        # EPD_HW_Init: data entry 0x01 and Y window starting at 295 (0x27, 0x01).
        # A 0x03 window (the Centaur SSD1680 default) writes the first byte at
        # Y=0, which this demo places at the end of the scan. A 0x32 LUT is the
        # GDEM029T94 table and is not in the demo.
        epd = self._epd()
        trace = _commands_of(epd, epd.init)
        commands = [command for command, _data in trace]
        self.assertIn((0x11, (0x01,)), trace)
        self.assertIn((0x45, (0x27, 0x01, 0x00, 0x00)), trace)
        self.assertNotIn(0x32, commands)

    def test_full_refresh_sends_inverted_red_and_row_reversed_top(self):
        # Vendor white is 0xFF on 0x24 and 0x00 on 0x26 (no red). Sending the
        # B/W image to 0x26 paints white pixels red. Data entry 0x01 puts the
        # first transmitted byte on the last row, so a black top row must be
        # the last 16 bytes on the wire or the board is upside down.
        epd = self._epd()
        epd.init()
        bw = [0xFF] * LOGICAL_BYTES
        bw[0:16] = [0x00] * 16  # row 0 black
        red = [0xFF] * LOGICAL_BYTES
        red[0] = 0x00  # row 0, first byte is red in mask space
        payloads = []

        def send_data2(data):
            payloads.append(list(data))

        epd.send_command = MagicMock()
        epd.send_data = MagicMock()
        epd.send_data2 = send_data2
        epd.ReadBusy = MagicMock()
        epd.display_color(bw, red)
        self.assertEqual(len(payloads), 2)
        bw_wire, red_wire = payloads
        # Row 0 is transmitted last. Its first byte is red, so the B/W plane
        # is forced white there (a pixel is never both black and red) and the
        # rest of that black row stays black.
        self.assertEqual(bw_wire[-16], 0xFF)
        self.assertEqual(bw_wire[-15:], [0x00] * 15)
        self.assertEqual(bw_wire[0], 0xFF)
        # Mask 0x00 (red) is inverted to 0xFF on the wire, at the last row.
        self.assertEqual(red_wire[-16], 0xFF)
        self.assertEqual(red_wire[0], 0x00)

    def test_partial_basemap_then_0x1c(self):
        # The vendor demo requires EPD_SetRAMValue_BaseMap (activation 0xF4)
        # before any 0x1C partial. Skipping it is what they call an unstable
        # partial. The second partial must be 0x1C and must not repeat 0xF4,
        # or every clock tick pays for a full refresh.
        epd = self._epd()
        epd.init()
        image = [0xFF] * LOGICAL_BYTES
        first = _commands_of(epd, lambda: epd.DisplayPartial(image))
        second = _commands_of(epd, lambda: epd.DisplayPartial(image))
        self.assertIn((0x22, (0xF4,)), first)
        self.assertNotIn((0x22, (0x1C,)), first)
        self.assertIn((0x22, (0x1C,)), second)
        self.assertNotIn((0x22, (0xF4,)), second)
        self.assertNotIn((0x22, (0x0F,)), second)


class Ssd1685Tests(unittest.TestCase):
    def test_window_starts_at_source_shift(self):
        # GxEPD2 SOURCE_SHIFT is 8: the glass is bonded to S8..S175. A window
        # starting at byte 0 drives the unbonded sources and clips the image.
        pairs = dict(ssd1685.ram_window_pairs())
        self.assertEqual(pairs[0x44], (1, 21))
        self.assertEqual(pairs[0x4E], (1,))

    def test_full_update_matches_gxepd2(self):
        # Fast full: bypass-red 0x40, temperature 0x6E 0x00, load 0x91, then
        # after the delay activation 0xC7. Dropping the temperature load runs
        # the slow OTP path.
        self.assertEqual(ssd1685.full_update_pairs()[:3], (
            (0x21, (0x40, 0x00)),
            (0x1A, (0x6E, 0x00)),
            (0x22, (0x91,)),
        ))
        self.assertEqual(ssd1685.full_update_activation_pairs(), (
            (0x22, (0xC7,)),
            (0x20, ()),
        ))
        self.assertEqual(ssd1685.partial_update_pairs()[1], (0x22, (0xDC,)))

    def test_gate_count_is_383(self):
        # Driver output is height-1 = 383 = 0x017F. 295 (0x0127) is the
        # 296-row SSD1680 value and would scan the wrong number of gates.
        pairs = dict(ssd1685.init_pairs())
        self.assertEqual(pairs[0x01], (0x7F, 0x01, 0x00))


class Jd7966xTests(unittest.TestCase):
    def test_f51h_resolution_and_booster(self):
        # TRES is source 168 then gate 384. The booster is the GxEPD2/F51H
        # full-init bytes, not the F52 fast booster.
        commands = [command for phase in jd.f51h_spec().phases for command, _data in phase]
        tres = dict(pair for phase in jd.f51h_spec().phases for pair in phase)[0x61]
        booster = dict(pair for phase in jd.f51h_spec().phases for pair in phase)[0x06]
        self.assertEqual(tres, (0x00, 168, 0x01, 128))  # 384 = 0x0180
        self.assertEqual(booster, (0x05, 0x00, 0x3F, 0x0A, 0x25, 0x12, 0x1A))
        self.assertEqual(commands[-1], 0x04)
        self.assertEqual(jd.f51h_spec().refresh_pairs[0], (0x50, (0x37,)))

    def test_f52_fast_init_order(self):
        # Vendor EPD_init_Fast: 0x65 immediately after TRES, then 0xE7, and
        # 0xA5 is its own phase before power-on 0x04. The fast booster is
        # 0x0F 0x0A ... not the F51H booster.
        phases = jd.f52_spec().phases
        first = [command for command, _data in phases[0]]
        self.assertLess(first.index(0x61), first.index(0x65))
        self.assertLess(first.index(0x65), first.index(0xE7))
        self.assertEqual(first[-1], 0xA5)
        self.assertEqual(phases[1], ((0x04, ()),))
        booster = dict(phases[0])[0x06]
        self.assertEqual(booster, (0x0F, 0x0A, 0x2F, 0x25, 0x22, 0x2E, 0x21))
        tres = dict(phases[0])[0x61]
        self.assertEqual(tres, (0x00, 128, 0x01, 40))  # 296 = 0x0128
        self.assertEqual(jd.f52_spec().refresh_pairs, ((0x12, (0x00,)),))

    def test_f52_display_sends_white_as_0x55(self):
        # A logical all-white frame must hit RAM 0x10 as 0x55 bytes. Sending
        # the 1-bit buffer (0xFF) would paint the panel solid red.
        epd = jd.EPD(jd.f52_spec())
        sent = {}

        def send_data2(data):
            sent["data"] = list(data)

        epd.send_command = MagicMock()
        epd.send_data = MagicMock()
        epd.send_data2 = send_data2
        epd.ReadBusy = MagicMock()
        epd.display([0xFF] * LOGICAL_BYTES)
        self.assertTrue(sent["data"])
        self.assertTrue(all(byte == 0x55 for byte in sent["data"]))
        self.assertEqual(len(sent["data"]), 128 * 296 // 4)


class ExplicitPanelBootTests(unittest.TestCase):
    def test_explicit_panel_does_not_probe_centaur_controllers(self):
        # build_epd is the V1/V2 probe. Running it for a selected F52 sends
        # UC8151D voltages to a JD79661. The replacement builder must be the
        # only constructor, and a failed init must not try the other driver.
        # The board then runs headless on the Manager whose init failed.
        from unittest.mock import patch
        built = []
        panel_built = []
        dead_panel = MagicMock(name="dead_panel_manager")

        def _build_epd(*args, **kwargs):
            built.append(args)
            return MagicMock(), MagicMock(key="x")

        def _build_panel(model, *args, **kwargs):
            panel_built.append(model.key)
            return MagicMock()

        with patch.object(display_settings, "read_panel_key", return_value="gdey029f52"), \
                patch.object(display_boot, "read_selection", return_value=("", False)), \
                patch.object(display_boot, "read_flag", return_value=False), \
                patch.object(display_boot, "build_epd", side_effect=_build_epd), \
                patch.object(display_boot, "build_panel_epd", side_effect=_build_panel), \
                patch.object(
                    display_boot, "attempt_display_init",
                    return_value=(dead_panel, DisplayAttempt(ok=False, error="busy")),
                ), \
                patch.object(display_boot, "_log_display_event"), \
                patch("universalchess.board.hardware_info.write_display_status"):
            manager, splash = display_boot.init_display()
        self.assertIs(manager, dead_panel)
        self.assertIsNone(splash)
        self.assertEqual(built, [])
        self.assertEqual(panel_built, ["gdey029f52"])


class SlowPanelSchedulerTests(unittest.TestCase):
    def test_no_partial_coalesces_to_one_full_refresh(self):
        # A four-color refresh is 11–25 s. Replaying a burst as partials would
        # queue several of those. The last frame is the one that must be drawn,
        # via display() (full), not DisplayPartial.
        calls = []

        class _Epd:
            width = 128
            height = 296
            supports_partial = False
            three_color = False

            def getbuffer(self, image):
                return [0xFF]

            def display(self, buf, should_abort=None):
                calls.append("display")

            def DisplayPartial(self, image):
                calls.append("partial")

        scheduler = Scheduler(MagicMock(), _Epd(), batch_updates=False)
        image = Image.new("1", (128, 296), 1)
        from concurrent.futures import Future
        first, second = Future(), Future()
        scheduler._process_batch([
            (False, first, image, None, False),
            (False, second, image, None, False),
        ])
        self.assertEqual(calls, ["display"])
        self.assertEqual(first.result(), "coalesced")
        self.assertEqual(second.result(), "full")


class Gdew029I6fdGrayTests(unittest.TestCase):
    """4-gray is the GDEW029I6FD path only.

    Why these tests exist:
        The flexible panel's published 4-gray waveform stores two bits in the
        old and new RAM planes. A dither, or another panel's 1-bit refresh,
        would paint dots or drive the wrong LUT.

    How a regression manifests:
        - Light gray packed as black (both planes 0) or white (both planes 1)
          shows the wrong tone. The plane bytes below are the GxEPD2_4G codes.
        - A checkerboard collapsed to one gray level means packing invented
          gray from a dither. Dark squares would then be gray on every panel.
        - Command 0x20 during a stock display() means the 4-gray LUT leaked
          onto a Centaur V2 panel.
    """

    def test_catalog_is_four_gray_only_for_i6fd(self):
        model = panels.get_model("gdew029i6fd")
        self.assertEqual(model.gray_levels, 4)
        self.assertEqual(model.panel_mode, "gray")
        self.assertTrue(model.supports_partial)
        self.assertFalse(model.three_color_supported)
        self.assertFalse(model.waveform_applicable)
        self.assertTrue(model.partial_only_supported)
        self.assertFalse(panels.get_model("gdey029t94").partial_only_supported)
        self.assertEqual(panels.get_model("gdey029t94").gray_levels, 1)
        self.assertNotEqual(panels.get_model("gdey029t94").panel_mode, "gray")
        self.assertEqual(panels.get_model("auto").panel_mode, "auto")

    def test_build_forces_gray_and_clears_red(self):
        # three_color True in the saved settings must not claim 0x13, which
        # is the second gray plane on this glass. A regression leaves
        # three_color set and the red path writes a mask into the gray bits.
        model = panels.get_model("gdew029i6fd")
        epd = display_boot.build_panel_epd(model, model.key, False, True)
        self.assertEqual(epd.gray_levels, 4)
        self.assertTrue(epd.supports_partial)
        self.assertFalse(epd.three_color)
        other = display_boot.build_panel_epd(
            panels.get_model("gdey029t94"), "gdey029t94", False, False,
        )
        self.assertFalse(other.partial_only_supported)
        self.assertFalse(other.partial_only)
        self.assertEqual(other.gray_levels, 1)

    def test_build_partial_only_drops_to_one_bit(self):
        # Why: the switch is stored as partial_only and applied at construction.
        # On, the flexible panel must dither (gray_levels 1) and mark every
        # refresh partial. Off, it stays 4-gray. Another panel must ignore
        # the flag so a leftover setting cannot drop its gray or enable a
        # mode it has no LUT for.
        # A regression leaves gray_levels at 4 while partial_only is set, so
        # the next refresh still loads the 3-second waveform, or sets
        # partial_only on T94.
        model = panels.get_model("gdew029i6fd")
        epd = display_boot.build_panel_epd(model, model.key, False, False, True)
        self.assertTrue(epd.partial_only)
        self.assertTrue(epd.partial_only_supported)
        self.assertEqual(epd.gray_levels, 1)
        self.assertFalse(epd.three_color)
        off = display_boot.build_panel_epd(model, model.key, False, False, False)
        self.assertFalse(off.partial_only)
        self.assertEqual(off.gray_levels, 4)
        other = display_boot.build_panel_epd(
            panels.get_model("gdey029t94"), "gdey029t94", False, False, True,
        )
        self.assertFalse(other.partial_only)
        self.assertEqual(other.gray_levels, 1)

    def test_partial_only_display_uses_the_partial_lut(self):
        # Why: with the switch on, the scheduler still asks for a full refresh
        # on a screen change. That call must load the I6FD partial LUT (phase
        # byte 0x10) and must not load the 4-gray VCOM (phase 0x0A). Turning
        # the switch off restores four gray levels. A driver that does not
        # advertise the switch must ignore the request.
        # A regression is payloads[0][1] == 0x0A, which is the 3-second
        # waveform, or gray_levels staying 4 so the UI keeps flat grays that
        # the partial LUT will smash to black or white.
        from universalchess.epaper.framework.waveshare import epd2in9d

        epd = _i6fd_driver()
        epd.ReadBusy = MagicMock()
        epdconfig.delay_ms = MagicMock()
        epd.apply_partial_only(True)
        self.assertTrue(epd.partial_only)
        self.assertEqual(epd.gray_levels, 1)

        payloads = []
        current_cmd = {"cmd": None, "data": []}

        def send_command(command):
            current_cmd["cmd"] = command
            current_cmd["data"] = []

        epd.send_command = send_command
        epd.send_data = lambda data: current_cmd["data"].append(data)
        epd.send_data2 = lambda data: payloads.append(list(data))
        epd.display([0xFF] * LOGICAL_BYTES)
        self.assertGreaterEqual(len(payloads), 1)
        self.assertEqual(payloads[0][1], 0x10)

        epd.apply_partial_only(False)
        self.assertFalse(epd.partial_only)
        self.assertEqual(epd.gray_levels, 4)

        stock = epd2in9d.EPD()
        stock.apply_partial_only(True)
        self.assertFalse(stock.partial_only)
        self.assertEqual(stock.gray_levels, 1)

    def test_flat_levels_pack_to_the_published_plane_bits(self):
        image = Image.new("L", (8, 1))
        image.putdata([255, 170, 85, 0, 255, 170, 85, 0])
        previous, current = frame_codec.pack_gray4(image, 8, 1)
        # white 11, light 10, dark 01, black 00, repeated.
        # previous bits: 1,1,0,0,1,1,0,0. current bits: 1,0,1,0,1,0,1,0.
        self.assertEqual(previous, [0xCC])
        self.assertEqual(current, [0xAA])

    def test_checkerboard_stays_black_and_white(self):
        image = Image.new("1", (8, 1), 255)
        pixels = image.load()
        for x in range(0, 8, 2):
            pixels[x, 0] = 0
        previous, current = frame_codec.pack_gray4(image, 8, 1)
        self.assertEqual(previous, [0x55])
        self.assertEqual(current, [0x55])

    def test_dark_square_is_flat_only_on_a_gray_canvas(self):
        # Why: the framebuffer is mode "L" only while the 4-gray glass is
        # driven, so the image being drawn is what says whether gray exists.
        # A regression reads some other state and paints flat 170 into a
        # 1-bit sprite (which stores it as white) or dithers on the gray glass.
        from universalchess.epaper.chess_board import fill_dither_dark

        dithered = Image.new("1", (8, 8), 255)
        fill_dither_dark(dithered, 0, 0, 8)
        self.assertEqual(set(dithered.getdata()), {0, 255})

        flat = Image.new("L", (8, 8), 255)
        fill_dither_dark(flat, 0, 0, 8)
        self.assertEqual(set(flat.getdata()), {170})

    def test_widget_sprite_takes_the_canvas_mode(self):
        # Why: a widget renders into a sprite, then pastes it onto the
        # framebuffer. The sprite must have the framebuffer's mode so a gray
        # background survives on the 4-gray glass and a 1-bit panel still gets
        # the dither. A regression creates the sprite from other state: on an
        # "L" canvas the background comes back as a {0, 255} dither, or on a
        # "1" canvas the flat shade is stored as white.
        from universalchess.epaper.framework.widget import Widget

        class _Shaded(Widget):
            def render(self, sprite):
                self.draw_background_on_sprite(sprite)

        cases = (("L", {170}), ("1", {0, 255}))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                widget = _Shaded(0, 0, 8, 8, MagicMock(), background_shade=8)
                canvas = Image.new(mode, (8, 8), 255)
                widget.draw_on(canvas, 0, 0)
                self.assertEqual(canvas.mode, mode)
                self.assertEqual(set(canvas.getdata()), expected)

    def test_refresh_loads_the_4g_lut_and_both_planes(self):
        epd = _i6fd_driver()
        epd.ReadBusy = MagicMock()
        image = Image.new("L", (128, 296), 170)
        plane = epd.getbuffer(image)
        trace = []
        payloads = []
        current_cmd = {"cmd": None, "data": []}

        def send_command(command):
            if current_cmd["cmd"] is not None:
                trace.append((current_cmd["cmd"], tuple(current_cmd["data"])))
            current_cmd["cmd"] = command
            current_cmd["data"] = []

        epd.send_command = send_command
        epd.send_data = lambda data: current_cmd["data"].append(data)
        epd.send_data2 = lambda data: payloads.append(list(data))
        # No prior frame, so this refresh is a gray-level change and must
        # load the published 4-gray tables. A regression is payloads[0][1]
        # == 0x10, the partial LUT, which cannot deposit a gray tone.
        epd.display(plane)
        if current_cmd["cmd"] is not None:
            trace.append((current_cmd["cmd"], tuple(current_cmd["data"])))

        self.assertIn((0x00, (0xBF,)), trace)
        self.assertIn((0x50, (0x17,)), trace)
        self.assertEqual(payloads[0][:6], [0x00, 0x0A, 0x00, 0x00, 0x00, 0x01])
        self.assertEqual(payloads[1][:6], [0x40, 0x0A, 0x00, 0x00, 0x00, 0x01])
        self.assertEqual(payloads[2][18:24], [0x99, 0x0C, 0x01, 0x03, 0x04, 0x01])
        self.assertEqual(len(payloads[0]), 44)
        self.assertEqual(len(payloads[1]), 42)
        nbytes = 128 * 296 // 8
        self.assertEqual(payloads[5], [0xFF] * nbytes)
        self.assertEqual(payloads[6], [0x00] * nbytes)
        commands = [command for command, _data in trace]
        self.assertIn(0x12, commands)
        self.assertEqual(
            trace[commands.index(0x90)][1],
            (0, 127, 0, 0, 1, 0x27, 1),
        )

    def test_stock_display_does_not_load_the_4g_lut(self):
        from universalchess.epaper.framework.waveshare import epd2in9d

        epd = epd2in9d.EPD()
        self.assertEqual(epd.gray_levels, 1)
        epd.ReadBusy = MagicMock()
        trace = _commands_of(epd, lambda: epd.display([0xFF] * (128 * 296 // 8)))
        commands = [command for command, _data in trace]
        self.assertNotIn(0x20, commands)
        self.assertNotIn(0xBF, [data for command, data in trace if command == 0x00])

    def test_clock_change_uses_the_partial_lut_and_a_gray_change_does_not(self):
        # Why: the 4-gray waveform is about 3 seconds, so a clock tick cannot
        # take it. A black/white change must load the I6FD partial LUT (phase
        # byte 0x10). A gray pixel that changes must load the 4-gray VCOM
        # (second byte 0x0A) or the partial paints that square solid white.
        # A regression is the clock tick at phase 0x0A, or the gray-square
        # change at phase 0x10.
        epd = _i6fd_driver()
        epd.ReadBusy = MagicMock()
        epdconfig.delay_ms = MagicMock()

        def run(action):
            trace = []
            payloads = []
            current_cmd = {"cmd": None, "data": []}

            def send_command(command):
                if current_cmd["cmd"] is not None:
                    trace.append((current_cmd["cmd"], tuple(current_cmd["data"])))
                current_cmd["cmd"] = command
                current_cmd["data"] = []

            epd.send_command = send_command
            epd.send_data = lambda data: current_cmd["data"].append(data)
            epd.send_data2 = lambda data: payloads.append(list(data))
            action()
            if current_cmd["cmd"] is not None:
                trace.append((current_cmd["cmd"], tuple(current_cmd["data"])))
            return trace, payloads

        base = Image.new("L", (128, 296), 170)
        base.putpixel((0, 0), 0)
        _trace, _payloads = run(lambda: epd.display(epd.getbuffer(base)))

        clock = base.copy()
        clock.putpixel((0, 0), 255)
        _trace, payloads = run(lambda: epd.DisplayPartial(epd.getbuffer(clock)))
        # Partial VCOM phase is 0x10. The 4-gray VCOM phase is 0x0A.
        self.assertEqual(payloads[0][1], 0x10)
        self.assertEqual(payloads[5][0], 0x7F)
        self.assertEqual(payloads[6][0], 0xFF)
        self.assertEqual(payloads[5][1], 0xFF)
        self.assertEqual(payloads[6][1], 0xFF)

        moved = clock.copy()
        moved.putpixel((8, 0), 0)
        _trace, payloads = run(lambda: epd.DisplayPartial(epd.getbuffer(moved)))
        self.assertEqual(payloads[0][1], 0x0A)

    def _gray_epd(self, partial_only=False):
        model = panels.get_model("gdew029i6fd")
        epd = display_boot.build_panel_epd(model, model.key, False, False, partial_only)
        epd.ReadBusy = MagicMock()
        epdconfig.delay_ms = MagicMock()
        return epd

    @staticmethod
    def _gray_frame_with_black_pixel():
        frame = Image.new("L", (128, 296), 170)
        frame.putpixel((0, 0), 0)
        return frame

    def test_full_refresh_after_a_black_and_white_change_keeps_the_gray(self):
        # Why: the scheduler asks for a full refresh on every screen change,
        # including one where only black and white moved. On this glass a
        # full refresh must be the 4-gray waveform with both gray planes. The
        # 1-bit full path writes 0x00 to 0x10: under the 4-gray tables that
        # reads as dark gray or black, and under the OTP table every gray
        # pixel is driven to black or white.
        # A regression is the first payload being the 1-bit old plane (second
        # byte 0x00) instead of the 4-gray VCOM LUT (0x0A).
        epd = self._gray_epd()
        base = self._gray_frame_with_black_pixel()
        _payloads_of(epd, lambda: epd.display(epd.getbuffer(base)))
        clock = base.copy()
        clock.putpixel((0, 0), 255)

        payloads = _payloads_of(epd, lambda: epd.display(epd.getbuffer(clock)))

        previous, current = frame_codec.pack_gray4(clock, 128, 296)
        self.assertEqual(payloads[0][1], GRAY4_VCOM_PHASE)
        self.assertEqual(payloads[OLD_PLANE], previous)
        self.assertEqual(payloads[NEW_PLANE], current)

    def test_a_frame_that_was_never_shown_is_not_the_partial_baseline(self):
        # Why: a frame can be packed and then not drawn (shutdown, a failed
        # refresh). The next partial must compare with the frame on the glass.
        # Here the unshown frame turned a gray pixel black, and the next frame
        # only changes the clock pixel relative to it. Relative to the glass,
        # that gray pixel still has to change.
        # A regression compares with the unshown frame, sees a black/white
        # change only, and loads the partial LUT (0x10), so the gray pixel is
        # never redrawn.
        epd = self._gray_epd()
        base = self._gray_frame_with_black_pixel()
        _payloads_of(epd, lambda: epd.display(epd.getbuffer(base)))
        unshown = base.copy()
        unshown.putpixel((8, 0), 0)
        epd.getbuffer(unshown)
        clock = unshown.copy()
        clock.putpixel((0, 0), 255)

        payloads = _payloads_of(epd, lambda: epd.DisplayPartial(epd.getbuffer(clock)))

        self.assertEqual(payloads[0][1], GRAY4_VCOM_PHASE)

    def test_partial_only_clear_drives_every_pixel_to_white(self):
        # Why: the scheduler's cold start clears once so the driver's partial
        # baseline matches the glass. With Partial refresh only on, the clear
        # is a partial. With white as both the old and the new plane the
        # partial LUT drives no pixel, and the glass keeps whatever it showed
        # while the driver assumes white.
        # A regression is an old plane (0x10) of 0xFF bytes, equal to the new
        # plane.
        epd = self._gray_epd(partial_only=True)

        payloads = _payloads_of(epd, epd.Clear)

        self.assertEqual(payloads[0][1], PARTIAL_VCOM_PHASE)
        self.assertEqual(payloads[OLD_PLANE], [0x00] * LOGICAL_BYTES)
        self.assertEqual(payloads[NEW_PLANE], [0xFF] * LOGICAL_BYTES)
        self.assertEqual(epd.buffer, [0xFF] * LOGICAL_BYTES)


class PanelWaveformChoiceTests(unittest.TestCase):
    """A replacement panel's waveform setting survives a reboot.

    Why this exists: selecting a panel pins its waveform, and the waveform
    dropdown can change it afterwards on a panel that has one (GDEY029T94,
    GDEY029Z95). The boot builder used the pinned key unconditionally, so a
    chosen waveform ran until the next boot and was then replaced.

    How a regression manifests: the built driver's profile is the pinned
    ``gdey029t94`` or ``gdey029z95`` when the stored key is a Centaur SSD16xx
    waveform.
    """

    def test_panel_choice_resolves_the_stored_waveform(self):
        cases = (
            ("gdey029t94", "gdem029t94", "gdem029t94"),
            ("gdey029t94", "uc8151d_waveshare", "gdey029t94"),
            ("gdey029t94", "", "gdey029t94"),
            ("gdew029i6fd", "uc8151d_t5d", "uc8151d_gdew029i6fd"),
            ("gdey029z95", "gdem029t94", "gdem029t94"),
            ("gdey029z95", "uc8151d_waveshare", "gdey029z95"),
            # The Z95 tri-color recipe is SSD16xx too, but it is not T94's.
            ("gdey029t94", "gdey029z95", "gdey029t94"),
        )
        for panel_key, stored_key, expected in cases:
            with self.subTest(panel=panel_key, stored=stored_key):
                epd = display_boot.build_panel_epd(
                    panels.get_model(panel_key), stored_key, False, False,
                )
                self.assertEqual(epd.profile.key, expected)

    def test_automatic_probe_ignores_a_replacement_panel_recipe(self):
        # Why: after returning to Automatic the stored key can still be a
        # replacement panel's pinned recipe. How a regression manifests: the
        # Centaur V1 driver is built with gdey029t94 or gdey029z95 instead of
        # the family default gdem029t94.
        for stored_key in ("gdey029t94", "gdey029z95"):
            with self.subTest(stored=stored_key):
                _epd, profile = display_boot.build_epd(
                    panels.CONTROLLER_SSD1680, stored_key, False, False,
                )
                self.assertEqual(profile.key, "gdem029t94")


# JD7966x power-on command, the last step of each four-color init.
POWER_ON = 0x04


class FourColorRefreshTests(unittest.TestCase):
    """Each four-color frame is preceded by the panel's register block.

    Why this exists: GxEPD2 ``GxEPD2_290c_GDEY029F51H::_refresh`` clears
    ``_init_display_done`` after every refresh ("needed"), so the next frame
    re-sends the ``_InitDisplay`` register block. That block ends in
    ``_PowerOn()``, which sends 0x04 only while the power is off, and
    ``_refresh`` leaves the power on. Neither resets the controller between
    frames that follow each other.

    How a regression manifests: the frame after init() starts with 0x10 (the
    data write) and no PSR 0x00 / TRES 0x61 before it, a second power-on 0x04
    is sent while the power is on, or a reset appears between the frames.
    """

    def test_every_frame_re_sends_the_register_block_without_a_reset(self):
        for spec in (jd.f51h_spec(), jd.f52_spec()):
            with self.subTest(panel=spec.key):
                epd = jd.EPD(spec)
                epdconfig_module_init = epdconfig.module_init
                epdconfig.module_init = MagicMock(return_value=0)
                try:
                    trace = _commands_of(epd, lambda: (
                        epd.init(),
                        epd.display([0xFF] * LOGICAL_BYTES),
                        epd.display([0x00] * LOGICAL_BYTES),
                    ))
                finally:
                    epdconfig.module_init = epdconfig_module_init
                register_block = [
                    command for phase in spec.phases for command, _data in phase
                ]
                without_power_on = [c for c in register_block if c != POWER_ON]
                commands = [command for command, _data in trace]
                data_writes = [i for i, command in enumerate(commands) if command == 0x10]
                self.assertEqual(len(data_writes), 2)
                init_part = commands[:len(register_block)]
                first_frame = commands[len(register_block):data_writes[0]]
                second_frame = commands[data_writes[0] + 1:data_writes[1]]
                self.assertEqual(init_part, register_block)
                self.assertEqual(first_frame, [])
                refresh = [command for command, _data in spec.refresh_pairs]
                self.assertEqual(second_frame, refresh + without_power_on)
                self.assertEqual(epd.reset.call_count, 1)


# Attributes the scheduler, manager and board read from whichever driver is live.
DRIVER_CAPABILITIES = (
    "PANEL_KEY", "supports_partial", "gray_levels",
    "partial_only_supported", "partial_only",
)


class DriverCapabilityTests(unittest.TestCase):
    """Every driver states its panel's capabilities from its constructor.

    Why this exists: the capabilities were attributes set on the driver after
    construction, and the scheduler, manager and board read them with
    ``getattr`` defaults. A driver built by the Automatic probe had no
    ``PANEL_KEY`` or ``supports_partial`` at all, so the defaults stood in for
    them, and a driver built directly for GDEW029I6FD kept 1-bit until a
    caller set ``gray_levels`` by hand.

    How a regression manifests: an attribute is missing on the Automatic
    drivers, or a driver's value differs from its panel model.
    """

    def test_automatic_drivers_report_the_stock_panel(self):
        for controller in (panels.CONTROLLER_UC8151D, panels.CONTROLLER_SSD1680):
            with self.subTest(controller=controller):
                epd, _profile = display_boot.build_epd(controller, "", False, False)
                self.assertEqual(
                    {name: getattr(epd, name) for name in DRIVER_CAPABILITIES},
                    {"PANEL_KEY": panels.PANEL_AUTO, "supports_partial": True,
                     "gray_levels": 1, "partial_only_supported": False,
                     "partial_only": False},
                )

    def test_replacement_drivers_report_their_panel_model(self):
        for model in panels.all_panels():
            if not model.explicit:
                continue
            with self.subTest(panel=model.key):
                epd = display_boot.build_panel_epd(model, "", False, False)
                self.assertEqual(
                    {name: getattr(epd, name) for name in DRIVER_CAPABILITIES},
                    {"PANEL_KEY": model.key, "supports_partial": model.supports_partial,
                     "gray_levels": model.gray_levels,
                     "partial_only_supported": model.partial_only_supported,
                     "partial_only": False},
                )

    def test_i6fd_driver_restores_its_gray_after_partial_only(self):
        epd = _i6fd_driver()
        self.assertEqual(epd.gray_levels, 4)
        epd.apply_partial_only(True)
        self.assertEqual(epd.gray_levels, 1)
        epd.apply_partial_only(False)
        self.assertEqual(epd.gray_levels, 4)


# Panels whose glass has red ink. The Centaur V1/V2 glass, GDEY029T94,
# GDEW029I6FD and GDEY029T71H are black and white.
RED_INK_PANELS = frozenset({"gdey029z95", "gdey029f51h", "gdey029f52"})


class ThreeColorScopeTests(unittest.TestCase):
    """Three-color mode reaches only a panel with red ink.

    Why this exists: the switch was offered for Automatic and GDEY029T94,
    whose glass is black and white. Turning it on there sent a red plane the
    glass cannot show and ran the slow tri-color full refresh for nothing.

    How a regression manifests: a black/white panel reports
    three_color_supported, or its driver is built with three_color True
    while the stored switch is on.
    """

    def test_only_red_ink_panels_support_three_color(self):
        for model in panels.all_panels():
            with self.subTest(panel=model.key):
                self.assertEqual(model.three_color_supported, model.key in RED_INK_PANELS)

    def test_stored_switch_reaches_only_a_red_ink_driver(self):
        for controller in (panels.CONTROLLER_UC8151D, panels.CONTROLLER_SSD1680):
            with self.subTest(panel="auto", controller=controller):
                epd, _profile = display_boot.build_epd(controller, "", False, True)
                self.assertFalse(epd.three_color)
        for model in panels.all_panels():
            if not model.explicit:
                continue
            with self.subTest(panel=model.key):
                epd = display_boot.build_panel_epd(model, "", False, True)
                self.assertEqual(epd.three_color, model.key in RED_INK_PANELS)


class ExplicitPanelProbeLogTests(unittest.TestCase):
    """The diagnostics line names only the panel that was tried.

    Why this exists: an explicit panel is one init against one glass. The
    boot log was built from an invented two-controller order with the panel's
    controller in both places, so a BUSY timeout read "GDEY029T94: busy;
    GDEY029T94: not tried".

    How a regression manifests: the message names the panel twice, says
    "not tried", or names a Centaur controller.
    """

    def test_log_line_names_the_selected_panel_once(self):
        from universalchess.board import display_selection as ds

        model = panels.get_model("gdey029t94")
        failed = ds.DisplayOutcome(
            initialized=False, busy_timeout=True, active_controller=None, error="busy",
        )
        ok = ds.DisplayOutcome(
            initialized=True, busy_timeout=False,
            active_controller=model.controller, error=None,
        )
        cases = (
            (failed, DisplayAttempt(ok=False, busy_timeout=True, error="busy"),
             ("error", "E-paper init failed (GDEY029T94 (SSD1680)): busy")),
            (ok, DisplayAttempt(ok=True),
             ("info", "E-paper panel detected: GDEY029T94 (SSD1680)")),
        )
        for outcome, attempt, expected in cases:
            with self.subTest(initialized=outcome.initialized):
                self.assertEqual(
                    ds.explicit_panel_log_entry(outcome, attempt, model.event_label),
                    expected,
                )
