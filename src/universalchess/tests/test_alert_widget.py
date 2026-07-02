"""Tests for AlertWidget CHECK / QUEEN alert refresh behavior.

Background / why these tests exist
----------------------------------
The AlertWidget shows either a CHECK alert or a YOUR QUEEN threat alert. These
can replace one another with no intervening hide() - e.g. white's queen is
threatened (QUEEN shown), then on the next move black gives check (CHECK should
show). The widget caches its rendered sprite, and the base Widget.show() only
invalidates/refreshes on a hidden->visible transition. A content switch while the
widget is already visible therefore left the stale cached sprite on screen,
producing the observed "Black has me in check, but YOUR QUEEN is displayed" bug.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.alert_widget import AlertWidget
from universalchess.state.chess_game import ChessGameState

# White king on e1 is in check from the black knight on c2 (knight attacks e1).
# Not checkmate, so the in-check state is stable. Taken from a real game log of
# the reported bug.
IN_CHECK_FEN = "3r2nr/1p1k1pbp/p2pp1p1/2p5/P1P1P3/2N3Pq/1PnP1P1N/R1B1KQ1R w KQ - 4 16"


def _make_widget():
    """Build an AlertWidget with a mock game state and a recording update callback.

    Returns (widget, updates) where updates is a list appended to on every
    display-update request.
    """
    game_state = MagicMock()
    updates = []

    def update_cb(full=False, immediate=False):
        updates.append((full, immediate))
        return None

    widget = AlertWidget(0, 144, 128, 40, update_cb, game_state=game_state)
    return widget, updates


def test_switch_queen_to_check_while_visible_invalidates_and_updates():
    """Switching QUEEN -> CHECK while already visible must re-render and refresh.

    Why: this is the exact reported regression. The QUEEN alert is on screen,
    then a move puts the king in check; the alert type changes while the widget
    stays visible.

    How the regression manifests: before the fix, super().show() saw the widget
    was already visible and did nothing, so _cached_sprite kept the stale QUEEN
    image and no update was requested - the panel showed YOUR QUEEN during check.
    """
    widget, updates = _make_widget()

    widget.show_queen_threat(False, 23, 5)
    assert widget.visible is True
    assert widget._alert_type == AlertWidget.ALERT_QUEEN

    # Simulate the Manager having rendered and cached the QUEEN sprite.
    widget._cached_sprite = Image.new('1', (widget.width, widget.height), 255)
    updates.clear()

    widget.show_check(False, 10, 4)

    assert widget._alert_type == AlertWidget.ALERT_CHECK
    assert widget._cached_sprite is None, (
        "stale QUEEN sprite must be invalidated when switching to CHECK"
    )
    assert len(updates) >= 1, (
        "switching alert content while visible must request a display update"
    )


def test_switch_check_to_queen_while_visible_invalidates_and_updates():
    """The reverse switch (CHECK -> QUEEN while visible) must also refresh.

    Why: symmetric to the reported bug; escaping check into a position where the
    queen is hanging must replace CHECK with YOUR QUEEN rather than leave the
    cached CHECK sprite on screen.
    """
    widget, updates = _make_widget()

    widget.show_check(False, 10, 4)
    widget._cached_sprite = Image.new('1', (widget.width, widget.height), 255)
    updates.clear()

    widget.show_queen_threat(False, 23, 5)

    assert widget._alert_type == AlertWidget.ALERT_QUEEN
    assert widget._cached_sprite is None
    assert len(updates) >= 1


def test_hint_defaults_to_white_mover_and_stores_notation_text():
    """show_hint keeps the formatted text and defaults the mover to White.

    Why: DisplayManager now formats the hint in the selected notation and passes
    the mover color for figurine piece art; callers that omit it (and existing
    behavior) must default to white art rather than crash.
    """
    widget, _ = _make_widget()

    widget.show_hint("e2e4", 12, 28)

    assert widget._alert_type == AlertWidget.ALERT_HINT
    assert widget._hint_text_value == "e2e4"
    assert widget._hint_white_side is True


def test_hint_renders_figurine_text_without_a_sprite_sheet():
    """A figurine hint renders (falling back to letters) instead of blanking.

    Why: figurine is the default notation, so the hint text can contain a glyph
    the bundled font cannot draw. render() must composite/letter-fallback the
    glyph via move_render rather than leave the panel blank or raise.

    How the regression manifests: if the figurine branch were missing, the
    canvas would stay all-white (extrema (255, 255)) because the glyph draws as
    nothing.
    """
    widget, _ = _make_widget()

    # Black knight-move hint: figurine glyph plus destination square.
    widget.show_hint("\u2658f3", 6, 21, white_side=False)
    assert widget._hint_white_side is False

    img = Image.new("1", (widget.width, widget.height), 255)
    widget.render(img)

    assert img.getextrema() == (0, 255)  # move drawn, not a blank panel


def _make_widget_for_state(state):
    """Build an AlertWidget observing the given ChessGameState.

    Returns (widget, updates). Mirrors how DisplayManager constructs a fresh
    AlertWidget (hidden by default) bound to the singleton game state.
    """
    updates = []

    def update_cb(full=False, immediate=False):
        updates.append((full, immediate))
        return None

    widget = AlertWidget(0, 144, 128, 40, update_cb, game_state=state)
    return widget, updates


def test_refresh_alerts_shows_check_on_freshly_built_widget():
    """A widget created while a check is already active must show CHECK on refresh.

    Why: when a transient UI (king-lift resign menu, kings-in-center menu) is
    cancelled, DisplayManager._init_widgets() rebuilds every widget, producing a
    brand-new AlertWidget that starts hidden. The check/threat alert is only ever
    raised by push_move()/reset(); rebuilding mid-check therefore silently drops
    it ("remove and replace a piece, the check alert goes away" bug). The rebuild
    path must re-derive the alert from the authoritative ChessGameState via
    refresh_alerts().

    How the regression manifests: without refresh_alerts() re-emitting the check,
    the freshly built widget stays hidden (visible False / _alert_type None) even
    though the position is still in check.
    """
    state = ChessGameState()
    state.set_position(IN_CHECK_FEN)
    assert state.is_check is True

    # Fresh widget (as produced by a rebuild) starts hidden and unaware of check.
    widget, _ = _make_widget_for_state(state)
    assert widget.visible is False
    assert widget._alert_type is None

    state.refresh_alerts()

    assert widget.visible is True, "rebuilt widget must show the still-active check"
    assert widget._alert_type == AlertWidget.ALERT_CHECK


def test_refresh_alerts_hides_when_no_check_or_threat():
    """refresh_alerts() on a quiet position must clear a stale visible alert.

    Why: rebuilding widgets when there is no check/threat (the common case) must
    not leave an alert showing. refresh_alerts() routes through the same
    no-alert -> alert_clear path used after a move.

    How the regression manifests: if refresh_alerts() failed to fire alert_clear,
    a widget left visible from a prior state would remain on screen with no
    underlying threat.
    """
    state = ChessGameState()  # starting position: no check, no threat
    widget, _ = _make_widget_for_state(state)

    # Force the widget visible as if a prior alert had been shown.
    widget.show_check(False, 10, 4)
    assert widget.visible is True

    state.refresh_alerts()

    assert widget.visible is False
    assert widget._alert_type is None
