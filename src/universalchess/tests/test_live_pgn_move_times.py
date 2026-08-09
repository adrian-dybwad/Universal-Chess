"""Tests for time annotations in the live (in-memory) PGN.

``ChessGameService`` maintains a PGN tree incrementally as the game is played and
broadcasts it to web clients over SSE. It used to export with ``comments=False``
and carried no timing at all, so the PGN a user copied out of the live board view
disagreed with the one served by ``/getpgn`` for the same finished game.

The durations come from ``ChessGameState`` -- the same single measurement the
database stores -- rather than being measured again here, so the two exports
report the same number for the same move.
"""

import io

import pytest

pytest.importorskip("chess")

import chess
import chess.pgn

from universalchess.services.chess_game import ChessGameService
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.chess_game import reset_chess_game
from universalchess.state.time_control import TimeControl
from universalchess.tests.fake_clock import FakeMonotonic


@pytest.fixture
def live(monkeypatch):
    """A ChessGameService over a fresh game state with a hand-advanced clock.

    Broadcasting is stubbed out: these tests are about the PGN text, and the
    real broadcast opens a unix socket.
    """
    reset_chess_clock()
    state = reset_chess_game()
    clock = FakeMonotonic()
    state._now_monotonic = clock
    state.start_move_timing()

    monkeypatch.setattr(
        "universalchess.services.chess_game.broadcast_game_state",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "universalchess.services.chess_game.write_fen_log", lambda *a, **k: None)

    return ChessGameService(), state, clock


def _play(state, clock, moves):
    """Play (uci, think_seconds) pairs through the state."""
    for uci, seconds in moves:
        clock.advance(seconds)
        state.push_move(chess.Move.from_uci(uci))


def _nodes(game):
    return list(game.mainline())


def test_live_pgn_carries_elapsed_move_times(live):
    """Each move in the live PGN is annotated with the time it took.

    Why: this is the flagged gap -- the live export dropped comments entirely.
    How a regression manifests: an exporter built with comments=False yields
    movetext with no braces at all, so every reparsed emt is None.
    """
    service, state, clock = live

    _play(state, clock, [("e2e4", 4.0), ("e7e5", 9.0), ("g1f3", 12.0)])

    reparsed = chess.pgn.read_game(io.StringIO(service.get_pgn()))

    assert [node.emt() for node in _nodes(reparsed)] == [4, 9, 12]


def test_live_pgn_durations_match_the_state_measurement(live):
    """The annotation is the state's number, not one measured again here.

    Why: two independent measurements of the same interval would let the live
    PGN and the stored PGN disagree about the same move. How a regression
    manifests: the service anchors its own timer at construction, so the first
    move's duration differs from what the database records.
    """
    service, state, clock = live

    _play(state, clock, [("e2e4", 4.0), ("e7e5", 9.0)])

    reparsed = chess.pgn.read_game(io.StringIO(service.get_pgn()))
    expected = [round(ms / 1000) for ms in state.move_durations_ms]

    assert [node.emt() for node in _nodes(reparsed)] == expected


def test_untimed_game_gets_durations_but_no_clock(live):
    """With no time control the live PGN has [%emt] and no [%clk].

    Why: an untimed clock reads zero seconds for both sides, which would export
    as though both players had flagged on every move. How a regression
    manifests: "[%clk 0:00:00]" appears throughout a casual game.
    """
    service, state, clock = live

    _play(state, clock, [("e2e4", 4.0), ("e7e5", 9.0)])

    text = service.get_pgn()

    assert "%emt" in text
    assert "%clk" not in text
    assert "TimeControl" not in text or '[TimeControl "-"]' in text


def test_timed_game_annotates_the_moving_side_clock(live):
    """In a timed game the latest move carries that mover's remaining clock.

    Why: [%clk] is defined as the clock of the player who made the commented
    move, and the service reads a service that only knows the present. How a
    regression manifests: the value is read for the side now on turn, so every
    annotation reports the opponent's clock.
    """
    service, state, clock = live

    from universalchess.services.chess_clock import get_chess_clock_service

    service_clock = get_chess_clock_service()
    service_clock.configure(TimeControl.fischer_minutes(5, 0))
    service_clock.set_times(296, 300)

    _play(state, clock, [("e2e4", 4.0)])

    reparsed = chess.pgn.read_game(io.StringIO(service.get_pgn()))
    nodes = _nodes(reparsed)

    # White has just moved, so the annotation is White's remaining time.
    assert nodes[-1].clock() == 296
    assert reparsed.headers["TimeControl"] == "300"


def test_takeback_removes_the_annotation_with_the_move(live):
    """A retracted move's timing disappears along with the move.

    Why: the PGN tree and the duration list are both rewound on a takeback; if
    only one is, later moves are annotated with a neighbouring move's time.
    How a regression manifests: the mainline is two moves but three durations
    were consumed, shifting the second move's emt.
    """
    service, state, clock = live

    _play(state, clock, [("e2e4", 4.0), ("e7e5", 9.0)])
    state.pop_move()
    clock.advance(3.0)
    state.push_move(chess.Move.from_uci("c7c5"))

    reparsed = chess.pgn.read_game(io.StringIO(service.get_pgn()))
    nodes = _nodes(reparsed)

    assert [node.move.uci() for node in nodes] == ["e2e4", "c7c5"]
    assert [node.emt() for node in nodes] == [4, 3]


def test_live_movetext_stays_parseable_with_annotations(live):
    """Comments must not corrupt the movetext they are interleaved with.

    Why: a malformed comment swallows the moves after it. How a regression
    manifests: the reparsed mainline is shorter than the moves played, the final
    position does not match the board, or the moves survive while the comments
    they were interleaved with are lost.
    """
    service, state, clock = live

    _play(state, clock, [("e2e4", 4.0), ("e7e5", 9.0), ("g1f3", 12.0), ("b8c6", 5.0)])

    reparsed = chess.pgn.read_game(io.StringIO(service.get_pgn()))
    nodes = _nodes(reparsed)

    assert [node.move.uci() for node in nodes] == ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert [node.emt() for node in nodes] == [4, 9, 12, 5]
    assert reparsed.end().board().fen() == state.fen
