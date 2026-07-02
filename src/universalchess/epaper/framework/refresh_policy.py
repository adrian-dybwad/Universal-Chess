"""Pure decision logic for when a display update refreshes the panel.

The e-paper panel is a single, slow shared resource (a partial refresh is
hundreds of milliseconds and the whole widget stack funnels through one
scheduler). Two independent problems arise when every widget refreshes the panel
whenever its own content changes:

1. Redundant renders. One logical event (e.g. a move) pokes several observing
   widgets (board, analysis, clock), and each independently triggers a full
   render + refresh. The scheduler coalesces the *panel* refreshes but not the
   *renders*, so the CPU does the work several times in a synchronous burst.

2. Clock contention. While a timed game's clock is counting, that render/refresh
   burst competes with the clock's once-per-second refresh on the single panel,
   so the clock visibly pauses/stutters at exactly the moments other widgets
   change (a move, the engine indicating its move).

This module encodes the policy that resolves both, as a pure function so it can
be tested without any hardware:

- Priority updates (the clock's own heartbeat tick, and time-sensitive overlays
  such as check/queen/hint alerts, modals and screen transitions) always refresh
  immediately.
- While the clock is the sole refresher (a timed game running), routine updates
  are deferred: they only mark the framebuffer dirty and ride the clock's next
  tick, which renders the whole stack once (picking up the prepped content).
- Otherwise (untimed, or the clock paused/stopped) routine updates are coalesced:
  the first schedules a single flush and the rest fold into it, so a burst
  renders once rather than N times.

The Manager owns the mutable dirty/scheduled flags and performs the actual
rendering; this function only decides which of the three actions to take.
"""

from __future__ import annotations

from enum import Enum, auto


class RefreshAction(Enum):
    """What the Manager should do for a single ``update`` request."""

    #: Render the whole widget stack and submit a panel refresh now.
    RENDER_NOW = auto()
    #: Mark the framebuffer dirty and wait for the clock tick to flush it.
    DEFER_TO_CLOCK = auto()
    #: Mark dirty and schedule the single coalesced flush (this is the first
    #: routine update of a burst; later ones in the same burst return DEFER).
    SCHEDULE_FLUSH = auto()
    #: Mark dirty; a coalesced flush is already scheduled, so do nothing else.
    DEFER = auto()


def decide_refresh_action(priority: bool, defer_to_clock: bool,
                          flush_scheduled: bool) -> RefreshAction:
    """Decide how a single ``update`` request should refresh the panel.

    Args:
        priority: True for the clock's heartbeat tick and time-sensitive overlays
            (alerts, modals, transitions) that must appear immediately.
        defer_to_clock: True while a timed game's clock is running and is the sole
            refresher, so routine updates ride the next tick instead of refreshing.
        flush_scheduled: True when a coalesced flush has already been scheduled for
            the current burst of routine updates (only meaningful when
            ``defer_to_clock`` is False).

    Returns:
        The :class:`RefreshAction` the Manager should perform. Priority always
        renders now. Otherwise, in clock-driven mode the update defers to the
        tick; in normal mode the first update of a burst schedules the flush and
        subsequent ones defer into it.
    """
    if priority:
        return RefreshAction.RENDER_NOW
    if defer_to_clock:
        return RefreshAction.DEFER_TO_CLOCK
    if flush_scheduled:
        return RefreshAction.DEFER
    return RefreshAction.SCHEDULE_FLUSH
