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

    def test_il3820_profile_requests_additions(self):
        # The IL3820 profile must request the analog additions on top of register
        # LUTs. Regression: il3820_additions False would silently drop the
        # IL3820-specific init the profile exists to apply.
        profile = wp.get_profile("il3820_gdeh029a1")
        self.assertTrue(profile.il3820_additions)
        self.assertFalse(profile.use_otp)

    def test_every_profile_has_provenance(self):
        # Enforces the "no fabricated/unattributed waveforms" rule: each profile
        # must name a source. A blank source means a table shipped without a
        # credited origin.
        for profile in wp.all_profiles():
            with self.subTest(key=profile.key):
                self.assertTrue(profile.source.strip(),
                                f"profile {profile.key} has no source attribution")

    def test_register_luts_are_correct_length(self):
        # The 0x32 write expects 153 LUT bytes + 6 voltage bytes. A wrong length
        # corrupts the waveform write (SetLut indexes [153]..[158]).
        for profile in wp.all_profiles():
            if profile.use_otp:
                continue
            with self.subTest(key=profile.key):
                self.assertEqual(len(profile.full_lut), EXPECTED_LUT_LEN)
                self.assertEqual(len(profile.partial_lut), EXPECTED_LUT_LEN)

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
