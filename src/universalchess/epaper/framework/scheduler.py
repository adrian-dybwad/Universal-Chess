"""
Refresh scheduler that uses Waveshare DisplayPartial directly.
"""

import contextlib
import threading
import queue
import time
from typing import Optional
from concurrent.futures import Future
from PIL import Image
from .framebuffer import FrameBuffer
from .waveshare.epd2in9d import EPD, RefreshInterrupted
from .waveshare import epdconfig

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

class Scheduler:
    """Background thread that schedules display refreshes using Waveshare DisplayPartial.
    
    Args:
        framebuffer: The FrameBuffer to read display state from
        epd: The EPD hardware driver
        on_display_updated: Optional callback invoked with the displayed image (PIL Image)
                            after each successful display update. Used for web dashboard mirroring.
    """
    
    # Maximum queue size - when full, oldest items are dropped to make room for new ones
    QUEUE_MAX_SIZE = 5

    # Seconds of inactivity after which the panel is parked in deep sleep to make
    # it robust against light-induced darkening (see EPD.idle_sleep). Long enough
    # not to interfere with normal navigation, short enough that a display left
    # unattended settles quickly. Waking costs a reset+Clear, so this is a balance.
    IDLE_SLEEP_SECONDS = 20.0
    
    def __init__(self, framebuffer: FrameBuffer, epd: EPD, on_display_updated=None,
                 batch_updates: bool = True):
        self._framebuffer = framebuffer
        self._epd = epd
        # Update batching (default on): coalesce a rapid burst of mono partial
        # refreshes to a single refresh of the final frame. When refresh requests
        # arrive faster than the panel can draw (fast menu browsing, a quickly
        # changing position, frequent status/clock updates), each queued frame
        # otherwise replays as its own partial, so the panel visibly lags behind
        # the latest state. Off restores the legacy per-item replay (every
        # intermediate frame drawn). Does NOT affect the three-color path, which
        # always coalesces out of necessity (each tri-color refresh is ~14s).
        self._batch_updates = batch_updates
        self._queue = queue.Queue(maxsize=self.QUEUE_MAX_SIZE)
        self._queue_lock = threading.Lock()  # Protects queue operations during eviction
        self._thread = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()  # Event to wake scheduler for immediate processing
        # Number of partial refreshes before forcing a periodic full refresh to
        # clear e-paper ghosting. 0 disables the periodic full refresh entirely;
        # full refreshes then happen only when explicitly requested (full=True),
        # e.g. screen transitions. Disabled for now since proper panel sleep made
        # the periodic flash unnecessary - raise above 0 to re-enable.
        self._max_partial_refreshes = 0
        self._partial_refresh_count = 0
        self._in_partial_mode = False  # Track if display is in partial refresh mode
        # The very first partial refresh must Clear() once to establish a known
        # white baseline (controller RAM is undefined at power-on). After that the
        # driver keeps the partial baseline in sync (self.buffer), so no further
        # Clear() - and no white flash - is needed on full->partial transitions or
        # deep-sleep wakes.
        self._baseline_established = False
        self._on_display_updated = on_display_updated  # Callback after display update
        self._idle_sleep_seconds = self.IDLE_SLEEP_SECONDS
        self._last_activity = None  # monotonic time of last refresh; None until first refresh
        self._deep_asleep = False  # True while the panel is parked in idle deep sleep
        # Set by force_reinit() to make the next refresh re-run epd.init() even in
        # full-refresh mode, so a live waveform-profile change reloads the panel's
        # LUT/voltages (a normal full refresh skips init() unless transitioning).
        self._force_reinit = False
        # Set by an interrupted full refresh: the panel was left mid-transition
        # by the aborted waveform, so the very next refresh MUST be full (a
        # partial drawn over a half-developed full frame ghosts). Consumed by the
        # next routing decision; re-armed if that restart is itself interrupted.
        self._pending_full_after_interrupt = False
        # Last RED-plane buffer shown on the panel (three-color mode only), in the
        # getbuffer_red mask space (red = 0 bit, no-red = 0xFF byte). None until
        # the first three-color frame. Used to detect when red appears, changes,
        # or clears so the slow full tri-color refresh runs only then.
        self._last_red_buffer = None
    
    def set_batch_updates(self, enabled: bool) -> None:
        """Enable/disable update batching live (no reboot).

        Backs the display-tuning toggle: when enabled, a rapid burst of mono
        partial refreshes coalesces to the final frame; when disabled, every
        queued frame is replayed. Takes effect on the next batch processed, so
        the web toggle applies without restarting the board.
        """
        self._batch_updates = enabled

    def force_reinit(self) -> None:
        """Force the next refresh to re-run the panel's init().

        A full refresh normally calls init() only when transitioning from partial
        mode or waking from deep sleep. A live waveform-profile change swaps the
        driver's LUT/voltage recipe, which is only applied during init(), so this
        flag makes the next refresh re-init even when already in full mode.
        """
        self._force_reinit = True

    def start(self) -> None:
        """Start the refresh scheduler thread."""
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
    
    def stop(self) -> None:
        """Stop the refresh scheduler thread."""
        self._stop_event.set()
        # Wake the thread if it's waiting
        self._wake_event.set()
        # Drain the queue to prevent new operations from starting
        self.clear_pending()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
    
    def clear_pending(self) -> None:
        """Clear all pending refresh requests from the queue.
        
        Marks all pending futures as cancelled. Use when transitioning between
        display states to prevent stale updates from rendering.
        """
        with self._queue_lock:
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    future = item[1]
                    if not future.done():
                        future.set_result("cleared")
                except queue.Empty:
                    break
        log.debug("Scheduler.clear_pending(): Cleared pending refresh requests")
    
    def submit_deferred(self, callback) -> None:
        """Schedule a callback to run on the scheduler thread.
        
        Used to defer operations that would cause recursion if executed immediately.
        The callback will run on the next scheduler iteration.
        
        Args:
            callback: A callable to execute on the scheduler thread.
        """
        # Use a simple threading.Timer with 0 delay to defer to next event loop tick
        timer = threading.Timer(0.001, callback)
        timer.daemon = True
        timer.start()
    
    def submit(self, full: bool = False, immediate: bool = False, image: Optional[Image.Image] = None,
               red_image: Optional[Image.Image] = None, clock_source: bool = False) -> Future:
        """Submit a refresh request.
        
        If the queue is full, the oldest item is dropped to make room for the new one.
        This ensures the display always shows the latest state.
        
        Args:
            full: If True, force a full refresh instead of partial refresh.
            immediate: If True, wake scheduler immediately to process without batching delay.
            image: Optional pre-captured image snapshot. If provided, this exact image will be
                   displayed. If None, scheduler will take snapshot from framebuffer when processing.
            red_image: Optional RED-plane snapshot for three-color mode (0 = red, 255 = not red).
                   None on mono panels. Drives the hybrid refresh decision: a non-empty or changed
                   red plane forces a full three-color refresh, otherwise the fast B/W partial path
                   is used (see _process_batch).
            clock_source: True if this frame originates from the clock's tick heartbeat.
                   Clock frames must NOT interrupt an in-progress full refresh (see
                   _refresh_should_abort): on a three-color panel a full refresh is
                   ~14s, and a once-per-second clock tick would abort and restart it
                   forever so it never completes. Non-clock frames (moves, overlays,
                   transitions) still interrupt so the freshest state wins promptly.
        """
        if full:
            log.warning(f"Scheduler.submit() called with full=True (will cause flashing refresh)")
        
        future = Future()
        with self._queue_lock:
            # If queue is full, drop oldest item to make room
            if self._queue.full():
                # queue.Empty -> another thread drained it between full() and
                # get_nowait(); there is simply nothing to evict, which is fine.
                with contextlib.suppress(queue.Empty):
                    old_item = self._queue.get_nowait()
                    old_future = old_item[1]
                    if not old_future.done():
                        old_future.set_result("evicted")
                    log.warning("Scheduler.submit(): Queue full, evicted oldest item to make room for new update")
            
            try:
                self._queue.put_nowait((full, future, image, red_image, clock_source))
                if immediate:
                    # Wake scheduler thread immediately for urgent updates (e.g., menu arrow)
                    self._wake_event.set()
            except queue.Full:
                # Should not happen after eviction, but handle gracefully
                log.warning("Scheduler.submit(): Queue still full after eviction attempt")
                future.set_result("queue-full")
        return future
    
    def _refresh_should_abort(self) -> bool:
        """Predicate handed to the driver's full-refresh BUSY wait.

        Returns True the instant a newer *interrupting* frame is queued (or
        shutdown is requested), so an in-flight full refresh aborts and the
        scheduler restarts with the latest data. "Interrupting" excludes clock
        heartbeat frames (submitted with clock_source=True): on a three-color
        panel a full refresh is ~14s, so a once-per-second clock tick would abort
        and restart it forever -- it never completes, and any red on screen stays
        half-developed. Ignoring clock frames here lets the slow full refresh
        finish; the queued clock frames are drained (and coalesced) afterwards.
        Non-clock frames (moves, overlays, transitions) still interrupt so the
        freshest real state wins promptly. By design there is NO livelock guard
        for interrupting frames: real update bursts are finite (a move settles)
        and showing the freshest frame is preferred over completing a stale one.
        Only full refreshes consult this; fast B/W partials are too short to be
        worth interrupting.
        """
        if self._stop_event.is_set():
            return True
        # Peek the pending frames (bounded, QUEUE_MAX_SIZE) under the queue's own
        # mutex; abort only if a non-clock frame is waiting.
        with self._queue.mutex:
            return any(not item[4] for item in self._queue.queue)

    def _mark_activity(self) -> None:
        """Record that a refresh just happened, resetting the idle-sleep timer."""
        self._last_activity = time.monotonic()

    def _should_idle_sleep(self, now: float) -> bool:
        """Return True if the panel is due to be parked in idle deep sleep.

        Requires that at least one refresh has occurred (_last_activity set), the
        panel is not already parked, and the inactivity threshold has elapsed.
        """
        if self._deep_asleep:
            return False
        if self._last_activity is None:
            return False
        return (now - self._last_activity) >= self._idle_sleep_seconds

    def _enter_idle_sleep(self) -> None:
        """Park the panel in deep sleep and force the next draw to re-init.

        Setting _in_partial_mode False (and resetting the partial count) makes the
        next refresh run its init() transition, whose reset() wakes the panel from
        deep sleep. Errors are swallowed during shutdown races (SPI may be closing).
        """
        try:
            self._epd.idle_sleep()
        except Exception as e:
            error_msg = str(e).lower()
            is_shutdown_error = 'closed' in error_msg or 'uninitialized' in error_msg or 'gpio' in error_msg
            if not self._stop_event.is_set() and not is_shutdown_error:
                log.error(f"ERROR entering idle sleep: {e}")
            return
        self._deep_asleep = True
        self._in_partial_mode = False
        self._partial_refresh_count = 0
        log.debug("Scheduler: panel parked in idle deep sleep")

    def _maybe_idle_sleep(self, now: Optional[float] = None) -> None:
        """Park the panel if it has been idle past the threshold."""
        if self._stop_event.is_set():
            return
        if now is None:
            now = time.monotonic()
        if self._should_idle_sleep(now):
            self._enter_idle_sleep()

    def _run(self) -> None:
        """Main scheduler loop."""
        while not self._stop_event.is_set():
            try:
                batch = []
                timeout = 0.1
                
                # Collect batch of requests
                while len(batch) < 10:
                    # Check if we should wake up immediately (for urgent updates like menu navigation)
                    if self._wake_event.is_set():
                        self._wake_event.clear()
                        # Try to get item immediately without waiting
                        try:
                            item = self._queue.get_nowait()
                            batch.append(item)
                            timeout = 0.0
                            continue
                        except queue.Empty:
                            # No items yet, but wake event was set - process what we have
                            break
                    
                    try:
                        item = self._queue.get(timeout=timeout)
                        batch.append(item)
                        timeout = 0.0
                    except queue.Empty:
                        break
                
                if batch:
                    self._process_batch(batch)
                else:
                    # No work this tick: park the panel if it has gone idle.
                    self._maybe_idle_sleep()
                    
            except Exception as e:
                log.error(f"ERROR in refresh scheduler: {e}")
                import traceback
                traceback.print_exc()
    
    def _process_batch(self, batch: list) -> None:
        """Process a batch of refresh requests.
        
        Each item in the batch is processed separately to ensure rapid updates
        (like menu navigation) display all intermediate states, not just the final state.
        Each item carries its own image snapshot captured at render time.
        """
        # Three-color mode: COALESCE the batch to its final composite frame. Each
        # tri-color full refresh is ~14s, and one logical change (e.g. a single
        # move) emits several widget updates (board + clock + analysis + status),
        # each a separate request. Replaying them in order would queue several full
        # refreshes for one change -- the "full refresh multiple times" flicker.
        # Only the last item carries the latest snapshot, so refresh that once and
        # resolve the superseded futures. A full request anywhere in the batch is
        # preserved. This coalescing is MANDATORY here (not gated by
        # _batch_updates): replaying ~14s refreshes is never acceptable.
        if getattr(self._epd, "three_color", False):
            if self._stop_event.is_set():
                for item in batch:
                    if not item[1].done():
                        item[1].set_result("shutdown")
                return
            *superseded, last = batch
            for s_full, s_future, _s_img, _s_red, _s_clock in superseded:
                if not s_future.done():
                    s_future.set_result("coalesced")
            any_full = any(item[0] for item in batch)
            full, future, image, red_image, _clock = last
            self._process_three_color(any_full, future, image, red_image)
            return

        # Mono path. With update batching on (default), a rapid burst -- refresh
        # requests arriving faster than the panel can draw a partial (fast menu
        # browsing, a quickly changing position, frequent status/clock updates) --
        # is coalesced to a single refresh of the final frame, mirroring the
        # three-color path above. Otherwise each queued frame replays as its own
        # partial, so N rapid requests run N sequential partials and the panel
        # visibly lags behind the latest state. A full request anywhere in the
        # burst is preserved so screen transitions still force a full refresh.
        # With the option off, the legacy per-item replay below runs unchanged
        # (every intermediate frame drawn).
        if self._batch_updates and len(batch) > 1:
            *superseded, last = batch
            for s_full, s_future, _s_img, _s_red, _s_clock in superseded:
                if not s_future.done():
                    s_future.set_result("coalesced")
            any_full = any(item[0] for item in batch)
            batch = [(any_full, last[1], last[2], last[3], last[4])]

        # Process each item separately to ensure all updates are displayed
        for item in batch:
            full, future, image, red_image, _clock = item

            if self._stop_event.is_set():
                if not future.done():
                    future.set_result("shutdown")
                continue

            # Full refresh when explicitly requested, or when the periodic
            # partial-count threshold is reached. _max_partial_refreshes == 0
            # disables the periodic count-based full refresh.
            periodic_full = (
                self._max_partial_refreshes > 0
                and self._partial_refresh_count >= self._max_partial_refreshes
            )
            # Promote to full after an interrupted full refresh (consume the
            # one-shot flag; the interrupt handler re-arms it if this restart is
            # itself interrupted). The aborted waveform left the panel
            # mid-transition, so a partial over it would ghost.
            force_full = self._pending_full_after_interrupt
            self._pending_full_after_interrupt = False
            full_refresh = full or periodic_full or force_full
            if full_refresh:
                self._execute_full_refresh_single(full or force_full, future, image)
            else:
                self._execute_partial_refresh_single(full, future, image)

    def _no_red_buffer(self) -> list:
        """All-no-red buffer in getbuffer_red mask space (every byte 0xFF)."""
        return [0xFF] * ((self._epd.width // 8) * self._epd.height)

    def _emit_display_updated(self, image: Image.Image,
                             red_image: Optional[Image.Image] = None) -> None:
        """Notify the refresh callback of the just-displayed frame.

        Passes the B/W image and, in three-color mode, the RED-plane snapshot so
        the web mirror can compose an RGB (white/black/red) preview. red_image is
        None for mono/fast-B/W refreshes (no red on screen). Callback failures are
        swallowed -- the mirror is best-effort and must never break a refresh.
        """
        if not self._on_display_updated:
            return
        try:
            self._on_display_updated(image, red_image)
        except Exception as cb_e:
            log.debug(f"on_display_updated callback failed: {cb_e}")

    def _process_three_color(self, full: bool, future: Future,
                             image: Optional[Image.Image],
                             red_image: Optional[Image.Image]) -> None:
        """Route one three-color update to the fast B/W or full tri-color path.

        Decision (red buffers compared in getbuffer_red mask space):
          - red present, red changed, red just cleared, or an explicit full
            refresh -> display_color (full tri-color). The explicit-full case
            must NOT fall through to the mono full path, whose display() writes
            the B/W image to the panel RED channel (0x13) and bleeds black to red.
          - otherwise -> the fast B/W partial path (DisplayPartial, which the
            driver routes to its tri-color B/W-only refresh).

        Tracks the shown red buffer so a clear (non-empty -> empty) still forces
        one full refresh to erase the bistable red ink.
        """
        if red_image is not None:
            red_buf = self._epd.getbuffer_red(red_image)
        else:
            red_buf = self._no_red_buffer()

        has_red = any(b != 0xFF for b in red_buf)
        # A full tri-color refresh is the ONLY way to change the red layer, but it
        # is also the slow (~14s) flashing path. So go full ONLY when the red plane
        # actually CHANGES -- not merely because red is on screen. Static red (e.g.
        # a persistent analysis bar or a king-in-check square that has not moved)
        # rides along untouched on the fast B/W partial path: the partial waveform
        # drives 0x24 only and the bistable red RAM (0x26) holds its ink. Forcing a
        # full refresh on has_red instead made every clock tick a full refresh
        # whenever any red was visible -- the runaway flicker.
        if self._last_red_buffer is None:
            # First three-color frame: full only if there is red to lay down;
            # otherwise a B/W partial is fine (no red to develop yet).
            red_changed = has_red
        else:
            red_changed = red_buf != self._last_red_buffer
        # A restart after an interrupted full refresh is forced full regardless of
        # red: the prior aborted waveform left the panel mid-transition, so a
        # partial would ghost. Consume the one-shot flag; the interrupt handler
        # re-arms it if this restart is itself interrupted.
        force_full = self._pending_full_after_interrupt
        self._pending_full_after_interrupt = False
        go_full = full or red_changed or force_full

        log.info(
            "Scheduler three-color: path=%s (full_req=%s has_red=%s red_changed=%s "
            "post_interrupt_full=%s) in_partial=%s deep_asleep=%s force_reinit=%s",
            "FULL_COLOR" if go_full else "BW_PARTIAL",
            full, has_red, red_changed, force_full,
            self._in_partial_mode, self._deep_asleep, self._force_reinit,
        )

        if go_full:
            interrupted = self._execute_color_refresh_single(
                future, image, red_buf, red_image)
            if interrupted:
                # The aborted tri-color frame never fully developed on the panel,
                # so do NOT record it as the baseline. Forgetting it forces the
                # restart (with the newer data) to take the full color path and
                # re-lay the red from a known state.
                self._last_red_buffer = None
                return
        else:
            self._execute_partial_refresh_single(False, future, image)

        self._last_red_buffer = red_buf

    def _execute_color_refresh_single(self, future: Future,
                                      image: Optional[Image.Image],
                                      red_buf: list,
                                      red_image: Optional[Image.Image] = None) -> bool:
        """Execute a full three-color (red/white/black) refresh for one request.

        The tri-color refresh is the only path that can change the red layer, so
        it always drives every pixel (no Clear() needed). Re-inits when waking
        from deep sleep, transitioning from the B/W partial path, or when a live
        mode/profile change forced it -- matching the B/W full path.

        Returns True if the refresh was interrupted by newer queued data (the
        caller must then forget the red baseline and let the restart redraw it),
        False otherwise.
        """
        if self._stop_event.is_set():
            if not future.done():
                future.set_result("shutdown")
            return False

        try:
            if self._in_partial_mode or self._deep_asleep or self._force_reinit:
                self._epd.init()
                self._in_partial_mode = False
                self._deep_asleep = False
                self._force_reinit = False

            if image is not None:
                full_image = image
            else:
                full_image = self._framebuffer.snapshot(rotation=epdconfig.ROTATION)

            bw_buf = self._epd.getbuffer(full_image)
            log.debug("Scheduler: Sending FULL THREE-COLOR refresh to display")
            self._epd.display_color(bw_buf, red_buf,
                                    should_abort=self._refresh_should_abort)
            self._partial_refresh_count = 0
            self._baseline_established = True
            self._mark_activity()

            self._emit_display_updated(full_image, red_image)
        except RefreshInterrupted:
            # Newer data arrived mid-refresh. The aborted waveform left the panel
            # mid-transition, so force a re-init before the next draw (its reset()
            # halts the partial update and re-establishes a clean state). The
            # restart with the latest frame happens on the next loop iteration.
            log.info("Scheduler: three-color refresh interrupted by newer data; "
                     "will re-init and restart with a FULL refresh")
            self._force_reinit = True
            self._pending_full_after_interrupt = True
            if not future.done():
                future.set_result("interrupted")
            return True
        except Exception as e:
            error_msg = str(e).lower()
            is_shutdown_error = 'closed' in error_msg or 'uninitialized' in error_msg or 'gpio' in error_msg
            if not self._stop_event.is_set() and not is_shutdown_error:
                log.error(f"ERROR in three-color refresh: {e}")
                import traceback
                traceback.print_exc()

        if not future.done():
            future.set_result("color")
        return False
    
    def _execute_full_refresh_single(self, full: bool, future: Future, image: Optional[Image.Image]) -> None:
        """Execute a full screen refresh for a single request."""
        # Check if shutdown was requested before using hardware
        if self._stop_event.is_set():
            if not future.done():
                future.set_result("shutdown")
            return
        
        try:
            # Re-initialize when transitioning from partial mode, OR when waking the
            # panel from idle deep sleep. init() calls reset(), which is what exits
            # deep sleep; without it display() would write to an unresponsive panel.
            if self._in_partial_mode or self._deep_asleep or self._force_reinit:
                log.debug(f"Scheduler._execute_full_refresh_single(): init() (partial->full, idle-sleep wake, or forced re-init)")
                self._epd.init()
                self._in_partial_mode = False
                self._deep_asleep = False
                self._force_reinit = False
            
            # Use provided image if available, otherwise take snapshot from framebuffer
            if image is not None:
                full_image = image
            else:
                full_image = self._framebuffer.snapshot(rotation=epdconfig.ROTATION)
            
            buf = self._epd.getbuffer(full_image)
            log.debug(f"Scheduler: Sending FULL refresh to display")
            self._epd.display(buf, should_abort=self._refresh_should_abort)
            self._partial_refresh_count = 0
            # A full refresh drives every pixel and records the shown image as the
            # driver baseline, so the following partial needs no cold-start Clear().
            self._baseline_established = True
            self._mark_activity()
            
            # Invoke callback after successful display update (mono: no red plane)
            self._emit_display_updated(full_image)
        except RefreshInterrupted:
            # Newer data arrived mid-refresh: abort and force a re-init so the
            # next draw resets the panel (halting the aborted waveform) and
            # restarts with the latest frame on the next loop iteration.
            log.info("Scheduler: full refresh interrupted by newer data; "
                     "will re-init and restart with a FULL refresh")
            self._force_reinit = True
            self._pending_full_after_interrupt = True
            if not future.done():
                future.set_result("interrupted")
            return
        except Exception as e:
            # Don't log errors during shutdown (SPI may be closed)
            # Also suppress GPIO-related errors that occur during shutdown race conditions
            error_msg = str(e).lower()
            is_shutdown_error = 'closed' in error_msg or 'uninitialized' in error_msg or 'gpio' in error_msg
            if not self._stop_event.is_set() and not is_shutdown_error:
                log.error(f"ERROR in full refresh: {e}")
                import traceback
                traceback.print_exc()
        
        if not future.done():
            future.set_result("full")
    
    def _execute_partial_refresh_single(self, full: bool, future: Future, image: Optional[Image.Image]) -> None:
        """Execute partial refresh for a single request."""
        # Check if shutdown was requested before using hardware
        if self._stop_event.is_set():
            if not future.done():
                future.set_result("shutdown")
            return
        
        try:
            # Prepare the panel for a partial refresh. Three distinct cases:
            #  - Cold start: controller RAM is undefined, so init()+Clear() once to
            #    establish a known white baseline matching the driver's buffer.
            #  - Deep-sleep wake / full->partial transition: init() re-powers/resets
            #    the panel (reset() is what exits deep sleep). No Clear() is needed:
            #    DisplayPartial re-sends the preserved previous frame (self.buffer)
            #    to old-RAM every call, and display() records the shown image, so
            #    the partial baseline already matches the panel - skipping Clear()
            #    removes the white flash.
            needs_baseline = not self._baseline_established
            needs_init = (needs_baseline or self._deep_asleep
                          or not self._in_partial_mode or self._force_reinit)
            if needs_init:
                self._force_reinit = False
                # Check again before using hardware (might have been shut down while processing)
                if self._stop_event.is_set():
                    if not future.done():
                        future.set_result("shutdown")
                    return
                
                self._epd.init()
                if needs_baseline:
                    log.debug("Scheduler._execute_partial_refresh_single(): cold start - init()+Clear() to establish baseline")
                    self._epd.Clear()
                    self._baseline_established = True
                else:
                    log.debug("Scheduler._execute_partial_refresh_single(): init() without Clear() (deep-sleep wake / full->partial)")
                self._in_partial_mode = True
                self._deep_asleep = False
            
            # Use provided image or take snapshot from framebuffer
            if image is not None:
                display_image = image
            else:
                display_image = self._framebuffer.snapshot(rotation=epdconfig.ROTATION)
            
            # Final check before continuing (SPI might be closed during shutdown)
            if self._stop_event.is_set():
                if not future.done():
                    future.set_result("shutdown")
                return
            
            # Get buffer from image and display
            buf = self._epd.getbuffer(display_image)
            log.debug(f"Scheduler: Sending PARTIAL refresh to display (count={self._partial_refresh_count + 1})")
            self._epd.DisplayPartial(buf)
            
            self._partial_refresh_count += 1
            self._mark_activity()
            
            # Invoke callback after successful display update (fast B/W: no red plane)
            self._emit_display_updated(display_image)
        except Exception as e:
            # Don't log errors during shutdown (SPI may be closed)
            # Also suppress GPIO-related errors that occur during shutdown race conditions
            error_msg = str(e).lower()
            is_shutdown_error = 'closed' in error_msg or 'uninitialized' in error_msg
            if not self._stop_event.is_set() and not is_shutdown_error:
                log.error(f"ERROR in partial refresh: {e}")
                import traceback
                traceback.print_exc()
        
        if not future.done():
            future.set_result("partial")
