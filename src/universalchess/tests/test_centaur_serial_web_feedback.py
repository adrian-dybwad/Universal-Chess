"""Tests for the piece-in-hand overlay mapping.

Pin the display-only contract: lift highlights the source, place clears it, and
nothing else (key events, undecodable squares) touches the overlay. A regression
here would either lose the piece-in-hand highlight or, worse, publish a spurious
pending move that misleads the web while the authoritative game state is owned by
the UCI proxy.
"""

from universalchess.services.centaur_serial.decoder import KeyEvent, PieceEvent
from universalchess.services.centaur_serial.web_feedback import PieceInHandTracker


def test_lift_publishes_source_and_place_clears():
    """Lift publishes the source square; the following place clears the overlay.

    Why this test exists: this is the whole feature -- highlight while in hand,
    clear on placement. Regression manifests as the highlight not appearing (no
    lift publish) or sticking after placement (no clear).
    """
    published = []
    tracker = PieceInHandTracker(published.append)

    tracker.observe(PieceEvent(action="lift", field=52, square="e2"))
    tracker.observe(PieceEvent(action="place", field=36, square="e4"))

    assert published == ["e2", None]


def test_second_lift_moves_highlight_without_a_place():
    """A lift while another piece is already in hand moves the highlight.

    Why this test exists: captures/adjustments can produce two lifts before a
    place; the overlay should follow the latest lifted square rather than freeze
    or clear. Regression manifests as the highlight staying on the first square.
    """
    published = []
    tracker = PieceInHandTracker(published.append)

    tracker.observe(PieceEvent(action="lift", field=52, square="e2"))
    tracker.observe(PieceEvent(action="lift", field=36, square="e4"))

    assert published == ["e2", "e4"]


def test_key_events_do_not_touch_the_overlay():
    """Key events are ignored by the overlay tracker.

    Why this test exists: the tap delivers both piece and key events to a single
    callback; keys must not publish pending moves. Regression manifests as a
    button press clearing or setting the highlight.
    """
    published = []
    tracker = PieceInHandTracker(published.append)

    tracker.observe(KeyEvent(button="BACK", code=0x01, is_down=True))

    assert published == []


def test_undecodable_square_is_skipped():
    """An event whose square did not decode publishes nothing.

    Why this test exists: a corrupt field byte yields square=None; publishing it
    would highlight a fabricated square. Regression manifests as a None-square
    lift still calling publish.
    """
    published = []
    tracker = PieceInHandTracker(published.append)

    tracker.observe(PieceEvent(action="lift", field=200, square=None))

    assert published == []
