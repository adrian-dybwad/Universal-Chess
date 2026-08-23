"""Timed games show the e-paper clock even when Show Clock is off.

Why these tests exist
---------------------
Show Clock hides the untimed turn indicator. The same flag also hid the clock
in a timed game, so remaining time vanished and the layout still reserved a
blank band. DisplayManager must add the clock widget visible for any timed
game, and leave it off only for an untimed game with Show Clock off.

How a regression manifests
--------------------------
``clock_widget.hide()`` is called (or the widget is never added) when
``show_clock=False`` and the control is timed, so the board shows no countdown.
"""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from universalchess.services.chess_clock import ChessClockService
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.time_control import TimeControl


@contextmanager
def painted_display(*, timed: bool, show_clock: bool):
    """Paint game widgets with the clock stubbed; yields ``(panel, clock)``.

    ``_reload_display_settings`` is a no-op so the constructor's show_clock
    value is the one _init_widgets sees (otherwise Settings.read would overwrite
    it from whatever centaur.ini is on disk).
    """
    reset_chess_clock()
    service = ChessClockService()
    fake_game = SimpleNamespace(
        on_alert_clear=lambda *_a, **_k: None,
        set_position=lambda *_a, **_k: None,
        refresh_alerts=lambda: None,
    )
    panel = MagicMock()
    clock = MagicMock()
    spec = (
        TimeControl.sudden_death_minutes(5)
        if timed
        else TimeControl.sudden_death_minutes(0)
    )

    import universalchess.managers.display as display_module
    from universalchess.managers.display import DisplayManager

    with ExitStack() as stack:
        stack.enter_context(patch.object(display_module, "_load_widgets"))
        stack.enter_context(
            patch.object(display_module, "get_chess_game", return_value=fake_game)
        )
        stack.enter_context(
            patch.object(
                display_module, "get_chess_clock_service", return_value=service
            )
        )
        stack.enter_context(
            patch.object(display_module.board, "display_manager", panel)
        )
        stack.enter_context(
            patch.object(display_module, "_ChessBoardWidget", return_value=MagicMock())
        )
        stack.enter_context(
            patch.object(display_module, "_ChessClockWidget", return_value=clock)
        )
        stack.enter_context(
            patch.object(
                display_module, "_GameAnalysisWidget", return_value=MagicMock()
            )
        )
        stack.enter_context(
            patch.object(display_module, "_MoveListWidget", return_value=MagicMock())
        )
        stack.enter_context(
            patch.object(
                display_module, "_CoachTextWidget", return_value=MagicMock()
            )
        )
        stack.enter_context(
            patch.object(display_module, "_AlertWidget", return_value=MagicMock())
        )
        stack.enter_context(
            patch.object(display_module, "_GameOverWidget", return_value=MagicMock())
        )
        stack.enter_context(patch.object(DisplayManager, "_reload_display_settings"))
        stack.enter_context(patch.object(DisplayManager, "_reload_chess_sprites"))
        dm = DisplayManager(
            time_control_spec=spec,
            show_clock=show_clock,
            analysis_mode=False,
            defer_widgets=True,
        )
        dm.show_game_widgets()
        try:
            yield panel, clock
        finally:
            service.stop()
            reset_chess_clock()


def test_timed_game_adds_clock_visible_when_show_clock_is_off():
    """A timed game must paint the clock even if Show Clock is off.

    Failure: hide() is called, or add_widget never receives the clock, so
    remaining time is missing from the panel.
    """
    with painted_display(timed=True, show_clock=False) as (panel, clock):
        panel.add_widget.assert_any_call(clock)
        clock.hide.assert_not_called()


def test_untimed_game_omits_clock_when_show_clock_is_off():
    """Show Clock off in an untimed game must not add the turn indicator.

    Failure: the clock widget is added anyway, so turning Show Clock off no
    longer frees the band for an untimed game.
    """
    with painted_display(timed=False, show_clock=False) as (panel, clock):
        assert all(
            call.args[0] is not clock for call in panel.add_widget.call_args_list
        )
        clock.hide.assert_not_called()
