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
        # LUT and drives full from OTP (no full LUT).
        expected = {
            wp.DRIVER_SSD1680: {"full": EXPECTED_LUT_LEN, "partial": EXPECTED_LUT_LEN},
            wp.DRIVER_IL3820: {"full": 30, "partial": 30},
            wp.DRIVER_DKE_SSD1680: {"full": 0, "partial": 153},
        }
        for profile in wp.all_profiles():
            if profile.use_otp:
                continue
            with self.subTest(key=profile.key):
                exp = expected[profile.driver]
                self.assertEqual(len(profile.full_lut), exp["full"])
                self.assertEqual(len(profile.partial_lut), exp["partial"])

    def test_metadata_excludes_waveform_bytes(self):
        # The web API payload must expose only key/label/source/url -- never the
        # raw LUT bytes. Regression: leaking byte arrays bloats the response and
        # couples the UI to panel internals.
        meta = wp.profiles_metadata()
        self.assertEqual(len(meta), len(wp.all_profiles()))
        for entry in meta:
            self.assertEqual(set(entry.keys()), {"key", "label", "source", "url"})


if __name__ == "__main__":
    unittest.main()
