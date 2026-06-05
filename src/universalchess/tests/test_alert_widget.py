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
