"""The board shows no clock times while a connected app keeps the clock.

Why these tests exist
---------------------
The Millennium (ChessLink), Pegasus and Chessnut protocols carry no clock
command, so a connected app such as HIARCS runs a clock the board cannot read.
The board's own clock is paused for the session, and its frozen times on the
e-paper and the web were read as the game's clock even though they disagreed
with the app's. ``ChessClockState.kept_by_app`` records that the app keeps the
clock; the e-paper widget shows the turn and "clock on app" instead of times,
and the board -> web clock broadcast carries the flag so the web does the same.

How a regression manifests
--------------------------
Frozen times are drawn on the e-paper during a session (the drawn texts contain
the remaining time), the flag never reaches the web (the broadcast's
``kept_by_app`` is False), or setting the flag does not repaint the widget (no
state-change notification).
"""

from unittest.mock import MagicMock

import pytest
from PIL import Image

from universalchess.clock_format import format_clock_time
from universalchess.epaper.chess_clock import ChessClockWidget
from universalchess.epaper.text import TextWidget
from universalchess.i18n import t
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.chess_game import reset_chess_game
from universalchess.state.players import reset_players_state

REMAINING_SECONDS = 300
WIDGET_WIDTH = 128
WIDGET_HEIGHT = 72


@pytest.fixture
def clock_state():
    """A fresh, timed clock state with both sides on REMAINING_SECONDS."""
    reset_chess_game()
    reset_players_state()
    state = reset_chess_clock()
    state.set_timed_mode(True)
    state.set_times(REMAINING_SECONDS, REMAINING_SECONDS)
    yield state
    reset_chess_clock()


def _drawn_texts(monkeypatch, timed_mode):
    """Render a ChessClockWidget once and return the text of every drawn TextWidget."""
    drawn = []
    original_draw_on = TextWidget.draw_on

    def recording_draw_on(self, *args, **kwargs):
        if self.text:
            drawn.append(self.text)
        return original_draw_on(self, *args, **kwargs)

    monkeypatch.setattr(TextWidget, "draw_on", recording_draw_on)
    widget = ChessClockWidget(
        0, 0, WIDGET_WIDTH, WIDGET_HEIGHT,
        update_callback=MagicMock(),
        timed_mode=timed_mode,
    )
    try:
        widget.render(Image.new("1", (WIDGET_WIDTH, WIDGET_HEIGHT), 255))
    finally:
        widget.stop()
    return drawn


def test_setting_kept_by_app_notifies_once_per_change_and_reset_clears_it(clock_state):
    """The flag must repaint observers on a change, and a reset must clear it.

    The e-paper widget repaints from on_state_change, so a silent setter would
    leave the frozen times on screen until some unrelated change. Re-setting the
    same value must not notify, or every session hook call would refresh the
    panel. A reset (new clock) must not inherit a session's flag.

    Regression: notifications is not [True, False], or kept_by_app is still
    True after reset().
    """
    notifications = []
    clock_state.on_state_change(lambda: notifications.append(clock_state.kept_by_app))

    clock_state.set_kept_by_app(True)
    clock_state.set_kept_by_app(True)
    clock_state.set_kept_by_app(False)
    assert notifications == [True, False]

    clock_state.set_kept_by_app(True)
    clock_state.reset()
    assert clock_state.kept_by_app is False


@pytest.mark.parametrize("timed_mode", [True, False], ids=["timed", "untimed"])
def test_widget_shows_turn_and_clock_on_app_instead_of_times(
    clock_state, monkeypatch, timed_mode
):
    """While the app keeps the clock, the panel shows whose turn it is, not times.

    In a timed game the widget used to draw both sides' frozen remaining time.
    In an untimed game it drew the turn and the player name; the note replaces
    the name there too, so both layouts say where the clock is.

    Regression: the drawn texts contain the remaining time, or lack the note.
    """
    clock_state.set_kept_by_app(True)

    assert _drawn_texts(monkeypatch, timed_mode) == [
        t("clock.turn_white"),
        t("clock.on_app"),
    ]


def test_widget_draws_both_times_when_the_board_keeps_the_clock(clock_state, monkeypatch):
    """Without a session the timed layout is unchanged.

    Guards the other side of the switch: the flag must default to False and the
    timed layout must still draw each side's remaining time.

    Regression: the remaining time is drawn fewer than two times, or the note
    appears outside a session.
    """
    drawn = _drawn_texts(monkeypatch, timed_mode=True)

    assert drawn.count(format_clock_time(REMAINING_SECONDS)) == 2
    assert t("clock.on_app") not in drawn


@pytest.mark.parametrize("kept_by_app", [True, False])
def test_board_app_clock_broadcast_carries_kept_by_app(clock_state, monkeypatch, kept_by_app):
    """The board -> web clock snapshot must say whether the app keeps the clock.

    The web hides its times only from this field; the board app's broadcaster is
    the one place that reads the clock state into the snapshot.

    Regression: the captured kept_by_app differs from the clock state's.
    """
    from universalchess.app import board_app
    from universalchess.services import game_broadcast

    captured = []
    monkeypatch.setattr(
        game_broadcast,
        "broadcast_clock_status",
        lambda *args, **kwargs: captured.append(kwargs.get("kept_by_app")),
    )
    clock_state.set_kept_by_app(kept_by_app)

    board_app._broadcast_clock_status()

    assert captured == [kept_by_app]
