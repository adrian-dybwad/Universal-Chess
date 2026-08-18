"""A remote abort or resign must end the board game and offer Seek / Lobby / Cancel.

Why these tests exist
---------------------
Lichess streams status ``aborted`` (and ``noStart``) when the opponent aborts
before the game counts. ``_check_game_status`` treated abort as no PGN result
and skipped the game-over callback, so the clocks kept running and the Seek
New Game menu never appeared. Resign did fire game-over, but only painted
GameOverWidget and never opened that menu.

How a regression manifests
--------------------------
``lichess_terminal_result('aborted')`` is None; ``_process_game_state`` does
not call the game-over callback; the session does not notify unfinished on
abort or resign.
"""

from unittest.mock import MagicMock

from universalchess.players.base import PlayerState
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess.player import (
    LichessPlayer,
    lichess_status_name,
    lichess_terminal_result,
)
from universalchess.players.lichess.session import LichessPlaySession
from universalchess.players.manager import PlayerManager


def test_lichess_status_name_accepts_string_and_object():
    """Board API NDJSON uses a string; converters may emit {name} or an enum.

    How the regression manifests: a dict status is stringified to a repr that
    is not in the terminal set, so abort is ignored.
    """
    assert lichess_status_name("aborted") == "aborted"
    assert lichess_status_name({"name": "aborted", "id": 4}) == "aborted"
    assert lichess_status_name(None) == ""
    assert lichess_status_name("started") == "started"


def test_aborted_has_an_unfinished_pgn_result():
    """Abort must produce a result so ChessGameState.is_game_over becomes true.

    How the regression manifests: result is None and the callback is skipped.
    """
    assert lichess_terminal_result("aborted") == ("*", "ABORTED")
    assert lichess_terminal_result("noStart") == ("*", "NOSTART")
    assert lichess_terminal_result({"name": "aborted"}) == ("*", "ABORTED")


def test_started_is_not_terminal():
    """A live gameState must not look like game over.

    How the regression manifests: started maps to a result and ends the game
    on the first stream event.
    """
    assert lichess_terminal_result("started") == (None, None)


def test_resign_uses_the_winner_field():
    """Resign still has a side that won; abort does not.

    How the regression manifests: winner is ignored and resign becomes '*'.
    """
    assert lichess_terminal_result("resign", winner="black") == ("0-1", "RESIGN")
    assert lichess_terminal_result("mate", winner="white") == ("1-0", "CHECKMATE")


def test_game_state_abort_fires_the_game_over_callback():
    """A gameState with status aborted must end the local game.

    How the regression manifests: ended stays empty because result was None.
    """
    player = LichessPlayer()
    ended = []
    player.set_game_over_callback(
        lambda result, termination, winner: ended.append(
            (result, termination, winner)
        )
    )
    player._process_game_state(
        {"type": "gameState", "moves": "", "status": "aborted"}
    )
    assert ended == [("*", "ABORTED", None)]
    assert player._state is PlayerState.STOPPED


def test_game_full_nested_abort_fires_the_game_over_callback():
    """gameFull carries status on the nested state object.

    How the regression manifests: only the outer dict is inspected, status is
    empty, and a game that ended before attach never calls game over.
    """
    player = LichessPlayer()
    ended = []
    player.set_game_over_callback(
        lambda result, termination, winner: ended.append((result, termination))
    )
    player._process_game_state(
        {
            "type": "gameFull",
            "white": {"name": "alice"},
            "black": {"name": "bob"},
            "state": {"moves": "", "status": "aborted"},
        }
    )
    assert ended == [("*", "ABORTED")]


def test_a_second_abort_event_does_not_notify_again():
    """The stream can repeat the terminal gameState.

    How the regression manifests: ended has two rows and the Seek menu is
    queued twice.
    """
    player = LichessPlayer()
    ended = []
    player.set_game_over_callback(
        lambda result, termination, winner: ended.append(result)
    )
    event = {"type": "gameState", "moves": "", "status": "aborted"}
    player._process_game_state(event)
    player._process_game_state(event)
    assert ended == ["*"]


def test_session_abort_asks_what_to_do_next():
    """Opponent abort must offer Lobby / Seek / Cancel, not sit on a live board.

    How the regression manifests: unfinished is never called, so the main loop
    never shows board_reset_rebuild_action.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    unfinished = []
    results = []
    session.attach(
        player_manager=PlayerManager(HumanPlayer(), remote),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda result, termination: results.append(
            (result, termination)
        ),
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        on_unfinished_game=lambda termination: unfinished.append(termination),
    )
    session._on_game_over("*", "ABORTED", None)
    assert results == [("*", "ABORTED")]
    assert unfinished == ["ABORTED"]


def test_session_resign_asks_what_to_do_next():
    """Opponent resign must offer Lobby / Seek / Cancel, headed with the reason.

    Why: abort already opened that menu; resign only painted GameOverWidget, so
    the next-game menu never appeared. How the regression manifests: unfinished
    stays empty on RESIGN.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    unfinished = []
    results = []
    session.attach(
        player_manager=PlayerManager(HumanPlayer(), remote),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda result, termination: results.append(
            (result, termination)
        ),
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        on_unfinished_game=lambda termination: unfinished.append(termination),
    )
    session._on_game_over("0-1", "RESIGN", "black")
    assert results == [("0-1", "RESIGN")]
    assert unfinished == ["RESIGN"]
