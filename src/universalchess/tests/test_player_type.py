"""PlayerType is a move-source tag. It does not name providers.

Why these tests exist
---------------------
``PlayerType.LICHESS`` put a plugin name on the base class. The game only
needs to know whether a move is constructed on the board, computed locally,
or arrives from outside. A board-reset rebuild is a capability on the player
that is still attached to an external game, not a type named Lichess.

How a regression manifests
--------------------------
``LICHESS`` reappears on the enum, ``LichessPlayer.player_type`` is not
``REMOTE``, or Human/Engine report that they cannot continue in place.
"""

import chess

from universalchess.players.base import PlayerType
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer
from universalchess.players.manager import PlayerManager


def test_player_type_does_not_name_providers():
    """The enum is HUMAN, ENGINE, REMOTE — not Lichess or any other plugin.

    Failure: LICHESS (or another provider) is a member, so core still lists
    who exists.
    """
    assert {member.name for member in PlayerType} == {"HUMAN", "ENGINE", "REMOTE"}


def test_lichess_player_is_remote():
    """Lichess is one remote source, not its own PlayerType.

    Failure: player_type is still LICHESS (or HUMAN/ENGINE).
    """
    assert LichessPlayer().player_type is PlayerType.REMOTE


def test_human_does_not_require_rebuild_on_new_game():
    """A local human continues in place after a board-reset new game.

    Failure: Human reports True and the controller skips the in-place move
    request the way it must for an attached remote game.
    """
    assert HumanPlayer().requires_rebuild_on_new_game is False


def test_lichess_requires_rebuild_on_new_game():
    """A Lichess player is still attached to the remote game after a local reset.

    Failure: False, so EVENT_NEW_GAME requests a move against the abandoned
    stream while the board is at start.
    """
    assert LichessPlayer().requires_rebuild_on_new_game is True


def test_player_manager_rebuild_follows_either_side():
    """The manager is true when either slot cannot continue in place.

    Failure: Human vs Lichess reports False (only checking White) or Human vs
    Human reports True.
    """
    local = PlayerManager(HumanPlayer(), HumanPlayer())
    assert local.requires_rebuild_on_new_game is False
    remote_black = PlayerManager(HumanPlayer(), LichessPlayer())
    assert remote_black.requires_rebuild_on_new_game is True
    remote_white = PlayerManager(LichessPlayer(), HumanPlayer())
    assert remote_white.requires_rebuild_on_new_game is True


def test_human_remote_session_hooks_are_noops():
    """Core does not special-case a provider; Human ignores remote session hooks.

    Why: ProtocolManager and abort/leave used isinstance LichessPlayer. The
    default Player hooks must be safe to call on every slot.

    Failure: bind_remote_session / abort_remote_game / leave_remote_game raise
    or do not exist on Human.
    """
    human = HumanPlayer()
    human.bind_remote_session(
        clock_callback=lambda *_: None,
        game_info_callback=lambda *_: None,
        time_control_callback=lambda *_: None,
    )
    human.abort_remote_game()
    human.leave_remote_game()
    human.bind_board_cues(
        brain_hint=lambda *_: None,
        piece_squares_led=lambda *_: None,
        invalid_selection_flash=lambda *_: None,
    )
    assert human.help_key_result(chess.Board()) is None


def test_lichess_bind_remote_session_wires_clock_and_game_info():
    """A remote player accepts clock and game-info callbacks through the hook.

    Why: ProtocolManager imported LichessPlayer to call set_clock_callback.
    bind_remote_session is the capability; core must not name the plugin.

    Failure: callbacks are not stored, so GameManager never receives clock/info.
    """
    from universalchess.state.time_control import TimeControl

    player = LichessPlayer()
    clocks = []
    infos = []
    specs = []
    player.bind_remote_session(
        clock_callback=lambda w, b: clocks.append((w, b)),
        game_info_callback=lambda *args: infos.append(args),
        time_control_callback=lambda spec: specs.append(spec),
    )
    player._clock_callback(60, 45)
    player._game_info_callback("a", "1", "b", "2")
    spec = TimeControl.fischer_minutes(3, 2)
    player._time_control_callback(spec)
    assert clocks == [(60, 45)]
    assert infos == [("a", "1", "b", "2")]
    assert specs == [spec]


def test_player_manager_abort_and_leave_reach_only_remote_slot():
    """Abort/leave on the manager must hit the remote player, not require isinstance.

    Why: main aborted by looping players with isinstance LichessPlayer.

    Failure: Human is asked to abort (would raise if we later make that an
    error) or the Lichess slot's client is never called.
    """
    from unittest.mock import MagicMock

    remote = LichessPlayer()
    remote._game_id = "game-1"
    remote._client = MagicMock()
    manager = PlayerManager(HumanPlayer(), remote)
    manager.abort_remote_games()
    remote._client.board.abort_game.assert_called_once_with("game-1")
    remote._client.board.resign_game.assert_not_called()

    remote._client.reset_mock()
    remote._client.board.abort_game.side_effect = Exception("too late")
    manager.leave_remote_games()
    remote._client.board.abort_game.assert_called_once_with("game-1")
    remote._client.board.resign_game.assert_called_once_with("game-1")
