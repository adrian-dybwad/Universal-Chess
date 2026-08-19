"""Lichess remaining must tick with the browser from the moment it arrives.

Why these tests exist
---------------------
The Board API snapshot is remaining at that instant. Lichess starts White's
clock when the game starts. The board applied that snapshot, then left the
clock stopped until the first turn event, and painting the deferred widgets
reseeding the spec's initial time. The e-paper stayed at 30:00 while the
browser counted down -- a growing offset until a later gameState snapped it
(and the widget paint could wipe that snap too).

How a regression manifests
--------------------------
After ``set_clock``, ``is_running`` is False, or remaining 1785 becomes 1800
when the widgets are built.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from universalchess.managers.game.game_manager import GameManager
from universalchess.services.chess_clock import ChessClockService
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.chess_game import reset_chess_game
from universalchess.state.time_control import TimeControl
from universalchess.utils.led import LedCallbacks


def _noop_led() -> LedCallbacks:
    return LedCallbacks(
        from_to=lambda *a, **k: None,
        array=lambda *a, **k: None,
        single=lambda *a, **k: None,
        off=lambda *a, **k: None,
        from_to_hint=lambda *a, **k: None,
        array_hint=lambda *a, **k: None,
        array_fast=lambda *a, **k: None,
        from_to_fast=lambda *a, **k: None,
        single_fast=lambda *a, **k: None,
    )


@pytest.fixture
def game_manager():
    """A GameManager on a fresh clock, with the countdown thread reaped."""
    reset_chess_game()
    reset_chess_clock()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    try:
        yield manager
    finally:
        manager._clock_service.stop()
        manager._stop_event.set()
        reset_chess_clock()


def test_remote_remaining_starts_the_timed_clock(game_manager):
    """Lichess remaining is a live snapshot; the board must tick from it.

    How the regression manifests: is_running stays False, so White's time
    freezes at the snapshot while the browser counts down.
    """
    game_manager._clock_service.configure(TimeControl.sudden_death_minutes(30))
    game_manager.set_clock(1785, 1800)

    assert game_manager._clock_service.get_times() == (1785, 1800)
    assert game_manager._clock_service.is_running is True


def test_remote_remaining_does_not_start_an_untimed_clock(game_manager):
    """Unlimited correspondence has no countdown.

    How the regression manifests: start() sets running True and a thread
    would tick a zero clock to flag.
    """
    game_manager._clock_service.configure(TimeControl.sudden_death_minutes(0))
    game_manager.set_clock(0, 0)

    assert game_manager._clock_service.is_running is False


def test_remote_remaining_does_not_restart_a_finished_game(game_manager):
    """A mate/resign gameState still carries wtime/btime.

    How the regression manifests: the countdown starts again on a finished
    game and the e-paper ticks after game-over.
    """
    from universalchess.state import get_chess_game

    game_manager._clock_service.configure(TimeControl.sudden_death_minutes(5))
    get_chess_game().set_result("1-0", "checkmate")
    game_manager.set_clock(0, 120)

    assert game_manager._clock_service.get_times() == (0, 120)
    assert game_manager._clock_service.is_running is False


def test_deferred_widget_paint_keeps_lichess_remaining():
    """show_game_widgets must not replace remaining with the spec's initial.

    Remaining is applied while the clock is still stopped (the waiting splash
    holds the panel). _init_widgets reseeding from the spec turned 29:45 into
    30:00.

    How the regression manifests: white_time is 1800 after the paint.
    """
    reset_chess_clock()
    service = ChessClockService()
    fake_game = SimpleNamespace(
        on_alert_clear=lambda *_a, **_k: None,
        set_position=lambda *_a, **_k: None,
        refresh_alerts=lambda: None,
    )
    panel = MagicMock()
    widget = MagicMock()

    import universalchess.managers.display as display_module
    from universalchess.managers.display import DisplayManager

    with patch.object(display_module, "_load_widgets"), \
         patch.object(display_module, "get_chess_game", return_value=fake_game), \
         patch.object(display_module, "get_chess_clock_service", return_value=service), \
         patch.object(display_module.board, "display_manager", panel), \
         patch.object(display_module, "_ChessBoardWidget", return_value=widget), \
         patch.object(display_module, "_ChessClockWidget", return_value=widget), \
         patch.object(display_module, "_GameAnalysisWidget", return_value=widget), \
         patch.object(display_module, "_MoveListWidget", return_value=widget), \
         patch.object(display_module, "_CoachTextWidget", return_value=widget), \
         patch.object(display_module, "_AlertWidget", return_value=widget), \
         patch.object(display_module, "_GameOverWidget", return_value=widget), \
         patch.object(DisplayManager, "_reload_display_settings"), \
         patch.object(DisplayManager, "_reload_chess_sprites"):
        dm = DisplayManager(
            time_control_spec=TimeControl.sudden_death_minutes(30),
            defer_widgets=True,
            analysis_mode=False,
        )
        service.set_times(1785, 1800)
        dm.show_game_widgets()

    assert service.get_times() == (1785, 1800)
    service.stop()
    reset_chess_clock()
