"""Tests for the hybrid three-color refresh decision in the Scheduler (Phase 3).

Why these tests exist:
    Red ink on the BWR panel can only change via the slow (~12-15s) full
    tri-color refresh, while black/white content can refresh fast. The Scheduler
    therefore chooses per update:
      - no red on screen and none requested -> fast B/W path (DisplayPartial);
      - red appears, changes, or clears -> full color refresh (display_color);
      - any explicitly-full refresh in three-color mode -> display_color too,
        because the mono full path (display()) writes the B/W image to the panel
        RED channel (0x13) and would bleed black into red.
    A mono panel must keep its existing partial/full behaviour untouched.

How a regression manifests:
    - Bleed regression: three-color mode calls the mono display()/partial paths
      that write B/W to 0x13 -> the board renders red.
    - Stuck-red regression: clearing red does not trigger a full refresh, so a
      stale red highlight never erases (red is bistable; only the full waveform
      removes it).
    - Slowness regression: an ordinary no-red move triggers display_color, making
      every move a ~12-15s refresh.
"""

import sys
import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future

for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.scheduler import Scheduler
from universalchess.epaper.framework.waveshare.epd2in9d import (
    EPD_WIDTH,
    EPD_HEIGHT,
    RefreshInterrupted,
    pack_image_to_buffer,
)


def _bw_image():
    """A full-size B/W frame (content irrelevant to the routing decision)."""
    return Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)


def _red_image(has_red):
    """A full-size red mask: all not-red (255), or with a red block (0)."""
    img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)
    if has_red:
        img.paste(0, (0, 0, 16, 16))
    return img


def _red_image_at(box):
    """A full-size red mask with a red block at the given (l, t, r, b) box."""
    img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)
    img.paste(0, box)
    return img


class _FakeEpd:
    """Records which refresh method the scheduler chose for each item."""

    def __init__(self, three_color):
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        self.three_color = three_color
        self.calls = []

    # Packing reuses the real shared packer so has_red detection is realistic.
    def getbuffer(self, image):
        return pack_image_to_buffer(image, self.width, self.height)

    def getbuffer_red(self, image):
        return pack_image_to_buffer(image, self.width, self.height)

    def init(self):
        self.calls.append("init")
        return 0

    def Clear(self):
        self.calls.append("Clear")

    def display(self, buf, should_abort=None):
        self.calls.append("display")              # mono full (must NOT run in 3-color)
        if should_abort and should_abort():       # simulate driver aborting mid-wait
            raise RefreshInterrupted("newer data")

    def DisplayPartial(self, buf):
        self.calls.append("DisplayPartial")        # fast B/W path

    def display_color(self, bw, red, should_abort=None):
        self.calls.append("display_color")         # full tri-color path
        if should_abort and should_abort():       # simulate driver aborting mid-wait
            raise RefreshInterrupted("newer data")


def _make_scheduler(three_color, on_display_updated=None):
    epd = _FakeEpd(three_color)
    sched = Scheduler(MagicMock(), epd, on_display_updated=on_display_updated)
    return sched, epd


def _item(full, image, red_image):
    return (full, Future(), image, red_image)


class MonoSchedulerUnchangedTests(unittest.TestCase):
    def test_mono_partial_uses_displaypartial(self):
        # Off path, full=False -> fast partial. Regression would change mono UX.
        sched, epd = _make_scheduler(three_color=False)
        sched._process_batch([_item(False, _bw_image(), None)])
        self.assertIn("DisplayPartial", epd.calls)
        self.assertNotIn("display_color", epd.calls)

    def test_mono_full_uses_display(self):
        # Off path, full=True -> mono full refresh.
        sched, epd = _make_scheduler(three_color=False)
        sched._process_batch([_item(True, _bw_image(), None)])
        self.assertIn("display", epd.calls)
        self.assertNotIn("display_color", epd.calls)


class MonoBatchCoalesceTests(unittest.TestCase):
    """A rapid burst of refreshes must collapse to a single refresh of the final
    frame (mono path), not replay one partial per queued frame.

    Why these tests exist:
        Each widget update enqueues its own refresh request. When requests arrive
        faster than the panel can draw a partial (fast menu browsing, a quickly
        changing position, frequent status/clock updates), the scheduler drains
        the whole burst into one batch (the wake-event/get_nowait collection
        loop). The mono path historically replayed the burst item-by-item, so N
        rapid requests ran N sequential partial refreshes (each hundreds of ms on
        the panel). The panel then visibly lags behind the latest state -- the
        delay seen on every mono panel but absent on the three-color panel, which
        already coalesces its batch to the last frame. These pin the mono path to
        the same coalescing so a burst costs one refresh.

    How a regression manifests:
        If per-item replay returns, count("DisplayPartial") rises to N instead of
        1 and the superseded futures resolve "partial" instead of "coalesced" --
        i.e. the backlog/lag is back. A single request would still refresh, so a
        count of 1 (not 0) distinguishes the fix from over-coalescing that drops
        the final frame.
    """

    def test_partial_burst_coalesces_to_single_refresh_of_last(self):
        # Three queued partial frames (a rapid burst). Only the final frame
        # refreshes once; the two superseded futures resolve "coalesced".
        # Regression: three DisplayPartial calls -> the lag returns.
        sched, epd = _make_scheduler(three_color=False)
        i1 = _item(False, _bw_image(), None)
        i2 = _item(False, _bw_image(), None)
        i3 = _item(False, _bw_image(), None)
        sched._process_batch([i1, i2, i3])
        self.assertEqual(
            epd.calls.count("DisplayPartial"), 1,
            f"burst must collapse to one partial, got calls={epd.calls}")
        self.assertEqual(i1[1].result(), "coalesced")
        self.assertEqual(i2[1].result(), "coalesced")
        # The LAST frame is the one actually rendered (not a superseded one).
        self.assertEqual(i3[1].result(), "partial")

    def test_partial_burst_preserves_full_request_from_any_item(self):
        # A screen transition (full=True) anywhere in the burst must still force a
        # full refresh of the final frame, not be lost to coalescing. Regression:
        # the full request is dropped and the transition ghosts over a partial.
        sched, epd = _make_scheduler(three_color=False)
        i1 = _item(True, _bw_image(), None)   # full requested (e.g. screen change)
        i2 = _item(False, _bw_image(), None)
        sched._process_batch([i1, i2])
        self.assertIn("display", epd.calls)            # mono full path on last frame
        self.assertNotIn("DisplayPartial", epd.calls)
        self.assertEqual(i1[1].result(), "coalesced")
        self.assertEqual(i2[1].result(), "full")

    def test_single_item_batch_still_refreshes(self):
        # Edge guard for the coalescing split: a burst of one must still render
        # (not be treated as superseded and dropped). Fails if the fix's
        # "*superseded, last = batch" mishandles the single-item case.
        sched, epd = _make_scheduler(three_color=False)
        i1 = _item(False, _bw_image(), None)
        sched._process_batch([i1])
        self.assertEqual(epd.calls.count("DisplayPartial"), 1)
        self.assertEqual(i1[1].result(), "partial")

    def test_batching_off_replays_every_frame(self):
        # The "Batch rapid updates" option is user-disableable. With it off the
        # legacy per-item replay is restored: a 3-frame burst draws all three
        # intermediate states. Regression: OFF still coalesces -> the option is a
        # no-op and the user cannot get the show-every-frame behavior back.
        epd = _FakeEpd(three_color=False)
        sched = Scheduler(MagicMock(), epd, batch_updates=False)
        sched._process_batch([
            _item(False, _bw_image(), None),
            _item(False, _bw_image(), None),
            _item(False, _bw_image(), None),
        ])
        self.assertEqual(
            epd.calls.count("DisplayPartial"), 3,
            f"batching off must replay each frame, got calls={epd.calls}")

    def test_batch_updates_defaults_on(self):
        # The option ships ON: a scheduler built without the flag coalesces. This
        # pins the shipped default so a burst is fast out of the box. Regression:
        # default flips to off and every panel regains the lag.
        epd = _FakeEpd(three_color=False)
        sched = Scheduler(MagicMock(), epd)
        sched._process_batch([
            _item(False, _bw_image(), None),
            _item(False, _bw_image(), None),
        ])
        self.assertEqual(epd.calls.count("DisplayPartial"), 1)

    def test_set_batch_updates_live_toggle(self):
        # The setter flips behavior at runtime (the web toggle applies live with
        # no reboot): off -> replays both frames, on -> coalesces to one.
        # Regression: the setter does not take effect, so toggling in the UI does
        # nothing until reboot.
        sched, epd = _make_scheduler(three_color=False)
        sched.set_batch_updates(False)
        sched._process_batch([_item(False, _bw_image(), None),
                              _item(False, _bw_image(), None)])
        self.assertEqual(epd.calls.count("DisplayPartial"), 2)
        epd.calls.clear()
        sched.set_batch_updates(True)
        sched._process_batch([_item(False, _bw_image(), None),
                              _item(False, _bw_image(), None)])
        self.assertEqual(epd.calls.count("DisplayPartial"), 1)

    def test_three_color_always_coalesces_regardless_of_flag(self):
        # Update batching only governs the mono fast path. Three-color MUST always
        # coalesce because each tri-color refresh is ~14s; replaying a burst there
        # is catastrophic. So batch_updates=False must NOT re-enable per-item
        # replay on a three-color panel. Regression: the flag leaks into the
        # three-color path and a single move queues several ~14s refreshes.
        epd = _FakeEpd(three_color=True)
        sched = Scheduler(MagicMock(), epd, batch_updates=False)
        sched._process_batch([
            _item(False, _bw_image(), _red_image(has_red=False)),
            _item(False, _bw_image(), _red_image(has_red=False)),
            _item(False, _bw_image(), _red_image(has_red=True)),
        ])
        # Still exactly one refresh (the final frame had red -> color path).
        self.assertEqual(epd.calls.count("display_color"), 1)
        self.assertEqual(epd.calls.count("DisplayPartial"), 0)


class ThreeColorHybridTests(unittest.TestCase):
    def test_no_red_uses_fast_bw_path(self):
        # No red present/requested -> fast B/W (DisplayPartial), never the slow
        # color refresh, and never the mono display() (which would bleed red).
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=False))])
        self.assertIn("DisplayPartial", epd.calls)
        self.assertNotIn("display_color", epd.calls)
        self.assertNotIn("display", epd.calls)

    def test_red_present_uses_color_refresh(self):
        # Red onset -> full tri-color refresh.
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=True))])
        self.assertIn("display_color", epd.calls)
        self.assertNotIn("DisplayPartial", epd.calls)

    def test_full_request_uses_color_refresh_not_mono_display(self):
        # An explicit full refresh in three-color mode must use display_color,
        # NOT the mono display() (which writes B/W to the red channel 0x13).
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(True, _bw_image(), _red_image(has_red=False))])
        self.assertIn("display_color", epd.calls)
        self.assertNotIn("display", epd.calls)

    def test_red_clear_forces_one_color_refresh_then_fast(self):
        # Sequence: red present -> red cleared -> no red again.
        #   1) red present  -> display_color
        #   2) red cleared  -> display_color (erase red; red is bistable)
        #   3) still no red  -> fast DisplayPartial
        # Regression: step 2 going to the fast path leaves stale red on the panel.
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=True))])
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=False))])
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=False))])
        self.assertEqual(
            epd.calls.count("display_color"), 2,
            f"expected 2 color refreshes (onset + clear), got calls={epd.calls}")
        self.assertIn("DisplayPartial", epd.calls)


class StaticRedRidesPartialTests(unittest.TestCase):
    """Static (unchanged) red must NOT force a full refresh -- the flicker fix.

    Why: red ink is bistable and the fast B/W partial leaves the red RAM (0x26)
    untouched, so red persists across partials. Routing on has_red instead of
    red-CHANGED made every clock tick a ~14s full refresh whenever any red was on
    screen (e.g. a persistent analysis bar), which is the runaway flicker the user
    reported. Only a CHANGE to the red plane warrants the slow color refresh.
    """

    def test_static_red_second_update_uses_fast_bw_path(self):
        # Same red twice: 1st lays down red (display_color), 2nd is unchanged red
        # and must take the fast B/W path. Regression (has_red routing): the 2nd
        # would be a second full color refresh -> flicker.
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=True))])
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=True))])
        self.assertEqual(
            epd.calls.count("display_color"), 1,
            f"static red must refresh color once, got calls={epd.calls}")
        self.assertIn("DisplayPartial", epd.calls)

    def test_changed_red_triggers_second_color_refresh(self):
        # Red present, then red moves to a different location -> the red plane
        # changed, so a second full color refresh is required to redraw it.
        sched, epd = _make_scheduler(three_color=True)
        sched._process_batch([_item(False, _bw_image(), _red_image_at((0, 0, 16, 16)))])
        sched._process_batch([_item(False, _bw_image(), _red_image_at((32, 32, 48, 48)))])
        self.assertEqual(
            epd.calls.count("display_color"), 2,
            f"changed red must refresh color twice, got calls={epd.calls}")
        self.assertNotIn("DisplayPartial", epd.calls)


class BatchCoalesceTests(unittest.TestCase):
    """One logical change emits several widget updates; they must collapse to one
    refresh of the final composite, not one (full) refresh per item."""

    def test_batch_coalesces_to_single_refresh_of_last(self):
        # Three queued items in one batch (board, clock, analysis). Only the final
        # composite should be refreshed once; the earlier futures resolve as
        # "coalesced". Regression: each item refreshing separately queues multiple
        # ~14s full refreshes for a single move -- the observed flicker.
        sched, epd = _make_scheduler(three_color=True)
        i1 = _item(False, _bw_image(), _red_image(has_red=False))
        i2 = _item(False, _bw_image(), _red_image(has_red=False))
        i3 = _item(False, _bw_image(), _red_image(has_red=True))  # final state has red
        sched._process_batch([i1, i2, i3])
        # Exactly one hardware refresh happened, and it was the color path (last
        # frame had red). The two superseded items are resolved without refreshing.
        self.assertEqual(epd.calls.count("display_color"), 1)
        self.assertEqual(epd.calls.count("DisplayPartial"), 0)
        self.assertEqual(i1[1].result(), "coalesced")
        self.assertEqual(i2[1].result(), "coalesced")
        self.assertTrue(i3[1].done())

    def test_batch_preserves_full_request_from_any_item(self):
        # If any item in the batch requested a full refresh, the coalesced refresh
        # must be a full color refresh even if the final red plane is empty.
        sched, epd = _make_scheduler(three_color=True)
        i1 = _item(True, _bw_image(), _red_image(has_red=False))   # full requested
        i2 = _item(False, _bw_image(), _red_image(has_red=False))
        sched._process_batch([i1, i2])
        self.assertIn("display_color", epd.calls)
        self.assertNotIn("display", epd.calls)


class InterruptRefreshTests(unittest.TestCase):
    """A full refresh aborts and restarts when newer frame data is queued.

    The driver's wait raises RefreshInterrupted once should_abort fires;
    should_abort is true while sched._queue is non-empty (newer data waiting).
    The scheduler must resolve the in-flight future as "interrupted", arm a
    re-init (so the next refresh resets the panel and halts the aborted
    waveform), and -- for the color path -- forget the last red buffer so the
    next frame is forced full (the interrupted red never fully developed).
    """

    def _queue_newer_item(self, sched):
        # Put a newer item in the queue so _refresh_should_abort() returns True
        # during the refresh, then drain it so the test can inspect it.
        newer = _item(False, _bw_image(), _red_image(has_red=True))
        sched._queue.put_nowait(newer)
        return newer

    def test_color_refresh_interrupted_when_newer_queued(self):
        # Red onset would normally complete a color refresh; with a newer item
        # queued, display_color aborts. Regression (no abort handling): the future
        # never resolves "interrupted" and the stale red is committed, so the next
        # frame would diff against a frame that never rendered.
        sched, epd = _make_scheduler(three_color=True)
        self._queue_newer_item(sched)
        item = _item(False, _bw_image(), _red_image(has_red=True))
        sched._process_three_color(item[0], item[1], item[2], item[3])
        self.assertEqual(item[1].result(), "interrupted")
        self.assertTrue(sched._force_reinit)
        self.assertIsNone(sched._last_red_buffer)
        self.assertEqual(epd.calls.count("display_color"), 1)

    def test_mono_full_refresh_interrupted_when_newer_queued(self):
        # The mono full path is interruptible too (scope: all full refreshes).
        sched, epd = _make_scheduler(three_color=False)
        self._queue_newer_item(sched)
        future = Future()
        sched._execute_full_refresh_single(True, future, _bw_image())
        self.assertEqual(future.result(), "interrupted")
        self.assertTrue(sched._force_reinit)

    def test_restart_after_color_interrupt_is_full_even_without_red_change(self):
        # An aborted full refresh leaves the panel mid-transition, so the restart
        # must be a FULL refresh even when the interrupting frame would normally
        # take the fast B/W partial path (no red change, no full request).
        # Regression: routing on red_changed alone sends the restart to
        # DisplayPartial, ghosting on top of the half-developed full waveform.
        sched, epd = _make_scheduler(three_color=True)
        self._queue_newer_item(sched)
        first = _item(False, _bw_image(), _red_image(has_red=True))
        sched._process_three_color(first[0], first[1], first[2], first[3])
        self.assertEqual(first[1].result(), "interrupted")
        sched._queue.get_nowait()  # drain so the restart is not itself aborted
        epd.calls.clear()
        # No red, not a full request -> normally BW_PARTIAL; must be promoted.
        restart = _item(False, _bw_image(), _red_image(has_red=False))
        sched._process_three_color(restart[0], restart[1], restart[2], restart[3])
        self.assertIn("display_color", epd.calls)
        self.assertNotIn("DisplayPartial", epd.calls)

    def test_mono_restart_after_interrupt_is_full(self):
        # Same rule on a mono panel: after an interrupted full refresh, a plain
        # partial request must be promoted to a full refresh. Regression: the
        # restart runs DisplayPartial over a half-developed full waveform.
        sched, epd = _make_scheduler(three_color=False)
        self._queue_newer_item(sched)
        future = Future()
        sched._execute_full_refresh_single(True, future, _bw_image())
        self.assertEqual(future.result(), "interrupted")
        sched._queue.get_nowait()  # drain
        epd.calls.clear()
        sched._process_batch([_item(False, _bw_image(), None)])
        self.assertIn("display", epd.calls)
        self.assertNotIn("DisplayPartial", epd.calls)

    def test_color_refresh_completes_when_no_newer_data(self):
        # Inverse guard: empty queue -> should_abort False -> the refresh
        # completes normally, resolves "color", and commits the red buffer. A
        # regression that aborted spuriously would resolve "interrupted" here.
        sched, epd = _make_scheduler(three_color=True)
        item = _item(False, _bw_image(), _red_image(has_red=True))
        sched._process_three_color(item[0], item[1], item[2], item[3])
        self.assertEqual(item[1].result(), "color")
        self.assertFalse(sched._force_reinit)
        self.assertIsNotNone(sched._last_red_buffer)


class MirrorCallbackTests(unittest.TestCase):
    """The refresh callback receives the red plane only on the color path."""

    def test_color_refresh_forwards_red_plane(self):
        # The web mirror composes RGB from the red plane, so the color path must
        # pass a non-None red image to the callback. Regression: mirror loses red.
        seen = []
        sched, _ = _make_scheduler(True, on_display_updated=lambda img, red=None: seen.append(red))
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=True))])
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])

    def test_fast_bw_refresh_forwards_no_red(self):
        # The fast B/W path has no red on screen, so red must be None (mirror
        # falls back to a plain B/W preview).
        seen = []
        sched, _ = _make_scheduler(True, on_display_updated=lambda img, red=None: seen.append(red))
        sched._process_batch([_item(False, _bw_image(), _red_image(has_red=False))])
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0])


if __name__ == "__main__":
    unittest.main()
