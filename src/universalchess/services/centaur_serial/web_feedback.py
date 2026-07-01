"""Map decoded piece events to a piece-in-hand overlay for the web UI.

The serial tap can see a piece leave the board (LIFT) before it lands (PLACE).
This module turns that into the ``pending_move`` overlay the web already
understands: the source square is highlighted while a piece is in hand, and the
highlight clears once it is placed. The completed move itself is NOT published
here -- the authoritative position/PGN is owned by the UCI proxy's
:class:`CentaurStatePublisher`; this overlay is display-only and must never alter
the recorded game.

The mapping is pure (the publish side effect is injected) so it is unit-testable
and free of web/broadcast coupling. It is deliberately conservative: only lift
and place transition the overlay, key events are ignored, and an event whose
square could not be decoded is skipped rather than publishing a bogus highlight.
"""

from __future__ import annotations

from typing import Callable, Optional

from universalchess.services.centaur_serial.decoder import PieceEvent


class PieceInHandTracker:
    """Translate lift/place events into ``pending_move`` overlay updates.

    On LIFT the lifted square is published as the pending source; on PLACE the
    overlay is cleared (the real move renders via the proxy's own broadcast). A
    second lift before a place simply moves the highlight to the newly lifted
    square, which keeps the overlay honest during captures/adjustments without
    trying to reconstruct the move (that is the proxy's job).

    Args:
        publish_pending: Called with the pending source square (algebraic) on
            lift, or None to clear on place. Injected so the web/broadcast side
            effect stays in the application layer.
    """

    def __init__(self, publish_pending: Callable[[Optional[str]], None]) -> None:
        self._publish_pending = publish_pending
        self._source: Optional[str] = None

    def observe(self, event: object) -> None:
        """Update the overlay from one decoded event (non-piece events ignored)."""
        if not isinstance(event, PieceEvent):
            return
        if event.square is None:
            # A field that did not decode to a real square: skip rather than
            # publish a fabricated highlight.
            return
        if event.action == "lift":
            self._source = event.square
            self._publish_pending(event.square)
        elif event.action == "place":
            self._source = None
            self._publish_pending(None)
