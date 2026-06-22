"""Tests for the SSD1680 waveform-profile registry.

Why these tests exist:
    The SSD1680 driver selects a named, attributed waveform profile at runtime.
    These pin the registry's contracts that the driver and web API depend on:
    (a) the no-config default reproduces the prior GDEM029T94 behavior so the
    working bench panel is never silently changed, (b) an unknown/blank key
    falls back to that default rather than leaving the panel with no waveform,
    (c) every shipped profile carries provenance (no fabricated, unattributed
    tables), and (d) register LUTs are the correct length for the 0x32 write.

How a regression manifests:
    - Default drift: the default key/LUT changes, so an unconfigured board
      renders with a different (possibly wrong) waveform than before.
    - Fallback regression: get_profile(bad_key) raises or returns OTP, blanking
      a panel that should have rendered with the default table.
    - Provenance gap: a profile ships with an empty source -- the exact thing the
      "no fabricated waveforms" rule forbids.
"""

import unittest

from universalchess.epaper.framework.waveshare import waveform_profiles as wp

# A register LUT is 153 LUT bytes written via 0x32 plus 6 trailing voltage bytes.
EXPECTED_LUT_LEN = 159


class WaveformProfileRegistryTests(unittest.TestCase):
    def test_default_is_gdem029t94_with_register_luts(self):
        # The no-config default must be the GDEM029T94 register-LUT profile, which
        # is byte-identical to the prior hard-coded driver behavior. If this drifts
        # to OTP or another table, an unconfigured bench panel changes silently.
        profile = wp.get_profile("")
        self.assertEqual(profile.key, wp.DEFAULT_PROFILE_KEY)
        self.assertEqual(profile.key, "gdem029t94")
        self.assertFalse(profile.use_otp)
        self.assertEqual(profile.full_lut, wp.WS_20_30)
        self.assertEqual(profile.partial_lut, wp.WF_PARTIAL_2IN9)

    def test_unknown_key_falls_back_to_default(self):
        # A stale/mistyped stored key must resolve to the default, never raise and
        # never yield an empty waveform. Regression: KeyError or a use_otp profile
        # that would blank a panel expecting the register LUT.
        profile = wp.get_profile("does-not-exist")
        self.assertEqual(profile.key, wp.DEFAULT_PROFILE_KEY)

    def test_builtin_otp_profile_carries_no_register_lut(self):
        # The Built-In option must signal OTP and carry no register tables, so the
        # driver takes the OTP path. Regression: use_otp False or LUT bytes present
        # would make the driver write a register LUT in "use the panel's own" mode.
        profile = wp.get_profile("builtin_otp")
        self.assertTrue(profile.use_otp)
        self.assertEqual(profile.full_lut, ())
        self.assertEqual(profile.partial_lut, ())

    def test_il3820_profile_uses_il3820_driver_with_30_byte_luts(self):
        # The IL3820 profile must select the IL3820 driver strategy and carry the
        # 30-byte IL3820 LUTs (NOT the SSD1680 159-byte tables). Regression: a
        # wrong driver/length would drive a true IL3820 panel with the SSD1680
        # protocol -- the mislabeled-hybrid bug this profile replaced.
        profile = wp.get_profile("il3820_gdeh029a1")
        self.assertEqual(profile.driver, wp.DRIVER_IL3820)
        self.assertEqual(len(profile.full_lut), 30)
        self.assertEqual(len(profile.partial_lut), 30)
        self.assertFalse(profile.use_otp)

    def test_depg0290bs_profile_uses_dke_driver_otp_full_register_partial(self):
        # The DEPG0290BS profile must select the DKE/SSD1680 driver, carry NO full
        # LUT (full is driven from OTP) and a 153-byte register partial LUT (no
        # trailing voltage bytes). Regression: a full LUT present or wrong partial
        # length would not match the GxEPD2_290_BS sequence this transcribes.
        profile = wp.get_profile("depg0290bs")
        self.assertEqual(profile.driver, wp.DRIVER_DKE_SSD1680)
        self.assertEqual(profile.full_lut, ())
        self.assertEqual(len(profile.partial_lut), 153)

    def test_every_profile_has_provenance(self):
        # Enforces the "no fabricated/unattributed waveforms" rule: each profile
        # must name a source. A blank source means a table shipped without a
        # credited origin.
        for profile in wp.all_profiles():
            with self.subTest(key=profile.key):
                self.assertTrue(profile.source.strip(),
                                f"profile {profile.key} has no source attribution")

    def test_register_luts_are_correct_length_for_each_driver(self):
        # Each driver has a fixed LUT format; a wrong length corrupts the 0x32
        # write. SSD1680 SetLut() indexes [0..152] + voltage [153..158] (159);
        # IL3820 writes 30 raw bytes; DEPG0290BS writes a 153-byte raw partial
        # LUT and drives full from OTP (no full LUT). UC8151D carries no SSD16xx
        # LUTs at all -- its tables live in the uc8151d field (checked below).
        expected = {
            wp.DRIVER_SSD1680: {"full": EXPECTED_LUT_LEN, "partial": EXPECTED_LUT_LEN},
            wp.DRIVER_IL3820: {"full": 30, "partial": 30},
            wp.DRIVER_DKE_SSD1680: {"full": 0, "partial": 153},
            wp.DRIVER_UC8151D: {"full": 0, "partial": 0},
        }
        for profile in wp.all_profiles():
            if profile.use_otp:
                continue
            with self.subTest(key=profile.key):
                exp = expected[profile.driver]
                self.assertEqual(len(profile.full_lut), exp["full"])
                self.assertEqual(len(profile.partial_lut), exp["partial"])

    def test_metadata_excludes_waveform_bytes(self):
        # The web API payload must expose only key/label/source/url/controller --
        # never the raw LUT bytes. Regression: leaking byte arrays bloats the
        # response and couples the UI to panel internals.
        meta = wp.profiles_metadata()
        self.assertEqual(len(meta), len(wp.all_profiles()))
        for entry in meta:
            self.assertEqual(set(entry.keys()),
                             {"key", "label", "source", "url", "controller"})


class Uc8151dProfileTests(unittest.TestCase):
    """UC8151D (V2) profiles: full is OTP for all; only the partial set varies.

    Why these exist: a replacement UC8151D variant can pass the primary driver's
    BUSY check yet ghost/render faint, so the driver must be able to load a
    different partial register-LUT set. These pin (a) the per-controller default
    reproduces the stock Waveshare partial byte-for-byte, (b) profiles are
    cleanly partitioned by controller so the UI never offers a UC8151D table for
    an SSD1680 panel, and (c) every UC8151D LUT is the exact register length
    (VCOM 44, channels 42) the controller expects.
    """

    # The stock Waveshare epd2in9d.py partial VCOM LUT: phase byte 0x19 (25).
    STOCK_VCOM = (0x00, 0x19, 0x01, 0x00, 0x00, 0x01) + (0x00,) * 38

    def test_uc8151d_default_matches_stock_waveshare_partial(self):
        # The no-config UC8151D default must reproduce the prior hard-coded
        # epd2in9d partial exactly. Regression: a changed phase byte or LUT shape
        # silently alters how every working V2 panel does partial refreshes.
        profile = wp.get_profile("", wp.CONTROLLER_UC8151D)
        self.assertEqual(profile.key,
                         wp.DEFAULT_PROFILE_KEY_BY_CONTROLLER[wp.CONTROLLER_UC8151D])
        self.assertEqual(profile.controller, wp.CONTROLLER_UC8151D)
        self.assertEqual(profile.driver, wp.DRIVER_UC8151D)
        wf = profile.uc8151d
        self.assertIsNotNone(wf)
        self.assertEqual(wf.vcom, self.STOCK_VCOM)
        # Stock analog bytes: VCOM_DC 0x12, interval 0x97, PLL 0x3a.
        self.assertEqual((wf.vcom_dc, wf.interval, wf.pll), (0x12, 0x97, 0x3a))

    def test_i6fd_and_t5d_differ_only_in_phase_and_skip_pll(self):
        # I6FD (faster, phase 0x10) and T5D (longer, phase 0x20) keep the stock
        # shape but change the phase byte, and -- matching GxEPD2 -- leave PLL at
        # the controller default (pll None). Regression: wrong phase or a spurious
        # PLL write would diverge from the transcribed GxEPD2 sequence.
        i6fd = wp.get_profile("uc8151d_gdew029i6fd", wp.CONTROLLER_UC8151D)
        t5d = wp.get_profile("uc8151d_t5d", wp.CONTROLLER_UC8151D)
        self.assertEqual(i6fd.uc8151d.vcom[1], 0x10)
        self.assertEqual(t5d.uc8151d.vcom[1], 0x20)
        self.assertIsNone(i6fd.uc8151d.pll)
        self.assertIsNone(t5d.uc8151d.pll)
        # Variant analog bytes from GxEPD2: VCOM_DC 0x08, interval 0x17.
        self.assertEqual((i6fd.uc8151d.vcom_dc, i6fd.uc8151d.interval), (0x08, 0x17))

    def test_m06_is_labelled_experimental(self):
        # The author marks the M06 balanced-charge LUTs experimental; the UI must
        # say so. Regression: dropping the label hides that this table is unproven.
        profile = wp.get_profile("uc8151d_gdew029m06", wp.CONTROLLER_UC8151D)
        self.assertIn("experimental", profile.label.lower())
        self.assertEqual(profile.uc8151d.pll, 0x3c)

    def test_uc8151d_lut_lengths(self):
        # Every UC8151D profile must carry a 44-byte VCOM LUT and four 42-byte
        # channel LUTs -- the fixed register lengths the controller latches.
        # Regression: a short/long LUT shifts the latched waveform and corrupts
        # the partial refresh.
        for profile in wp.all_profiles(wp.CONTROLLER_UC8151D):
            wf = profile.uc8151d
            with self.subTest(key=profile.key):
                self.assertEqual(len(wf.vcom), 44)
                for channel in (wf.ww, wf.bw, wf.wb, wf.bb):
                    self.assertEqual(len(channel), 42)

    def test_profiles_partition_cleanly_by_controller(self):
        # Filtering by controller must return only that family and the two
        # families must together account for every profile (no profile is
        # untagged or double-counted). Regression: a mis-tagged profile would be
        # offered for the wrong panel and rejected/ignored by its driver.
        ssd = wp.all_profiles(wp.CONTROLLER_SSD16XX)
        uc = wp.all_profiles(wp.CONTROLLER_UC8151D)
        self.assertTrue(all(p.controller == wp.CONTROLLER_SSD16XX for p in ssd))
        self.assertTrue(all(p.controller == wp.CONTROLLER_UC8151D for p in uc))
        self.assertEqual(len(ssd) + len(uc), len(wp.all_profiles()))
        self.assertTrue(uc, "no UC8151D profiles registered")

    def test_get_profile_falls_back_across_controller_mismatch(self):
        # A key belonging to the OTHER controller (e.g. after a panel swap) must
        # resolve to the requested controller's default, never return a profile
        # the live driver cannot drive. Regression: returning the mismatched
        # profile would feed UC8151D bytes to the SSD1680 driver or vice versa.
        self.assertEqual(
            wp.get_profile("uc8151d_t5d", wp.CONTROLLER_SSD16XX).key,
            wp.DEFAULT_PROFILE_KEY_BY_CONTROLLER[wp.CONTROLLER_SSD16XX])
        self.assertEqual(
            wp.get_profile("gdem029t94", wp.CONTROLLER_UC8151D).key,
            wp.DEFAULT_PROFILE_KEY_BY_CONTROLLER[wp.CONTROLLER_UC8151D])

    def test_is_known_profile_respects_controller(self):
        # Web input validation must reject a known key that targets the wrong
        # controller, so the UI cannot persist a UC8151D selection for an SSD1680
        # panel. Regression: a cross-controller key would pass validation and be
        # stored, then silently fall back at apply time.
        self.assertTrue(wp.is_known_profile("uc8151d_t5d", wp.CONTROLLER_UC8151D))
        self.assertFalse(wp.is_known_profile("uc8151d_t5d", wp.CONTROLLER_SSD16XX))
        self.assertTrue(wp.is_known_profile("uc8151d_t5d"))  # no controller: any


if __name__ == "__main__":
    unittest.main()
