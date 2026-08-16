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
from unittest.mock import patch

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
        def factory(defer_widgets: bool = False):
            dm = DisplayManager(
                time_control_spec=TimeControl.sudden_death_minutes(5),
                defer_widgets=defer_widgets,
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
