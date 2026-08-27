"""
Main display manager coordinating widgets and refresh scheduling.
"""

import functools
import threading
import time
from typing import List, Optional
from concurrent.futures import Future
from PIL import Image
from .waveshare.epd2in9d import EPD
from .framebuffer import FrameBuffer
from .refresh_policy import RefreshAction, decide_refresh_action
from .scheduler import Scheduler
from .widget import Widget
from .waveshare import epdconfig
from ..status_bar import StatusBarWidget

import logging
log = logging.getLogger(__name__)
#log.setLevel(logging.INFO)

class Manager:
    """Main coordinator for the ePaper framework.
    
    Args:
        on_refresh: Optional callback invoked with the display image (PIL Image)
                    after each successful display update. Used for web dashboard mirroring.
    """
    
    def __init__(self, on_refresh=None, epd=None, batch_updates: bool = True):
        # epd defaults to the UC8151D (V2) driver. The startup selector may
        # inject an alternate driver instance (e.g. the SSD1680 V1 fallback)
        # without this coordinator needing to know which controller it drives.
        # batch_updates is read from settings at the edge (main) and injected
        # here so this coordinator and the scheduler stay free of settings I/O.
        self._epd = epd if epd is not None else EPD()
        self._framebuffer = FrameBuffer(self._epd.width, self._epd.height)
        self._scheduler = Scheduler(self._framebuffer, self._epd,
                                    on_display_updated=on_refresh,
                                    batch_updates=batch_updates)
        self._widgets: List[Widget] = []
        # Modals displaced by a later modal (shutdown/inactivity countdown over a
        # waiting splash). Only one modal is visible; the previous one is parked
        # here without stop() so remove_widget can restore it. clear_widgets and
        # shutdown still stop everything in this list so a real screen change
        # cannot resurrect a splash from the previous one.
        self._parked_modals: List[Widget] = []
        self._background = None  # Optional BackgroundWidget for dithered backgrounds
        self._initialized = False
        self._shutting_down = False
        self._update_in_progress = False  # Re-entrancy guard for update()
        self._pending_update = False  # Whether another update was requested during current update
        self._pending_full = False  # Whether the pending update needs full refresh

        # Refresh coordination (see refresh_policy.py). A render is expensive and
        # the single panel is a shared, slow resource, so routine widget updates
        # are not painted synchronously per event. Instead they mark the
        # framebuffer dirty and are flushed once -- by the clock's tick while a
        # timed game is running (clock-driven mode), or by a single coalesced
        # flush otherwise. Priority updates (clock heartbeat, overlays,
        # transitions) still render immediately.
        self._render_lock = threading.RLock()  # serialises actual renders across threads
        self._refresh_state_lock = threading.Lock()  # guards the flags below
        self._defer_to_clock = False  # True while the clock is the sole refresher
        self._dirty = False  # a routine update is waiting to be rendered
        self._dirty_full = False  # a waiting update needs a full refresh
        self._flush_scheduled = False  # a coalesced flush is already queued
        # Extra rotation on top of epdconfig.ROTATION (the panel mount). 180 turns
        # every screen -- menus included -- to face a player seated at the far
        # end. Square remapping alone left abort/takeback/next-game upright for
        # the original seat.
        self._content_rotation = 0
        log.debug(f"Manager.__init__() completed - Manager id: {id(self)}, EPD id: {id(self._epd)}")

    def set_content_rotation(self, degrees: int) -> None:
        """Rotate drawn content on top of the panel's mounting rotation.

        180 turns the whole screen around when the seated player is at the far
        end of the board (Lichess handed them the other color). Menus, game-over,
        and the status bar use this path; square remapping alone left them facing
        the original seat. 0 restores the mounting-only orientation after the
        game ends so the main menu is not left upside down.
        """
        self._content_rotation = int(degrees) % 360

    def output_rotation(self) -> int:
        """Degrees logical content is rotated onto the panel."""
        return (int(epdconfig.ROTATION) + int(self._content_rotation)) % 360
    
    def initialize(self) -> None:
        """Initialize the display hardware.
        
        Initializes the e-paper hardware and scheduler. Does not set any background -
        callers should explicitly call set_background() if they want a dithered background.
        By default, the display will have a plain white background.
        
        The status bar is not added by default during initialization.
        The caller is responsible for adding it when appropriate (e.g., when showing a menu).
        
        Nothing is painted: the panel is left untouched until the first caller adds a
        widget, so boot shows the splash as its first frame rather than a blank screen.
        """
        if self._initialized:
            return
        
        try:
            result = self._epd.init()
            if result != 0:
                detail = getattr(self._epd, "init_error", None) or (
                    "e-Paper display init returned failure"
                )
                raise RuntimeError(detail)
            
            self._scheduler.start()
            time.sleep(0.1)
            
            self._initialized = True
            self.clear_widgets(addStatusBar=False)

        except Exception as e:
            raise RuntimeError(f"Failed to initialize display: {e}") from e
    
    @property
    def epd(self):
        """The active EPD driver instance.

        Exposed read-only so ``main`` can resolve the correct waveform profile
        for whichever controller actually drove the panel (the primary UC8151D or
        the SSD1680 fallback) via ``epd.CONTROLLER``, without reaching into the
        coordinator's internals.
        """
        return self._epd

    def apply_waveform_profile(self, profile, high_contrast: bool) -> Future:
        """Adopt a new waveform profile live and force a full refresh.

        Works for either driver (UC8151D or SSD1680): both expose ``apply_profile``
        and consume a controller-appropriate profile. Backs the no-reboot profile
        change: swaps the driver's profile/override, forces the next refresh to
        re-run init() (which reloads the LUT and
        voltages), and submits a full refresh so the current screen redraws with
        the new settings. A no-op when the active driver predates ``apply_profile``
        (no such attribute), so calling it is always harmless.

        Returns a Future that completes when the refresh finishes (already
        completed if not applicable or the display is not initialized).
        """
        if not hasattr(self._epd, "apply_profile"):
            future = Future()
            future.set_result("not-applicable")
            return future
        self._epd.apply_profile(profile, high_contrast)
        self._scheduler.force_reinit()
        return self.update(full=True, immediate=True)

    def apply_three_color(self, enabled: bool) -> Future:
        """Enable/disable three-color (red) mode live and force a full refresh.

        Mirrors apply_waveform_profile: flips the driver's three_color switch,
        forces the next refresh to re-run init(), and submits a full refresh so
        the current screen re-renders in the new mode without a reboot. The driver
        keeps the same waveform in both modes (three_color changes only the
        channel mapping, B/W -> 0x24 / red -> 0x26 on SSD1680, B/W -> 0x10 / red
        -> 0x13 on UC8151D), so turning the switch off restores the exact mono
        behavior. A no-op (already-completed Future) when the active driver
        predates ``apply_three_color``, so calling it is always harmless.

        Returns a Future that completes when the refresh finishes.
        """
        if not hasattr(self._epd, "apply_three_color"):
            future = Future()
            future.set_result("not-applicable")
            return future
        self._epd.apply_three_color(enabled)
        self._scheduler.force_reinit()
        return self.update(full=True, immediate=True)

    def set_batch_updates(self, enabled: bool) -> None:
        """Enable/disable update batching live, delegating to the scheduler.

        Unlike apply_waveform_profile / apply_three_color this needs no panel
        re-init or full refresh: it only changes how the scheduler folds future
        request bursts, so the next burst reflects the new setting.
        """
        self._scheduler.set_batch_updates(enabled)

    def add_widget(self, widget: Widget) -> Future:
        """Add a widget on top of the display stack and paint the result.
        
        If the widget has is_modal=True, it takes over the display and all other
        widgets are ignored until this widget is removed. An existing modal is
        parked (not stopped) and restored when this one is removed, so cancelling
        a countdown splash returns the waiting splash that was underneath.
        
        The widget should call request_update() when it's ready to be displayed.
        
        Args:
            widget: The widget to add
        
        Returns:
            Future that completes when the display is updated
        """
        self._attach_widget(widget)
        return self.update(full=False)
    
    def add_widget_at(self, widget: Widget, index: int) -> Future:
        """Add a widget at a specific position in the z-order stack and paint.
        
        Widgets are rendered in order, so index 0 is at the bottom (rendered first,
        may be obscured by others) and higher indices are on top.
        
        Args:
            widget: The widget to add
            index: Position in the stack. Clamped to valid range.
        
        Returns:
            Future that completes when the display is updated
        """
        self._attach_widget(widget, index=index)
        return self.update(full=False)
    
    def _attach_widget(self, widget: Widget, index: Optional[int] = None) -> None:
        """Register a widget in the stack WITHOUT painting the panel.
        
        Split out from add_widget so a screen transition can compose its entire
        widget stack before any frame reaches the panel -- see clear_widgets, which
        must not paint the half-built screen it leaves behind.
        
        If adding a modal widget when another modal is already present, the previous
        modal is parked (removed from the visible stack, not stopped) so that
        remove_widget can restore it. Only one modal is drawn.
        
        Args:
            widget: The widget to register
            index: Position in the z-order stack (0 = bottom). Clamped to the valid
                   range. None appends the widget on top.
        """
        if widget.is_modal:
            for existing in self._widgets:
                if existing.is_modal:
                    log.debug(
                        f"Manager._attach_widget() parking modal "
                        f"{existing.__class__.__name__} under {widget.__class__.__name__}"
                    )
                    self._widgets.remove(existing)
                    self._parked_modals.append(existing)
                    break  # Only one modal is visible
        
        # Pass scheduler and update callback to widget so it can trigger updates.
        # The callback is wrapped per widget so the widget's own refresh_priority
        # (and modal status) decides whether its updates refresh immediately or
        # defer to the clock/coalesced flush -- see _widget_update.
        widget.set_scheduler(self._scheduler)
        widget.set_update_callback(functools.partial(self._widget_update, widget))
        
        if index is None:
            self._widgets.append(widget)
        else:
            self._widgets.insert(max(0, min(index, len(self._widgets))), widget)
        
        if widget.is_modal:
            log.debug(f"Manager._attach_widget() added modal widget {widget.__class__.__name__} at index {self._widgets.index(widget)}")
    
    def set_background(self, shade: int = 0) -> None:
        """Set the background shade level using dithering.
        
        Creates or updates a BackgroundWidget that renders a dithered pattern
        to simulate grayscale on the 1-bit display.
        
        Args:
            shade: Grayscale level 0-16 (0=white, 8=50% gray, 16=black)
        """
        from ..background import BackgroundWidget
        
        if self._background is None:
            self._background = BackgroundWidget(self._epd.width, self._epd.height, self.update, shade)
        else:
            self._background.set_shade(shade)
    
    def clear_background(self) -> None:
        """Clear the background (revert to plain white)."""
        self._background = None
    
    def remove_widget(self, widget: Widget) -> Future:
        """Remove a widget from the display.

        Removing a modal restores the modal it displaced, if any, so a cancelled
        countdown returns the splash that was showing before it.
        
        Args:
            widget: The widget to remove
            
        Returns:
            Future that completes when the display is updated, or None if widget not found
        """
        if widget in self._widgets:
            try:
                widget.stop()
            except Exception as e:
                log.debug(f"Error stopping widget {widget.__class__.__name__} during remove: {e}")
            
            self._widgets.remove(widget)
            
            if widget.is_modal:
                log.debug(f"Manager.remove_widget() removed modal widget {widget.__class__.__name__}")
                if self._parked_modals:
                    restored = self._parked_modals.pop()
                    self._widgets.append(restored)
                    log.debug(
                        f"Manager.remove_widget() restored parked modal "
                        f"{restored.__class__.__name__}"
                    )
            else:
                log.debug(f"Manager.remove_widget() removed {widget.__class__.__name__}")
            
            return self.update(full=False)
        else:
            log.debug(f"Manager.remove_widget() widget {widget.__class__.__name__} not found")
            return None
    
    def clear_widgets(self, addStatusBar: bool = True) -> None:
        """Clear all widgets and background from the display, WITHOUT painting.
        
        Stops all widget background threads, drops refresh requests queued by those
        widgets, clears the widget list, any parked modals, and the background, and
        optionally seeds the fresh screen with a status bar.
        
        Deliberately paints nothing. This is the first half of a screen transition
        ("clear the old widgets, add the new ones") and the screen it leaves behind
        is empty by construction -- no caller ever wants that state shown. Painting
        it (which adding the status bar through add_widget used to do) submitted a
        frame containing nothing but the 16px status bar just before the frame with
        the real content. The scheduler only coalesces frames queued together, and
        building the next screen's widgets takes long enough that the empty frame is
        usually drawn first: on the device the screen visibly blanked between the
        boot splash and the main menu, which reads as a fault. The caller's following
        add_widget paints one frame that already contains the new content.
        
        Callers that want a dithered background should call set_background() after this.
        """
        log.debug(f"Manager.clear_widgets() called, clearing {len(self._widgets)} widgets")
        
        # Clear pending refresh requests first to prevent stale updates
        # from widgets that are about to be removed
        self._scheduler.clear_pending()
        
        # Stop all existing widgets before clearing to prevent background threads from continuing
        for widget in list(self._widgets) + list(self._parked_modals):
            try:
                widget.stop()
            except Exception as e:
                log.debug(f"Error stopping widget {widget.__class__.__name__} during clear: {e}")
        
        self._widgets.clear()
        self._parked_modals.clear()
        
        # Clear background to revert to plain white
        self._background = None
        
        if addStatusBar:
            self._attach_widget(StatusBarWidget(0, 0, self.update))
    
    def update(self, full: bool = False, immediate: bool = False,
               priority: bool = True) -> Future:
        """Update the display with current widget states.

        If any widget has is_modal=True, only that widget is rendered.
        Otherwise, all visible widgets are rendered.

        Refresh coordination (see refresh_policy.py). ``priority`` decides whether
        this request paints the panel now or is folded into a single later paint:

        - ``priority=True`` (the default, used by direct/external callers such as
          screen transitions and profile changes, and by the clock's heartbeat
          and time-sensitive overlays) renders and refreshes immediately.
        - ``priority=False`` (routine widget updates -- board, analysis, status,
          the clock's turn/state) only marks the framebuffer dirty. While a timed
          game's clock is running it rides the next clock tick; otherwise a single
          coalesced flush renders the whole burst once. This is what removes the
          per-event render burst and stops the running clock stuttering when other
          widgets change.

        The synchronous render path still has re-entrancy protection: if a child
        widget calls update() from within draw_on() during a render, that request
        is queued and replayed after the current render completes.

        Args:
            full: If True, force a full refresh instead of partial refresh.
            immediate: If True, wake scheduler immediately to bypass batching delay.
                      Use for time-sensitive UI like menu navigation.
            priority: If True, render now; if False, defer/coalesce per the policy.

        Returns:
            Future: completes when the refresh finishes (priority path), or a
            resolved placeholder for deferred/coalesced requests.
        """
        if full:
            log.debug("Manager.update() called with full=True (will cause flashing refresh)")

        if not self._initialized or self._shutting_down:
            future = Future()
            future.set_result("not-initialized")
            return future

        if priority:
            return self._render_now(full, immediate)

        # Routine update: record that the framebuffer needs rendering and let the
        # clock tick (clock-driven mode) or a single coalesced flush pick it up.
        with self._refresh_state_lock:
            self._dirty = True
            self._dirty_full = self._dirty_full or full
            action = decide_refresh_action(
                priority=False,
                defer_to_clock=self._defer_to_clock,
                flush_scheduled=self._flush_scheduled,
            )
            schedule_flush = action is RefreshAction.SCHEDULE_FLUSH
            if schedule_flush:
                self._flush_scheduled = True
        if schedule_flush:
            self._scheduler.submit_deferred(self._flush_deferred)
        future = Future()
        future.set_result("deferred")
        return future

    def _widget_update(self, widget: Widget, full: bool = False,
                       immediate: bool = False) -> Future:
        """Per-widget update entry point installed by add_widget().

        Maps a widget's update request onto update()'s ``priority`` flag: a modal
        or a widget that opts in via ``refresh_priority`` (the clock heartbeat and
        time-sensitive overlays) refreshes immediately; every other widget's
        routine change defers/coalesces so one event does not trigger a render
        per observing widget.
        """
        priority = bool(getattr(widget, "refresh_priority", False)) or widget.is_modal
        return self.update(full, immediate, priority=priority)

    def _render_now(self, full: bool = False, immediate: bool = False,
                    clock_source: bool = False) -> Future:
        """Render the whole widget stack and submit a refresh immediately.

        Folds any deferred dirty state into this render (a full-stack render
        includes pending routine changes), then renders under a lock so renders
        from different threads (the clock heartbeat vs an overlay) never run
        concurrently. Same-thread re-entrancy (a child calling update() during
        draw_on()) is queued and replayed once, not recursed.

        ``clock_source`` marks the submitted frame as originating from the clock
        tick so the scheduler will not let it interrupt an in-progress full
        refresh (see Scheduler._refresh_should_abort).
        """
        with self._refresh_state_lock:
            full = full or self._dirty_full
            self._dirty = False
            self._dirty_full = False

        with self._render_lock:
            if self._update_in_progress:
                self._pending_update = True
                self._pending_full = self._pending_full or full
                future = Future()
                future.set_result("queued")
                return future

            self._update_in_progress = True
            try:
                return self._do_update(full, immediate, clock_source)
            finally:
                self._update_in_progress = False
                if self._pending_update:
                    self._pending_update = False
                    pending_full = self._pending_full
                    self._pending_full = False
                    # Replay off-stack to avoid deep recursion.
                    self._scheduler.submit_deferred(
                        lambda: self._render_now(pending_full))

    def _flush_deferred(self) -> None:
        """Run the single coalesced flush for a burst of routine updates.

        Scheduled by the first routine update when not clock-driven; a whole
        synchronous burst of updates therefore collapses into this one render.
        Renders only if something is still dirty (a priority render in the
        interim may have already cleared it).
        """
        with self._refresh_state_lock:
            self._flush_scheduled = False
            dirty = self._dirty
        if dirty:
            self._render_now(full=False, immediate=False)

    def flush_now(self, full: bool = False) -> Future:
        """Render and refresh now -- the clock tick's heartbeat.

        Called once per tick while a timed game runs. In clock-driven mode it is
        the sole panel refresher: it renders every widget (picking up any routine
        changes deferred since the last tick) and submits one refresh, giving the
        clock a steady once-per-second cadence without other widgets preempting it.
        """
        if not self._initialized or self._shutting_down:
            return None
        return self._render_now(full, immediate=False, clock_source=True)

    def set_defer_to_clock(self, enabled: bool) -> None:
        """Enable/disable clock-driven refresh mode.

        While enabled (a timed game's clock is running), routine widget updates
        only mark the framebuffer dirty and are flushed by the clock's tick via
        flush_now(); priority updates (overlays, transitions) still refresh at
        once. Disabling it (clock paused/stopped, or untimed play) restores
        immediate coalesced refreshes and flushes any content deferred while it
        was enabled, so the screen is never left stale after the clock stops.
        """
        with self._refresh_state_lock:
            self._defer_to_clock = enabled
            needs_flush = (not enabled) and self._dirty
        if needs_flush:
            self._render_now(full=False, immediate=False)
    
    def _do_update(self, full: bool = False, immediate: bool = False,
                   clock_source: bool = False) -> Future:
        """Internal method that performs the actual update rendering.
        
        This should only be called from update() with the re-entrancy guard held.
        
        Args:
            full: If True, force a full refresh instead of partial refresh.
            immediate: If True, wake scheduler immediately to bypass batching delay.
            clock_source: True when this render is driven by the clock's tick
                heartbeat, so the scheduler tags the frame as non-interrupting (it
                must not abort an in-progress full/tri-color refresh).
        """
        # Get canvas and render background
        canvas = self._framebuffer.get_canvas()
        if self._background is not None:
            # Use dithered background widget
            self._background.draw_on(canvas, 0, 0)
        else:
            # Plain white background
            from PIL import ImageDraw
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((0, 0, self._epd.width, self._epd.height), fill=255)
        
        # Find if there's a modal widget
        modal_widget = None
        for widget in self._widgets:
            if widget.is_modal and widget.visible:
                modal_widget = widget
                break
        
        # Draw all visible non-modal widgets in z-order (first = bottom, last = top)
        for widget in self._widgets:
            if not widget.visible:
                continue
            if widget.is_modal:
                continue  # Modal drawn last, on top
            log.debug(f"Manager._do_update(): Rendering {widget.__class__.__name__} at ({widget.x}, {widget.y}) size {widget.width}x{widget.height}")
            widget.draw_on(canvas, widget.x, widget.y)
        
        # Draw modal widget last (on top of everything)
        if modal_widget:
            modal_widget.draw_on(canvas, modal_widget.x, modal_widget.y)
        
        # CRITICAL: Capture snapshot of framebuffer state at this exact moment
        # This ensures each update request carries its own image state, so rapid
        # updates display all intermediate states, not just the final one
        snapshot = self._framebuffer.snapshot(rotation=self.output_rotation())

        # Three-color mode: composite a parallel RED plane from the same widget
        # stack. Built only when the active driver reports three_color so a mono
        # panel pays zero cost and the mono scheduler path is never handed an
        # unexpected red image. The red canvas mirrors the B/W canvas geometry and
        # rotation; widgets that do not override render_red contribute nothing, so
        # an all-white (no-red) plane is produced when no highlight is active.
        red_snapshot = self._render_red_snapshot(modal_widget) if self._is_three_color() else None

        # Submit refresh with the captured snapshot and return Future
        # The on_refresh callback is invoked by Scheduler after display update
        return self._scheduler.submit(full=full, immediate=immediate, image=snapshot,
                                      red_image=red_snapshot, clock_source=clock_source)

    def _is_three_color(self) -> bool:
        """Whether the active driver is in three-color (red) mode.

        Read off the driver so the coordinator stays agnostic of which controller
        drives the panel; absent attribute (older drivers) means mono.
        """
        return bool(getattr(self._epd, "three_color", False))

    def _render_red_snapshot(self, modal_widget) -> Image.Image:
        """Composite the RED overlay plane for the current widget stack.

        Renders the same widgets, in the same z-order/modal precedence as the
        B/W plane, onto a fresh red-mask canvas (0 = red, 255 = not red). Returns
        a rotated snapshot matching the B/W snapshot so the driver can pack the
        two planes against identical geometry.
        """
        red_canvas = Image.new('1', (self._framebuffer.width, self._framebuffer.height), 255)

        if modal_widget:
            modal_widget.draw_red_on(red_canvas, modal_widget.x, modal_widget.y)
        else:
            for widget in self._widgets:
                if not widget.visible or widget.is_modal:
                    continue
                widget.draw_red_on(red_canvas, widget.x, widget.y)

        rotation = self.output_rotation()
        if rotation == 0:
            return red_canvas
        return red_canvas.rotate(-rotation, expand=False)
    
    def display_frame(self, image: Image.Image, red_image: Image.Image = None) -> Future:
        """Render an externally-produced frame, bypassing the widget stack.

        Injection point for frames produced outside the widget pipeline (e.g. the
        original-Centaur display-translation gateway, which reconstructs centaur's
        framebuffer and renders it through whatever panel is installed).

        The refresh is requested as **partial and batched** (``full=False,
        immediate=False``), not full+immediate. Original centaur drives its own
        panel with incremental partial updates -- reserving full refreshes for
        specific actions (its back/options buttons) -- and emits frames faster
        than an e-paper full refresh completes. Forcing a full, immediate refresh
        per frame made the panel flash on every update and thrash (each refresh
        'interrupted by newer data'). Partial avoids the full-refresh flash;
        batched lets the scheduler coalesce a rapid frame burst down to the latest
        frame. The scheduler establishes the white baseline on the first partial
        (its ``_baseline_established`` Clear()), so no priming full refresh is
        needed here. Trade-off: pure partial refreshing accumulates ghosting over
        time; mapping centaur's own full-refresh triggers to a full refresh is
        deferred follow-up work.

        The panel rotation is applied exactly as ``FrameBuffer.snapshot`` applies
        it (``rotate(-ROTATION)``), so an upright logical frame lands correctly on
        a rotated-mount panel. A ``red_image`` (mask: 0 = red, 255 = not red) is
        forwarded for three-color panels; pass ``None`` for mono frames.

        Args:
            image: PIL image in logical (un-rotated) panel coordinates. Mode is
                normalized by the driver's ``getbuffer`` when it is rendered.
            red_image: Optional RED-plane mask for three-color panels.

        Returns:
            The refresh ``Future`` from the scheduler, so callers can await paint.
        """
        rotation = self.output_rotation()
        frame = image if rotation == 0 else image.rotate(-rotation, expand=False)
        red = red_image
        if red is not None and rotation != 0:
            red = red.rotate(-rotation, expand=False)
        return self._scheduler.submit(full=False, immediate=False, image=frame, red_image=red)

    def cleanup(self, for_shutdown: bool = False) -> None:
        """Clean up display resources.
        
        Args:
            for_shutdown: If True, also puts display to sleep.
        """
        self.shutdown()
    
    def shutdown(self) -> None:
        """Shutdown the display."""
        if self._shutting_down:
            return
        
        self._shutting_down = True
        
        try:
            # Stop all widgets to allow cleanup of background threads and resources
            for widget in list(self._widgets) + list(self._parked_modals):
                try:
                    widget.stop()
                except Exception as e:
                    log.debug(f"Error stopping widget {widget.__class__.__name__}: {e}")
            self._parked_modals.clear()
            
            self._scheduler.stop()
            
            # Clear display to white before sleeping to leave it in a known state
            # try:
            #     self._epd.Clear()
            # except Exception:
            #     # If Clear() fails, try using display() with white image
            #     try:
            #         white_image = Image.new('1', (self._epd.width, self._epd.height), 255)
            #         white_buf = self._epd.getbuffer(white_image)
            #         self._epd.display(white_buf)
            #     except Exception:
            #         pass
            
            self._epd.sleep()
        except Exception as e:
            log.error(f"Error during shutdown: {e}")

    def release_hardware(self) -> None:
        """Fully release the panel hardware so a foreign process can own it.

        Used by the original-DGT-Centaur handoff. Stops the refresh scheduler (so
        no thread touches the panel after release), settles the panel into deep
        sleep, then closes the SPI fd AND releases the gpiozero RST/DC/BUSY lines
        via ``module_exit(cleanup=True)``.

        This differs from ``shutdown()``/``sleep()``, which call
        ``module_exit(cleanup=False)`` and leave the GPIO lines claimed. Leaving
        them claimed makes the original Centaur software's own panel driver
        collide with ours on ``/dev/spidev1.0`` and BCM 12/16/7: centaur's first
        frame shows but the panel never updates (board input still works because
        the serial port is released separately). Order matters -- the scheduler is
        stopped before SPI/GPIO are closed so a queued refresh cannot run against
        a closed device. The panel settle is best-effort: an unresponsive panel
        must not prevent the hardware from being freed.
        """
        self._shutting_down = True

        for widget in list(self._widgets) + list(self._parked_modals):
            try:
                widget.stop()
            except Exception as e:
                log.debug(f"Error stopping widget {widget.__class__.__name__}: {e}")
        self._parked_modals.clear()

        self._scheduler.stop()

        try:
            self._epd.idle_sleep()
        except Exception as e:
            log.debug(f"Panel settle before centaur handoff failed (continuing): {e}")

        epdconfig.module_exit(cleanup=True)

    def reacquire_hardware(self) -> None:
        """Take the panel back after :meth:`release_hardware` gave it away.

        The counterpart of the handoff release, used when the original Centaur
        software exits and Universal Chess must draw again -- the "Returning..."
        splash -- before the service restarts.

        The forced re-init is the load-bearing part. ``release_hardware`` settled
        the panel by calling ``epd.idle_sleep()`` directly rather than through
        the scheduler, so the scheduler's own ``_deep_asleep`` /
        ``_in_partial_mode`` state does not record that SPI and the GPIO lines
        were closed. Left alone, the next refresh would read that stale state,
        skip ``epd.init()`` -- the call that re-runs ``module_init()`` and
        reopens the device -- and write to closed hardware. Arming it before the
        scheduler thread starts avoids racing the first refresh.

        Lowering ``_shutting_down`` matters just as much: ``update()`` drops
        every draw while it is set, silently, so the panel would simply never
        change.
        """
        self._scheduler.force_reinit()
        self._scheduler.start()
        self._shutting_down = False
