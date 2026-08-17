"""Tests for the Positions submenu game-in-progress confirmation.

Setting up a board position discards any resumable game in progress. The menu
must confirm first and, only on confirm, record the running game as aborted
(via the injected abort_game callback) before setting up the new position. These
tests drive handle_positions_menu with a scripted show_menu so the confirm/cancel
branches and break propagation are covered without the e-paper or a real game.
"""

import pytest

from universalchess.menus.positions_menu import (
    POSITION_UNAVAILABLE_WITH_LICHESS,
    handle_positions_menu,
    position_unavailable_with_lichess,
)


POSITIONS = {
    "test": {
        "start_pos": ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", None),
    }
}


class _ScriptedShowMenu:
    """Returns queued results in order; records each menu it was shown."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def __call__(self, entries, initial_index=0):
        self.calls += 1
        return self._results.pop(0)


class _Board:
    SOUND_WRONG_MOVE = 0

    def beep(self, *_a, **_k):
        pass


def _find_index(entries, key):
    for i, e in enumerate(entries):
        if e.key == key:
            return i
    return 0


def _run(
    results,
    *,
    in_progress,
    started,
    aborted_calls,
    started_calls,
    lichess_as_player=False,
    alerts=None,
):
    """Drive the menu with scripted results and record callback invocations."""
    show_menu = _ScriptedShowMenu(results)

    def start_from_position(fen, name, hint):
        started_calls.append((fen, name, hint))
        return started

    def abort_game():
        aborted_calls.append(True)

    def show_alert(message):
        if alerts is not None:
            alerts.append(message)

    return handle_positions_menu(
        load_positions_config=lambda: POSITIONS,
        start_from_position=start_from_position,
        show_menu=show_menu,
        find_entry_index=_find_index,
        board=_Board(),
        log=__import__("logging").getLogger("test"),
        last_position_category_index_ref=[0],
        last_position_index_ref=[0],
        last_position_category_ref=[None],
        is_game_in_progress=lambda: in_progress,
        abort_game=abort_game,
        lichess_as_player=lambda: lichess_as_player,
        show_alert=show_alert,
    )


def test_confirm_aborts_then_sets_up_position():
    """Confirming must abort the running game once, then set up the position.

    Guards the core requirement: the in-progress game is recorded as aborted
    (abort_game called) and the chosen position is then set up. A regression
    that set up the position without aborting (or aborted twice) is caught by
    the exact call counts.
    """
    aborted, started = [], []
    # category -> position -> confirm
    result = _run(
        ["test", "start_pos", "confirm"],
        in_progress=True,
        started=True,
        aborted_calls=aborted,
        started_calls=started,
    )

    assert result is True
    assert len(aborted) == 1
    assert started == [(POSITIONS["test"]["start_pos"][0], "Start Pos", None)]


def test_cancel_keeps_running_game_and_does_not_set_up():
    """Cancelling must not abort the game nor set up the position.

    The player backed out, so the running game must survive untouched. The
    decline returns to the category loop; we then back out (BACK) to end the
    menu. Asserts neither callback fired, which a regression treating cancel as
    confirm would violate.
    """
    aborted, started = [], []
    # category -> position -> cancel -> (loop) category BACK
    result = _run(
        ["test", "start_pos", "cancel", "BACK"],
        in_progress=True,
        started=True,
        aborted_calls=aborted,
        started_calls=started,
    )

    assert result is False
    assert aborted == []
    assert started == []


def test_no_confirmation_when_no_game_in_progress():
    """With no game in progress, selecting a position must skip confirmation.

    A spurious confirm prompt would be a UX regression. Only two menus (category,
    position) should be shown and abort_game must never be called. The scripted
    show_menu has no confirm entry, so an unexpected extra show_menu call would
    raise IndexError here.
    """
    aborted, started = [], []
    result = _run(
        ["test", "start_pos"],
        in_progress=False,
        started=True,
        aborted_calls=aborted,
        started_calls=started,
    )

    assert result is True
    assert aborted == []
    assert started == [(POSITIONS["test"]["start_pos"][0], "Start Pos", None)]


def test_break_result_in_confirm_propagates_without_aborting():
    """A break result (e.g. client connect) during confirm must propagate.

    If a client connects (or PLAY is pressed) while the confirm dialog is up,
    the menu must return that break result so the caller can start/resume play,
    and must NOT abort the running game. Asserts the break result is returned
    and abort_game never fired.
    """
    aborted, started = [], []
    result = _run(
        ["test", "start_pos", "CLIENT_CONNECTED"],
        in_progress=True,
        started=True,
        aborted_calls=aborted,
        started_calls=started,
    )

    assert result == "CLIENT_CONNECTED"
    assert aborted == []
    assert started == []


@pytest.mark.parametrize(
    "p1, p2, blocked",
    [
        ("human", "engine", False),
        ("human", "human", False),
        ("human", "lichess", True),
        ("lichess", "human", True),
        ("engine", "lichess", True),
    ],
)
def test_position_unavailable_with_lichess_when_a_slot_is_lichess(p1, p2, blocked):
    """A custom FEN cannot be a Lichess live game; any Lichess slot blocks setup.

    Why: Lichess seek/challenge/ongoing always start from the opening. Starting
    a position while a slot is Lichess would seek a different game than the
    board shows. How a regression manifests: human/engine is blocked, or a
    Lichess slot is allowed.
    """
    assert position_unavailable_with_lichess(p1, p2) is blocked


def test_lichess_player_shows_alert_and_does_not_start():
    """Selecting a position with Lichess as a player must alert and stay put.

    Why: the Positions menu would otherwise call start_from_position, which
    still builds a Lichess seek. How a regression manifests: start_from_position
    runs, abort_game runs (if a game is in progress), or the alert copy changes.
    """
    aborted, started, alerts = [], [], []
    result = _run(
        ["test", "start_pos", "BACK"],
        in_progress=True,
        started=True,
        aborted_calls=aborted,
        started_calls=started,
        lichess_as_player=True,
        alerts=alerts,
    )

    assert result is False
    assert alerts == [POSITION_UNAVAILABLE_WITH_LICHESS]
    assert aborted == []
    assert started == []
