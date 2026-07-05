"""Tests for board.hardware_info: chip parsing, version parsing, and the
Bluetooth advertising health classifier.

Why these tests exist:
  The System card's Bluetooth advertising verdict is derived from the Broadcom
  chip stepping plus the kernel version. The field investigation that motivated
  this module proved exactly three data points (BCM43430B0's BlueZ LE
  advertising broken on 6.18, fine on 6.12; BCM43430A1 fine). These tests pin
  the parser to real kernel-log shapes
  and pin the classifier to those proven points -- and, just as importantly, to
  return "unknown" (never a guess) for combinations that were never observed.

How a regression manifests:
  - If the chip regex loses the stepping preference, ``parse_wireless_chip``
    returns "BCM43430" instead of "BCM43430B0" and the affected board would be
    mislabeled "no known issue".
  - If the classifier widens its "ok"/"affected" rules, an unverified
    chip+kernel pair would assert a verdict the evidence does not support.
"""

import unittest

from universalchess.board import hardware_info as hw
from universalchess.managers import bluez_patch_status


# Real kernel-log fragments (trimmed) from the two boards in the investigation.
# The B0 board's BT init line carries the stepping; the brcmfmac line carries
# only the bare family -- the parser must prefer the stepping line.
_LOG_B0 = (
    "Bluetooth: hci0: BCM: chip id 115\n"
    "Bluetooth: hci0: BCM43430B0\n"
    "brcmfmac: brcmf_fw_alloc_request: using brcm/brcmfmac43430-sdio.bin for "
    "chip BCM43430/2\n"
)
_LOG_A1 = (
    "Bluetooth: hci0: BCM: chip id 94\n"
    "Bluetooth: hci0: BCM43430A1\n"
)
# A log where only the brcmfmac family line is present (no BT stepping line).
_LOG_BARE_ONLY = (
    "brcmfmac: brcmf_c_preinit_dcmds: Firmware: BCM43430/2 wl0\n"
)

# A minimal two-stanza dpkg status snippet. The trailing "bluez-tools" stanza
# guards against a substring false-match when querying "bluez".
_DPKG_STATUS = (
    "Package: firmware-brcm80211\n"
    "Status: install ok installed\n"
    "Version: 1:20250410-1+rpt1\n"
    "\n"
    "Package: bluez\n"
    "Status: install ok installed\n"
    "Version: 5.82-1.1+rpt1\n"
    "\n"
    "Package: bluez-tools\n"
    "Status: install ok installed\n"
    "Version: 0.2.0-1\n"
)


class WirelessChipParseTests(unittest.TestCase):
    def test_prefers_stepping_over_bare_family(self):
        # Regression: the affected verdict depends on the A1/B0 stepping. If the
        # bare "BCM43430" matched first, the B0 board would be misclassified.
        self.assertEqual(hw.parse_wireless_chip(_LOG_B0), "BCM43430B0")

    def test_parses_older_a1_stepping(self):
        # The known-good chip must be reported with its stepping too.
        self.assertEqual(hw.parse_wireless_chip(_LOG_A1), "BCM43430A1")

    def test_falls_back_to_bare_family_when_no_stepping(self):
        # When only the brcmfmac family line exists, report the family rather
        # than nothing, so the card still shows a chip.
        self.assertEqual(hw.parse_wireless_chip(_LOG_BARE_ONLY), "BCM43430")

    def test_returns_none_when_absent(self):
        # No fabrication: an empty/irrelevant log yields None, which the
        # classifier turns into "unknown" rather than a guessed verdict.
        self.assertIsNone(hw.parse_wireless_chip(""))
        self.assertIsNone(hw.parse_wireless_chip("nothing wireless here"))


class DpkgVersionParseTests(unittest.TestCase):
    def test_reads_exact_package_version(self):
        # Guards the wifi-firmware row, a key part of the affected combination.
        self.assertEqual(
            hw.parse_dpkg_version(_DPKG_STATUS, "firmware-brcm80211"),
            "1:20250410-1+rpt1",
        )

    def test_exact_name_match_not_substring(self):
        # "bluez" must not match the "bluez-tools" stanza; a substring match
        # would return the wrong (0.2.0) version.
        self.assertEqual(hw.parse_dpkg_version(_DPKG_STATUS, "bluez"), "5.82-1.1+rpt1")

    def test_missing_package_returns_none(self):
        self.assertIsNone(hw.parse_dpkg_version(_DPKG_STATUS, "not-installed"))
        self.assertIsNone(hw.parse_dpkg_version("", "bluez"))


class KernelTupleParseTests(unittest.TestCase):
    def test_parses_major_minor(self):
        self.assertEqual(hw.parse_kernel_tuple("6.18.34+rpt-rpi-v7"), (6, 18))
        self.assertEqual(hw.parse_kernel_tuple("6.12.47+rpt-rpi-v7"), (6, 12))

    def test_unparseable_returns_none(self):
        # Drives the classifier to "unknown" instead of a mis-parse.
        self.assertIsNone(hw.parse_kernel_tuple(""))
        self.assertIsNone(hw.parse_kernel_tuple("weird-kernel"))


class WirelessHealthTests(unittest.TestCase):
    """Each case maps to a proven data point or an explicitly-unverified one."""

    def test_b0_on_618_is_affected(self):
        # The proven failure: B0 + kernel 6.18 breaks BlueZ LE advertising
        # (RegisterAdvertisement rejected). The summary must name the chip and
        # the known-good remedy (6.12.x), and stay scoped to Bluetooth -- it must
        # NOT claim a Wi-Fi hotspot failure, which was never observed.
        health, summary = hw.assess_wireless_health("BCM43430B0", "6.18.34+rpt-rpi-v7")
        self.assertEqual(health, hw.HEALTH_AFFECTED)
        self.assertIn("BCM43430B0", summary)
        self.assertIn("6.12", summary)  # names the known-good remedy
        self.assertIn("Bluetooth", summary)
        self.assertNotIn("Wi-Fi", summary)

    def test_b0_on_612_is_ok(self):
        # Same die, known-good kernel: this is the fix state, must read "ok".
        health, _ = hw.assess_wireless_health("BCM43430B0", "6.12.75+rpt-rpi-v7")
        self.assertEqual(health, hw.HEALTH_OK)

    def test_b0_on_intermediate_kernel_is_unknown(self):
        # 6.13-6.17 were never observed; assert no false "ok"/"affected".
        health, _ = hw.assess_wireless_health("BCM43430B0", "6.15.0+rpt-rpi-v7")
        self.assertEqual(health, hw.HEALTH_UNKNOWN)

    def test_a1_is_ok_regardless_of_kernel(self):
        # The older stepping had no reported fault on any observed kernel.
        health, _ = hw.assess_wireless_health("BCM43430A1", "6.18.34+rpt-rpi-v7")
        self.assertEqual(health, hw.HEALTH_OK)

    def test_unknown_chip_is_unknown(self):
        # No chip identified -> no verdict (not a silent "ok").
        health, _ = hw.assess_wireless_health(None, "6.18.34+rpt-rpi-v7")
        self.assertEqual(health, hw.HEALTH_UNKNOWN)

    def test_b0_with_unparseable_kernel_is_unknown(self):
        # B0 fitted but kernel unreadable -> cannot decide; must be "unknown".
        health, _ = hw.assess_wireless_health("BCM43430B0", "")
        self.assertEqual(health, hw.HEALTH_UNKNOWN)


class CollectHardwareInfoTests(unittest.TestCase):
    """End-to-end assembly through an injected (fake) source -- no Pi needed."""

    @staticmethod
    def _source(kernel_release, kernel_log, display_status=None, bluez_patch=None):
        # bluez_patch defaults to "no marker" (unknown) so the common cases do
        # not have to specify it; the stack-specific tests pass a real marker.
        return hw.HardwareInfoSource(
            pi_model=lambda: "Raspberry Pi Zero W Rev 1.1",
            kernel_release=lambda: kernel_release,
            kernel_log=lambda: kernel_log,
            dpkg_status=lambda: _DPKG_STATUS,
            display_status=lambda: display_status,
            bluez_patch=lambda: bluez_patch
            if bluez_patch is not None
            else bluez_patch_status.unknown_status(),
        )

    def test_affected_board_full_contract(self):
        # Verifies the whole to_dict shape for the affected board, so the React
        # card cannot read an undefined field and the verdict is wired through.
        # display_status None -> "unknown": the board has not reported yet.
        info = hw.collect_hardware_info(self._source("6.18.34+rpt-rpi-v7", _LOG_B0))
        self.assertEqual(
            info.to_dict(),
            {
                "pi_model": "Raspberry Pi Zero W Rev 1.1",
                "kernel_release": "6.18.34+rpt-rpi-v7",
                "wireless_chip": "BCM43430B0",
                "wifi_firmware_version": "1:20250410-1+rpt1",
                "bluez_version": "5.82-1.1+rpt1",
                # No marker supplied -> "unknown" (never a fabricated "stock").
                "bluez_stack": bluez_patch_status.STACK_UNKNOWN,
                "bluez_stack_summary": info.bluez_stack_summary,
                "hotspot_health": hw.HEALTH_AFFECTED,
                "hotspot_summary": info.hotspot_summary,
                "display_model": hw.DISPLAY_MODEL,
                "display_controller": hw.DISPLAY_CONTROLLER,
                "display_driver": hw.DISPLAY_DRIVER,
                "display_resolution": "128 x 296",
                "display_status": hw.DISPLAY_UNKNOWN,
                "display_detail": info.display_detail,
                "display_busy_timeout": False,
                "display_active_controller": None,
            },
        )
        self.assertEqual(info.hotspot_health, hw.HEALTH_AFFECTED)

    def test_patched_stack_wired_through_assembly(self):
        # The patched-BlueZ marker must reach the card's flat contract so the
        # System Information row can render the warning. If collect_hardware_info
        # drops the bluez_patch source, the row would fall back to "unknown" and
        # the warning would silently disappear (the exact bug this move must not
        # reintroduce elsewhere).
        marker = bluez_patch_status.make_status(
            active=bluez_patch_status.STACK_PATCHED,
            base_version="5.82-1.1+rpt1",
        )
        info = hw.collect_hardware_info(
            self._source("6.18.34+rpt-rpi-v7", _LOG_B0, bluez_patch=marker)
        )
        self.assertEqual(info.bluez_stack, bluez_patch_status.STACK_PATCHED)
        self.assertIn("5.82-1.1+rpt1", info.bluez_stack_summary)
        self.assertIn("bluez_stack", info.to_dict())
        self.assertIn("bluez_stack_summary", info.to_dict())

    def test_healthy_board_reports_ok(self):
        # The A1/older board assembles an "ok" verdict end-to-end.
        info = hw.collect_hardware_info(self._source("6.12.47+rpt-rpi-v7", _LOG_A1))
        self.assertEqual(info.wireless_chip, "BCM43430A1")
        self.assertEqual(info.hotspot_health, hw.HEALTH_OK)
        self.assertEqual(info.display_resolution, "128 x 296")

    def test_display_status_ok_wired_through(self):
        # A board that reported a successful panel init must surface "ok" so the
        # card shows the panel working.
        info = hw.collect_hardware_info(
            self._source("6.12.47+rpt-rpi-v7", _LOG_A1, {"initialized": True})
        )
        self.assertEqual(info.display_status, hw.DISPLAY_OK)

    def test_display_status_failed_carries_board_error(self):
        # The V1 / unresponsive-panel case: the board's error message must reach
        # the card so "Not responding" has a concrete cause, not a generic line.
        info = hw.collect_hardware_info(
            self._source(
                "6.12.47+rpt-rpi-v7",
                _LOG_A1,
                {"initialized": False, "error": "BUSY timeout after 5.0s"},
            )
        )
        self.assertEqual(info.display_status, hw.DISPLAY_FAILED)
        self.assertIn("BUSY timeout after 5.0s", info.display_detail)

    def test_busy_timeout_and_active_controller_wired_through(self):
        # The V1 recovery case: UC8151D timed out (busy_timeout True, gating the
        # web IL3820 opt-in) and the SSD1680 driver took over. Both must reach
        # the card; if busy_timeout is dropped the opt-in would never appear.
        info = hw.collect_hardware_info(
            self._source(
                "6.12.47+rpt-rpi-v7",
                _LOG_A1,
                {
                    "initialized": True,
                    "busy_timeout": True,
                    "active_controller": "SSD1680",
                },
            )
        )
        self.assertTrue(info.display_busy_timeout)
        self.assertEqual(info.display_active_controller, "SSD1680")
        # The card's "Display driver" row must reflect the controller that
        # actually drove the panel, not the configured UC8151D default --
        # otherwise the System card would claim epd2in9d/UC8151D/V2 on a V1 board
        # that is being driven by the SSD1680 fallback.
        self.assertEqual(info.display_controller, "SSD1680")
        self.assertEqual(info.display_driver, "epd2in9_ssd1680")
        self.assertEqual(info.display_model, hw.DISPLAY_MODEL_SSD1680)
        self.assertNotIn("V2", info.display_model)

    def test_busy_timeout_defaults_false_when_absent(self):
        # An older/partial record without the field must default to False (opt-in
        # hidden), never raise or default-True.
        info = hw.collect_hardware_info(
            self._source("6.12.47+rpt-rpi-v7", _LOG_A1, {"initialized": True})
        )
        self.assertFalse(info.display_busy_timeout)
        self.assertIsNone(info.display_active_controller)

    def test_uc8151d_active_reports_default_driver(self):
        # The healthy V2 path: UC8151D drove the panel, so the card shows the
        # default driver/controller. Guards against the resolver mapping the
        # default controller to the wrong driver module.
        info = hw.collect_hardware_info(
            self._source(
                "6.12.47+rpt-rpi-v7",
                _LOG_A1,
                {"initialized": True, "active_controller": "UC8151D"},
            )
        )
        self.assertEqual(info.display_controller, "UC8151D")
        self.assertEqual(info.display_driver, "epd2in9d")

    def test_unreported_controller_falls_back_to_default_driver(self):
        # No status record yet (board has not reported): the row must show the
        # configured default rather than a blank, so the card is never empty.
        info = hw.collect_hardware_info(self._source("6.12.47+rpt-rpi-v7", _LOG_A1))
        self.assertEqual(info.display_controller, hw.DISPLAY_CONTROLLER)
        self.assertEqual(info.display_driver, hw.DISPLAY_DRIVER)


class SummarizeBluezStackTests(unittest.TestCase):
    """Maps the install-time self-heal marker to the card's (stack, summary).

    Why this exists: the patched-BlueZ warning was moved off the board's
    Bluetooth menu and the web Connectivity card into the System Information
    card, which reads it from this classifier. The warning must appear only when
    the board actually runs a substituted binary, and must never falsely claim
    "stock" when the marker is absent (that would silently drop the warning).
    """

    def test_patched_marker_is_a_warning_with_base_and_reason(self):
        # The real device state: the self-heal installed a rebuilt bluetoothd.
        # Regression: if the summary loses the "security updates" clause the
        # operator would not learn the substituted binary forgoes distro
        # patches; if it loses the base version the row cannot name the build.
        marker = bluez_patch_status.make_status(
            active=bluez_patch_status.STACK_PATCHED,
            base_version="5.82-1.1+rpt1",
            reason="kernel 6.18 ext-adv-data length validation rejects stock BlueZ 5.82",
        )
        stack, summary = hw.summarize_bluez_stack(marker)
        self.assertEqual(stack, bluez_patch_status.STACK_PATCHED)
        self.assertIn("5.82-1.1+rpt1", summary)
        self.assertIn("security updates", summary)
        self.assertIn("kernel 6.18", summary)  # the marker's reason is appended

    def test_patched_without_base_version_still_warns(self):
        # A patched marker missing base_version must still classify as patched
        # (the warning is driven by "active", not by the optional base string).
        stack, summary = hw.summarize_bluez_stack(
            bluez_patch_status.make_status(active=bluez_patch_status.STACK_PATCHED)
        )
        self.assertEqual(stack, bluez_patch_status.STACK_PATCHED)
        self.assertNotIn("based on BlueZ", summary)

    def test_stock_marker_is_not_a_warning(self):
        # A confirmed stock binary is the healthy case: classified stock, and the
        # summary must not carry the patched-stack warning wording.
        stack, summary = hw.summarize_bluez_stack(bluez_patch_status.stock_status())
        self.assertEqual(stack, bluez_patch_status.STACK_STOCK)
        self.assertNotIn("security updates", summary)

    def test_missing_marker_is_unknown_not_stock(self):
        # No marker (self-heal never ran on this image) must degrade to
        # "unknown", NOT "stock": asserting stock would hide a real patch and is
        # a fabricated value the marker never confirmed.
        for absent in (None, {}, [], "corrupt"):
            stack, _ = hw.summarize_bluez_stack(absent)
            self.assertEqual(stack, bluez_patch_status.STACK_UNKNOWN)


class ResolveActiveDisplayTests(unittest.TestCase):
    """Maps the board-reported active controller to (controller, driver, model)."""

    def test_ssd1680_maps_to_v1_driver_and_model(self):
        # The V1 fallback: SSD1680 must resolve to its own driver module AND the
        # V1 panel model, so the card stops claiming the UC8151D/epd2in9d/V2
        # defaults on a panel the SSD1680 driver is actually driving.
        self.assertEqual(
            hw.resolve_active_display("SSD1680"),
            ("SSD1680", "epd2in9_ssd1680", hw.DISPLAY_MODEL_SSD1680),
        )

    def test_uc8151d_maps_to_default_driver_and_model(self):
        self.assertEqual(
            hw.resolve_active_display("UC8151D"),
            (hw.DISPLAY_CONTROLLER, hw.DISPLAY_DRIVER, hw.DISPLAY_MODEL),
        )

    def test_none_falls_back_to_default(self):
        # Board has not reported yet -> show the configured default, not a blank.
        self.assertEqual(
            hw.resolve_active_display(None),
            (hw.DISPLAY_CONTROLLER, hw.DISPLAY_DRIVER, hw.DISPLAY_MODEL),
        )

    def test_unknown_controller_falls_back_to_default(self):
        # An unrecognized controller string must not produce a fabricated driver
        # or model; fall back to the known default instead.
        self.assertEqual(
            hw.resolve_active_display("MYSTERY1234"),
            (hw.DISPLAY_CONTROLLER, hw.DISPLAY_DRIVER, hw.DISPLAY_MODEL),
        )

    def test_v1_and_v2_models_differ(self):
        # Guards the actual user-visible fix: the V1 panel must not be labeled a
        # V2 panel. If the two model strings ever collapse, the card would again
        # mislabel an SSD1680 panel as "DGT Centaur V2 panel".
        self.assertNotEqual(hw.DISPLAY_MODEL_SSD1680, hw.DISPLAY_MODEL)
        self.assertIn("V1", hw.DISPLAY_MODEL_SSD1680)
        self.assertIn("V2", hw.DISPLAY_MODEL)


class DeriveDisplayStatusTests(unittest.TestCase):
    """Maps the board-written record to the closed (status, detail) union."""

    def test_none_record_is_unknown(self):
        # No record -> the board has not reported; the card must not claim "ok".
        status, _ = hw.derive_display_status(None)
        self.assertEqual(status, hw.DISPLAY_UNKNOWN)

    def test_initialized_true_is_ok(self):
        status, _ = hw.derive_display_status({"initialized": True})
        self.assertEqual(status, hw.DISPLAY_OK)

    def test_initialized_false_is_failed(self):
        # The simulated/real broken panel: not initialized -> "failed".
        status, detail = hw.derive_display_status({"initialized": False, "error": ""})
        self.assertEqual(status, hw.DISPLAY_FAILED)
        # Falls back to a default cause when the board gave no error string.
        self.assertIn("BUSY", detail)


class DisplayStatusFileRoundTripTests(unittest.TestCase):
    """write_display_status -> read_display_status across the on-disk boundary,
    the actual cross-process channel between the board and web processes."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original = hw.DISPLAY_STATUS_FILE
        hw.DISPLAY_STATUS_FILE = f"{self._tmp.name}/display_status.json"
        self.addCleanup(self._restore)

    def _restore(self):
        hw.DISPLAY_STATUS_FILE = self._original

    def test_missing_file_reads_as_none(self):
        # Before the board writes, the web side must read None (-> "unknown"),
        # never a stale or fabricated success.
        self.assertIsNone(hw.read_display_status())

    def test_write_failure_then_read_round_trips(self):
        # The exact broken-panel path: board writes initialized=False + error,
        # web reads it back intact and derives "failed".
        hw.write_display_status(initialized=False, error="BUSY timeout after 5.0s")
        record = hw.read_display_status()
        self.assertIsNotNone(record)
        self.assertFalse(record["initialized"])
        self.assertEqual(record["error"], "BUSY timeout after 5.0s")
        status, detail = hw.derive_display_status(record)
        self.assertEqual(status, hw.DISPLAY_FAILED)
        self.assertIn("BUSY timeout after 5.0s", detail)

    def test_write_truncates_prior_record(self):
        # A success after a prior failure must fully replace it, so a stale
        # failure from an earlier boot never lingers and mislabels the panel.
        hw.write_display_status(initialized=False, error="old failure")
        hw.write_display_status(initialized=True)
        record = hw.read_display_status()
        self.assertTrue(record["initialized"])
        self.assertIsNone(record["error"])

    def test_busy_timeout_and_controller_round_trip(self):
        # The SSD1680-recovery write must persist busy_timeout (gates the web
        # opt-in) and the active controller, and read them back intact.
        hw.write_display_status(initialized=True, busy_timeout=True, controller="SSD1680")
        record = hw.read_display_status()
        self.assertTrue(record["busy_timeout"])
        self.assertEqual(record["active_controller"], "SSD1680")

    def test_busy_timeout_defaults_false_on_write(self):
        # A plain success (V2 panel) must record busy_timeout False so the web UI
        # keeps the IL3820 opt-in hidden.
        hw.write_display_status(initialized=True)
        record = hw.read_display_status()
        self.assertFalse(record["busy_timeout"])
        self.assertIsNone(record["active_controller"])


if __name__ == "__main__":
    unittest.main()
