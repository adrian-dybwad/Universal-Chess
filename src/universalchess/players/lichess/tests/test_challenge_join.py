"""Joining a challenge picked in the lobby: accept an incoming one, wait for an outgoing one.

Why these tests exist
---------------------
Selecting an OUT row in the lobby's Challenges list streamed the challenge id as
if it were a game. A challenge is not a game until the opponent accepts it, so
Lichess answered ``404 No such game``, the stream thread ended, and the board was
left in a local game: pieces moved on the board were accepted locally and never
reached Lichess. Observed on the board for challenge XGvWJB2I -- "Accepting
challenge..." with no accept POST, then
``GET /api/board/game/stream/XGvWJB2I -> 404``.

How a regression manifests
--------------------------
An outgoing challenge POSTs accept, or starts a game stream before the opponent
has accepted; an incoming challenge stops POSTing accept; or the wait attaches
whatever other game the account has running instead of the challenge that was
picked.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.players.base import PlayerState
from universalchess.players.lichess import (
    LichessGameMode,
    LichessPlayer,
    LichessPlayerConfig,
)

CHALLENGE_ID = "ch-1"
OTHER_GAME_ID = "another-game"


def _player(direction: str, challenge_id: str = CHALLENGE_ID) -> LichessPlayer:
    """A CHALLENGE-mode player with the network and thread starts stubbed out."""
    player = LichessPlayer(
        LichessPlayerConfig(
            mode=LichessGameMode.CHALLENGE,
            challenge_id=challenge_id,
            challenge_direction=direction,
        )
    )
    player._username = "BoardAccount"
    player._client = MagicMock()
    player._start_game_stream = MagicMock()
    player._listen_for_match = MagicMock()
    return player


def test_accepting_an_incoming_challenge_joins_the_game_it_creates():
    """An incoming challenge is joined by accepting it; the game keeps its id.

    How the regression manifests: no accept is POSTed (the game never starts on
    Lichess) or no stream is opened (the board never receives the opponent).
    """
    player = _player("in")

    assert player._start_challenge() is True

    player._client.challenges.accept.assert_called_once_with(CHALLENGE_ID)
    assert player._game_id == CHALLENGE_ID
    player._start_game_stream.assert_called_once()


def test_an_outgoing_challenge_waits_instead_of_streaming_a_game_that_does_not_exist():
    """The opponent has not accepted yet, so there is no game to stream.

    This is the reported failure: streaming the challenge id got 404 No such
    game and the board played on locally. How the regression manifests:
    _start_game_stream is called, _game_id is set to the challenge id, or the
    challenge is accepted as though the board were the one challenged.
    """
    player = _player("out")

    assert player._start_challenge() is True

    player._client.challenges.accept.assert_not_called()
    player._start_game_stream.assert_not_called()
    assert player._game_id is None
    player._listen_for_match.assert_called_once()


def test_the_outgoing_wait_attaches_the_game_when_the_opponent_accepts():
    """An accepted challenge keeps its id, so gameStart for that id is the game.

    How the regression manifests: the gameStart event is ignored and the board
    waits forever although the opponent has accepted.
    """
    player = _player("out")
    player._start_challenge()
    # Nothing is joined by the start itself: until this event arrives the
    # challenge is still an offer, and streaming its id answers 404.
    assert player._game_id is None

    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": CHALLENGE_ID}}
    )

    assert player._game_id == CHALLENGE_ID
    player._start_game_stream.assert_called_once()


@pytest.mark.parametrize(
    "attach",
    [
        pytest.param(
            lambda player: player._handle_incoming_event(
                {"type": "gameStart", "game": {"id": OTHER_GAME_ID}}
            ),
            id="game_start_event",
        ),
        pytest.param(
            lambda player: player._try_attach_ongoing_game(),
            id="ongoing_poll",
        ),
    ],
)
def test_the_outgoing_wait_ignores_a_game_that_is_not_the_chosen_challenge(attach):
    """Only the challenge that was picked may be joined.

    The account can already have other games running (correspondence, a game on
    the phone). Attaching one of those would put the board in a game the Human
    did not choose. How the regression manifests: _game_id becomes the unrelated
    game and its stream starts.
    """
    player = _player("out")
    player._client.games.get_ongoing.return_value = [{"gameId": OTHER_GAME_ID}]
    player._start_challenge()

    attach(player)

    assert player._game_id is None
    player._start_game_stream.assert_not_called()

    player._client.games.get_ongoing.return_value = [
        {"gameId": OTHER_GAME_ID},
        {"gameId": CHALLENGE_ID},
    ]
    assert player._try_attach_ongoing_game() is True
    assert player._game_id == CHALLENGE_ID
    player._start_game_stream.assert_called_once()


def test_a_refused_accept_reports_an_error_instead_of_starting_a_stream():
    """A challenge that expired or was cancelled cannot be joined.

    How the regression manifests: the stream starts for an id Lichess has no
    game for, and the board silently becomes a local game.
    """
    player = _player("in")
    player._client.challenges.accept.side_effect = RuntimeError("HTTP 404")

    assert player._start_challenge() is False

    player._start_game_stream.assert_not_called()
    assert player._game_id is None
    assert player.state == PlayerState.ERROR


def test_a_join_without_a_challenge_id_is_refused():
    """Nothing to accept and nothing to wait for.

    How the regression manifests: accept is POSTed to /api/challenge//accept, or
    the wait listeners run with no id and attach an unrelated game.
    """
    player = _player("in", challenge_id="")

    assert player._start_challenge() is False

    player._client.challenges.accept.assert_not_called()
    player._listen_for_match.assert_not_called()
