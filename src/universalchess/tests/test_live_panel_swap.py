"""Selecting another e-paper panel while the board is running.

Why these tests exist:
    Display tuning can swap the driver live. The scheduler thread may be in
    the middle of a refresh when the web request arrives, and the new glass
    may not answer. The swap must not interleave two command sets on one
    refresh, must keep the working driver when the new init fails, and must
    tell the web process which controller now drives the panel.

How a regression manifests:
    - The swap completes while the old driver is still refreshing, so the
      recorded order has the new driver's init before the old display ends.
    - A failed init still replaces the driver, and the board is left headless.
    - The display-status file keeps the previous controller, so the System
      card shows the old panel and a waveform for the new one is rejected.
"""

import sys
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

for _mod in ("spidev", "RPi", "RPi.GPIO", "gpiozero"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.app import display_boot
from universalchess.board import display_selection as ds
from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.waveshare import panel_models as panels

# Long enough that an unlocked swap finishes inside it; the locked one waits
# for the release, which only comes after this.
BLOCKED_SWAP_WAIT_SECONDS = 0.2
THREAD_JOIN_SECONDS = 2.0
INIT_OK = 0
INIT_FAILED = -1


class _FakeEpd:
    width = 128
    height = 296
    gray_levels = 1
    three_color = False
    supports_partial = True
    partial_only_supported = True
    partial_only = False

    def __init__(self, name, order, init_result=INIT_OK):
        self.name = name
        self.order = order
        self.init_result = init_result
        self.display_entered = threading.Event()
        self.display_release = threading.Event()
        self.display_release.set()

    def init(self):
        self.order.append(f"{self.name}.init")
        return self.init_result

    def getbuffer(self, image):
        return [0xFF]

    def display(self, buf, should_abort=None):
        self.display_entered.set()
        self.display_release.wait(THREAD_JOIN_SECONDS)
        self.order.append(f"{self.name}.display")

    def DisplayPartial(self, buf):
        self.order.append(f"{self.name}.partial")

    def apply_partial_only(self, enabled):
        self.order.append(f"{self.name}.apply_partial_only")

    def idle_sleep(self):
        self.order.append(f"{self.name}.idle_sleep")


def _full_refresh_item():
    return (True, Future(), Image.new("1", (128, 296), 255), None, False)


class ReplaceEpdTests(unittest.TestCase):
    def test_failed_init_keeps_the_working_driver(self):
        # Why: a panel chosen in Settings may not be connected. How a
        # regression manifests: manager.epd is the new driver after its init
        # failed, and every later refresh talks to glass that is not there.
        order = []
        old = _FakeEpd("old", order)
        manager = Manager(epd=old)

        replaced = manager.replace_epd(_FakeEpd("new", order, init_result=INIT_FAILED))

        self.assertFalse(replaced)
        self.assertIs(manager.epd, old)
        self.assertIs(manager._scheduler._epd, old)
        self.assertEqual(order, ["new.init"])

    def test_successful_swap_initializes_once_and_draws_with_the_new_driver(self):
        # Why: the swap runs init() so a failure can be reported; the refresh
        # after it must not run init() again. How a regression manifests: a
        # second new.init in the order, or old.display instead of new.display.
        order = []
        manager = Manager(epd=_FakeEpd("old", order))
        new = _FakeEpd("new", order)

        self.assertTrue(manager.replace_epd(new))
        manager._scheduler._process_batch([_full_refresh_item()])

        self.assertIs(manager.epd, new)
        self.assertEqual(order, ["new.init", "new.display"])

    def test_driver_changes_wait_for_an_in_flight_refresh(self):
        # Why: the web request arrives on the main thread while the scheduler
        # thread may be refreshing. How a regression manifests: the change
        # finishes during the blocked refresh (still_waiting is False), or its
        # first step lands before old.display in the order.
        operations = (
            ("replace_epd", lambda manager, order: manager.replace_epd(_FakeEpd("new", order)),
             "new.init"),
            ("apply_partial_only", lambda manager, order: manager.apply_partial_only(True),
             "old.apply_partial_only"),
        )
        for label, operation, first_step in operations:
            with self.subTest(operation=label):
                order = []
                old = _FakeEpd("old", order)
                old.display_release.clear()
                manager = Manager(epd=old)
                refresh = threading.Thread(
                    target=manager._scheduler._process_batch, args=([_full_refresh_item()],),
                )
                refresh.start()
                self.assertTrue(old.display_entered.wait(THREAD_JOIN_SECONDS))

                change = threading.Thread(target=operation, args=(manager, order))
                change.start()
                change.join(BLOCKED_SWAP_WAIT_SECONDS)
                still_waiting = change.is_alive()
                old.display_release.set()
                refresh.join(THREAD_JOIN_SECONDS)
                change.join(THREAD_JOIN_SECONDS)

                self.assertTrue(still_waiting)
                self.assertEqual(order[:2], ["old.display", first_step])


class SwitchPanelTests(unittest.TestCase):
    def _manager(self, replaced):
        manager = MagicMock()
        manager.replace_epd.side_effect = replaced
        return manager

    def test_explicit_panel_reports_its_controller_only_when_it_initialized(self):
        # Why: the web process reads the controller from the status file. How
        # a regression manifests: no write after a successful swap (the System
        # card keeps the old panel), or a write after a failed one.
        model = panels.get_model("gdey029t94")
        for replaced, expected_calls in ((True, 1), (False, 0)):
            with self.subTest(replaced=replaced):
                manager = self._manager(lambda epd, result=replaced: result)
                with patch("universalchess.board.hardware_info.write_display_status") as write:
                    switched = display_boot.switch_to_panel(
                        manager, model, "gdey029t94", False, False,
                    )
                self.assertEqual(switched, replaced)
                self.assertEqual(write.call_count, expected_calls)
                if replaced:
                    write.assert_called_once_with(
                        initialized=True, busy_timeout=False,
                        controller=panels.CONTROLLER_SSD1680, panel=model.key,
                    )
                    self.assertEqual(manager.replace_epd.call_args[0][0].PANEL_KEY, model.key)

    def test_automatic_tries_each_centaur_controller_and_reports_the_one_that_answered(self):
        # Why: returning to Automatic re-runs the V1/V2 probe. How a
        # regression manifests: the second controller is never tried, or the
        # status names the controller that failed.
        tried = []

        def replace(epd):
            tried.append(type(epd).__module__.rsplit(".", 1)[-1])
            return len(tried) == 2

        manager = self._manager(replace)
        with patch.object(ds, "controller_order", return_value=(ds.CONTROLLER_UC8151D, ds.CONTROLLER_SSD1680)), \
                patch("universalchess.board.hardware_info.read_display_status", return_value=None), \
                patch("universalchess.board.hardware_info.write_display_status") as write:
            switched = display_boot.switch_to_automatic(manager, "", False)

        self.assertTrue(switched)
        self.assertEqual(tried, ["epd2in9d", "epd2in9_ssd1680"])
        write.assert_called_once_with(
            initialized=True, busy_timeout=False,
            controller=ds.CONTROLLER_SSD1680, panel=panels.PANEL_AUTO,
        )

    def test_automatic_keeps_the_current_driver_when_no_controller_answers(self):
        # Why: with neither Centaur controller answering, the running driver is
        # the only working one. How a regression manifests: a status write
        # claiming a controller, or fewer than two attempts.
        manager = self._manager(lambda epd: False)
        with patch.object(ds, "controller_order", return_value=(ds.CONTROLLER_UC8151D, ds.CONTROLLER_SSD1680)), \
                patch("universalchess.board.hardware_info.read_display_status", return_value=None), \
                patch("universalchess.board.hardware_info.write_display_status") as write:
            switched = display_boot.switch_to_automatic(manager, "", False)

        self.assertFalse(switched)
        self.assertEqual(manager.replace_epd.call_count, 2)
        write.assert_not_called()


class _TuningManager:
    """The Manager calls the live display-tuning path makes."""

    initialized = True

    def __init__(self, epd):
        self.epd = epd
        self.applied_profiles = []
        self.applied_three_color = []

    def apply_waveform_profile(self, profile):
        self.applied_profiles.append(profile.key)

    def apply_three_color(self, enabled):
        self.applied_three_color.append(enabled)

    def set_batch_updates(self, enabled):
        pass


class LiveWaveformTests(unittest.TestCase):
    """A live tuning change loads the waveform a reboot would load.

    Why this exists: the boot builder gives a panel without a waveform
    dropdown its pinned recipe, but the live path resolved the stored key by
    family alone. On GDEW029I6FD a stored ``uc8151d_t5d`` was loaded live and
    replaced by the pinned recipe on the next boot. A panel with no waveform
    table (GDEY029T71H, the four-color glasses) still ran a full refresh for
    a waveform it does not have.

    How a regression manifests: the applied profile differs from the one
    ``build_panel_epd`` resolves for the same settings, or a profile is
    applied to a panel without one.
    """

    def _apply(self, panel_key, stored_key, stored_three_color=None):
        return self._apply_with_manager(panel_key, stored_key, stored_three_color)[:2]

    def _apply_with_manager(self, panel_key, stored_key, stored_three_color=None):
        from universalchess.app import board_app
        from universalchess.app.pending_work import PendingWork
        from universalchess.board import board, display_settings

        model = panels.get_model(panel_key)
        epd = display_boot.build_panel_epd(model, stored_key, False)
        manager = _TuningManager(epd)
        pending = PendingWork()
        pending.display_profile.request({})
        three_color = epd.three_color if stored_three_color is None else stored_three_color
        flags = {"three_color": three_color, "partial_only": False}
        with patch.object(board_app, "_pending", pending), \
                patch.object(board, "display_manager", manager, create=True), \
                patch.object(display_settings, "read_waveform_profile_key", return_value=stored_key), \
                patch.object(display_settings, "read_panel_key", return_value=panel_key), \
                patch.object(display_settings, "read_flag",
                             side_effect=lambda name, default=False: flags.get(name, default)):
            board_app._process_pending_display_profile()
        return manager.applied_profiles, epd, manager

    def test_live_apply_matches_the_boot_resolution(self):
        cases = (
            ("gdew029i6fd", "uc8151d_t5d"),
            ("gdey029t94", "gdem029t94"),
            ("gdey029t94", "gdey029z95"),
        )
        for panel_key, stored_key in cases:
            with self.subTest(panel=panel_key, stored=stored_key):
                applied, epd = self._apply(panel_key, stored_key)
                self.assertEqual(applied, [epd.profile.key])

    def test_centaur_waveform_is_applied_live_on_gdey029z95(self):
        # Why: the Waveshare GDEM029T94 table drives this glass with the
        # steadiest clock, and choosing it must take effect without a reboot.
        # How a regression manifests: the vendor recipe gdey029z95 is applied
        # instead, so the choice only lands after the next boot or never.
        applied, _epd = self._apply("gdey029z95", "gdem029t94")
        self.assertEqual(applied, ["gdem029t94"])

    def test_panel_without_a_waveform_table_is_not_refreshed_for_one(self):
        for panel_key in ("gdey029t71h", "gdey029f51h", "gdey029f52"):
            with self.subTest(panel=panel_key):
                applied, _epd = self._apply(panel_key, "gdem029t94")
                self.assertEqual(applied, [])

    def test_live_three_color_turns_on_only_for_red_ink(self):
        # Why: a stored switch left on from GDEY029Z95 must not turn red on
        # live after the glass becomes black and white. How a regression
        # manifests: apply_three_color(True) on GDEY029T94, which runs the
        # slow tri-color refresh on glass with no red, or no call on Z95.
        cases = (("gdey029t94", []), ("gdey029t71h", []), ("gdey029z95", [True]))
        for panel_key, expected in cases:
            with self.subTest(panel=panel_key):
                _applied, _epd, manager = self._apply_with_manager(
                    panel_key, "gdem029t94", stored_three_color=True,
                )
                self.assertEqual(manager.applied_three_color, expected)


if __name__ == "__main__":
    unittest.main()
