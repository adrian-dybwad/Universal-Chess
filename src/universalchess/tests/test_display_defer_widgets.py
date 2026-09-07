"""DisplayManager can skip the first widget paint so a waiting splash stays up.

Why these tests exist
---------------------
``DisplayManager.__init__`` always called ``_init_widgets``, which
``clear_widgets`` on the panel and adds the chess board. A Lichess seek splash
shown just before construction was therefore erased before it could paint.
``defer_widgets=True`` skips that first paint; ``show_game_widgets`` runs it
when the stream connects.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from universalchess.state.chess_clock import reset_chess_clock
from universalchess.services.chess_clock import ChessClockService
from universalchess.state.time_control import TimeControl


@pytest.fixture
def display_manager_factory():
    """Build a DisplayManager with widget/engine init stubbed (no panel).

    Returns ``(defer_widgets) -> (dm, init_mock)``. The ``_init_widgets`` patch
    stays active until the test returns so ``show_game_widgets`` is observable
    rather than hitting a real panel.
    """
    reset_chess_clock()
    service = ChessClockService()
    fake_game = SimpleNamespace(
        on_alert_clear=lambda *_a, **_k: None,
        set_position=lambda *_a, **_k: None,
    )
    import universalchess.managers.display as display_module
    from universalchess.managers.display import DisplayManager

    patches = [
        patch.object(DisplayManager, "_init_widgets"),
        patch.object(display_module, "_load_widgets"),
        patch.object(display_module, "get_chess_game", return_value=fake_game),
        patch.object(display_module, "get_chess_clock_service", return_value=service),
    ]
    started = [p.start() for p in patches]
    try:
        def factory(defer_widgets: bool = False, **kwargs):
            dm = DisplayManager(
                time_control_spec=TimeControl.sudden_death_minutes(5),
                defer_widgets=defer_widgets,
                **kwargs,
            )
            return dm, started[0]

        yield factory
    finally:
        for p in reversed(patches):
            p.stop()


def test_default_construction_paints_game_widgets(display_manager_factory):
    """A normal game start still paints the board in the constructor.

    Why: deferral is opt-in for Lichess seek. Human/engine games must not wait
    for a later reveal or the panel stays on the previous menu.

    How the regression manifests: if defer_widgets defaults True (or the
    constructor skips _init_widgets unconditionally), init is not called here.
    """
    _dm, init = display_manager_factory(defer_widgets=False)
    init.assert_called_once()


def test_defer_widgets_skips_init_until_show_game_widgets(display_manager_factory):
    """A deferred DisplayManager must not clear the panel until revealed.

    Why: that first _init_widgets is what wiped "Waiting for game". Construction
    with defer_widgets=True must leave the splash; show_game_widgets paints the
    board when the stream connects.

    How the regression manifests: if __init__ still calls _init_widgets, init
    has been called before show_game_widgets; if show_game_widgets is a no-op,
    the second assert fails.
    """
    dm, init = display_manager_factory(defer_widgets=True)
    init.assert_not_called()
    dm.show_game_widgets()
    init.assert_called_once()


def test_face_far_end_rotates_the_whole_panel_180(display_manager_factory, monkeypatch):
    """When the seated player is at the far end, menus must turn with the board.

    Why: square remapping only turned the chess diagram. Abort, takeback, and
    next-game menus still painted for the original seat. Diagram-from-black
    (Black on player 1) must not rotate the panel; a human on player 2 must.

    How the regression manifests: face_far_end never asks for content rotation
    180, board_from_black rotates it, or leaving the game leaves it at 180.
    """
    import universalchess.managers.display as display_module

    panel = MagicMock()
    monkeypatch.setattr(display_module.board, "display_manager", panel)
    dm, _ = display_manager_factory(defer_widgets=True)
    panel.set_content_rotation.assert_called_with(0)
    dm.set_orientation(board_from_black=True, face_far_end=False)
    panel.set_content_rotation.assert_called_with(0)
    dm.set_orientation(board_from_black=False, face_far_end=True)
    panel.set_content_rotation.assert_called_with(180)
    dm.set_orientation(board_from_black=False, face_far_end=False)
    panel.set_content_rotation.assert_called_with(0)


def test_constructor_face_far_end_rotates_without_diagram_from_black(
    display_manager_factory, monkeypatch
):
    """A far-side human at game start must turn the panel before the first paint.

    Why: local Human on player 2 sets orientation in DisplayManager.__init__,
    not later via set_orientation. If only the setter rotated, that game would
    stay facing player 1.

    How the regression manifests: face_far_end=True at construction asks for
    rotation 0, or flip_board=True asks for 180.
    """
    import universalchess.managers.display as display_module

    panel = MagicMock()
    monkeypatch.setattr(display_module.board, "display_manager", panel)
    display_manager_factory(defer_widgets=True, face_far_end=True)
    panel.set_content_rotation.assert_called_with(180)
    display_manager_factory(defer_widgets=True, flip_board=True)
    panel.set_content_rotation.assert_called_with(0)
