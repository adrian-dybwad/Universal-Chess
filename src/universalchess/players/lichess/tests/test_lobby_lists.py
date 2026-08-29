"""Pure lobby list and web-join helpers shared by the board and the web card.

Why these tests exist
---------------------
Ongoing/Challenges on the web must show the same rows the board menu builds.
A truncated Lichess payload (missing gameId, missing challenge id) must not
become a joinable row. Web start params must map to the same _lichess_join
stash the board lobby writes, or a seek would start instead of the selected
game.
"""

from universalchess.players.lichess.lobby import (
    challenge_summaries,
    lichess_join_from_web_params,
    ongoing_game_summaries,
)
from universalchess.players.lichess.player import LichessGameMode


def test_ongoing_game_summaries_use_game_id_and_drop_empty():
    """Each row is id/opponent/rating/color/fen; a row without a game id is dropped.

    Why: the web list posts that id as game_id, and the lobby paints ``fen``
    so the pieces can be set up before Join. How a regression manifests: a
    blank id is listed, gameId is ignored, or fen/lastMove are omitted so the
    list cannot show the position.
    """
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    rows = ongoing_game_summaries(
        [
            {
                "gameId": "g1",
                "opponent": {"username": "Bob", "rating": 1500},
                "color": "white",
                "fen": after_e4,
                "lastMove": "e2e4",
                "isMyTurn": False,
            },
            {"opponent": {"username": "NoId"}},
            {
                "game_id": "g2",
                "opponent": {"username": "Cara"},
                "color": "black",
            },
            {
                "gameId": "g3",
                "opponent": {"name": "Dana"},
                "color": "White",
            },
        ]
    )
    assert rows == [
        {
            "id": "g1",
            "opponent": "Bob",
            "rating": 1500,
            "color": "white",
            "fen": after_e4,
            "lastMove": "e2e4",
            "isMyTurn": False,
        },
        {
            "id": "g2",
            "opponent": "Cara",
            "rating": "",
            "color": "black",
            "fen": "",
            "lastMove": "",
            "isMyTurn": False,
        },
        {
            "id": "g3",
            "opponent": "Dana",
            "rating": "",
            "color": "white",
            "fen": "",
            "lastMove": "",
            "isMyTurn": False,
        },
    ]


def test_ongoing_game_summaries_empty_and_none():
    """No games (or a None payload) is an empty list, not an error.

    Why: the board returns to the lobby when the account has none. Regression:
    None raises, or a placeholder row is invented.
    """
    assert ongoing_game_summaries([]) == []
    assert ongoing_game_summaries(None) == []


def test_challenge_summaries_incoming_then_outgoing_and_drop_empty_id():
    """Incoming first, then outgoing; a challenge without id is omitted.

    Why: the board list is IN then OUT and the web join uses direction:id.
    How a regression manifests: order flips, or a blank id is listed.
    """
    rows = challenge_summaries(
        {
            "in": [
                {"id": "c1", "challenger": {"name": "Ann", "rating": 1400}},
                {"challenger": {"name": "NoId"}},
            ],
            "out": [
                {"id": "c2", "destUser": {"name": "Bo", "rating": 1600}},
            ],
        }
    )
    assert rows == [
        {"id": "c1", "direction": "in", "name": "Ann", "rating": 1400},
        {"id": "c2", "direction": "out", "name": "Bo", "rating": 1600},
    ]


def test_lichess_join_from_web_params_new_ongoing_challenge():
    """new / ongoing / challenge map to the lobby stash; bad payloads are None.

    Why: the board command and the Flask start route share this parser. A
    missing game_id must not become ONGOING with an empty id (that seeks).
    """
    assert lichess_join_from_web_params({"mode": "new"}) == {
        "mode": LichessGameMode.NEW,
        "game_id": "",
        "challenge_id": "",
        "challenge_direction": "in",
    }
    assert lichess_join_from_web_params({"mode": "ongoing", "game_id": " abc "}) == {
        "mode": LichessGameMode.ONGOING,
        "game_id": "abc",
        "challenge_id": "",
        "challenge_direction": "in",
    }
    assert lichess_join_from_web_params(
        {"mode": "challenge", "challenge_id": "c1", "challenge_direction": "out"}
    ) == {
        "mode": LichessGameMode.CHALLENGE,
        "game_id": "",
        "challenge_id": "c1",
        "challenge_direction": "out",
    }
    assert lichess_join_from_web_params({"mode": "ongoing"}) is None
    assert lichess_join_from_web_params({"mode": "challenge", "challenge_id": "c1", "challenge_direction": "side"}) is None
    assert lichess_join_from_web_params({"mode": "nope"}) is None
    assert lichess_join_from_web_params({}) is None
