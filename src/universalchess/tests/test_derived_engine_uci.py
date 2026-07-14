"""Tests for the derived-engine UCI wrapper loop.

Background / why these tests exist
----------------------------------
The derived engines (Worstfish, Drawfish) run as their own UCI process that the
board launches via ``popen_uci``. ``uci_wrapper.run`` implements that UCI
protocol loop: it answers the handshake (advertising the engine's options),
tracks the board from ``position`` commands, applies ``setoption`` changes, and
on ``go`` asks the injected Stockfish for a multi-PV evaluation, applies the
engine's selection policy, and prints ``bestmove``.

Stockfish is the one external dependency, so it is injected as a fake here (the
boundary), and the RNG is injected so randomised behaviour is deterministic.
Everything else -- command parsing, option handling, board tracking, candidate
construction, ``bestmove`` output -- is real, and the loop is driven through its
public entry point with scripted stdin so the tests exercise exactly what a real
UCI GUI would send.
"""

import io
import random

import chess
import chess.engine
from chess.engine import Cp, Mate, PovScore

from universalchess.services.derived_engines.spec import SPECS
from universalchess.services.derived_engines.uci_wrapper import run

WORSTFISH = SPECS["worstfish"]
DRAWFISH = SPECS["drawfish"]


class FakeStockfish:
    """Stand-in for a Stockfish ``SimpleEngine`` at the analyse boundary.

    ``analyse`` returns a caller-supplied list of InfoDicts (mirroring
    python-chess's multipv shape: each entry has ``pv`` and ``score``) and
    records its calls so a test can assert whether/what was analysed.
    """

    def __init__(self, infos):
        self._infos = infos
        self.analyse_calls = []

    def analyse(self, board, limit, multipv=None):
        self.analyse_calls.append((board.fen(), limit, multipv))
        return self._infos


def _info(uci: str, score: chess.engine.Score, turn: chess.Color):
    """Build one multipv InfoDict for ``uci`` scored from ``turn``'s POV."""
    return {"pv": [chess.Move.from_uci(uci)], "score": PovScore(score, turn)}


def _drive(engine, spec, commands, rng=None):
    """Run the UCI loop over newline-joined ``commands`` and return its output."""
    in_stream = io.StringIO("\n".join(commands) + "\n")
    out_stream = io.StringIO()
    run(engine, spec, in_stream, out_stream, rng=rng)
    return out_stream.getvalue()


def test_uci_handshake_reports_name_and_uciok():
    """`uci` must yield an id name line and terminate with `uciok`.

    Why: a GUI/`popen_uci` blocks on `uciok` during startup; without it the
    engine never becomes ready. How it manifests: python-chess would time out
    loading the engine. The name line carries the engine's display identity.
    Worstfish advertises only Randomness (default 0), never AvoidCaptures.
    """
    engine = FakeStockfish(infos=[])
    output = _drive(engine, WORSTFISH, ["uci", "quit"])

    assert "id name Worstfish" in output
    assert "uciok" in output
    assert "option name Randomness type spin default 0 min 0 max 10" in output
    assert "AvoidCaptures" not in output  # capture-avoidance is Drawfish-only


def test_uci_handshake_advertises_drawfish_options():
    """Drawfish advertises Randomness (spin) and AvoidCaptures (check).

    Why: the Settings profile editor and level picker read an engine's options
    from this handshake; these lines are what make Drawfish configurable. How it
    manifests: a missing/misspelled option line (or wrong bounds/default) leaves
    the UI with nothing to edit or seeds the wrong default.
    """
    engine = FakeStockfish(infos=[])
    output = _drive(engine, DRAWFISH, ["uci", "quit"])

    assert "id name Drawfish" in output
    assert "option name Randomness type spin default 3 min 0 max 10" in output
    assert "option name AvoidCaptures type check default true" in output


def test_isready_reports_readyok():
    """`isready` must be answered with `readyok` (the per-command sync point).

    Why: python-chess sends `isready` after configuration and waits for
    `readyok`. How it manifests: a missing/misspelled reply hangs the loader.
    """
    engine = FakeStockfish(infos=[])
    output = _drive(engine, WORSTFISH, ["isready", "quit"])

    assert "readyok" in output


def test_go_applies_worst_policy_and_emits_bestmove():
    """On `go`, the worst-scoring move is analysed, selected, and emitted.

    Why: this is the end-to-end Worstfish behaviour through the protocol. How it
    manifests: if scores were passed to the policy without POV handling, or the
    wrong info field was read, bestmove would be e2e4 (+1.00) rather than the
    worst move d2d4 (-0.50).
    """
    infos = [
        _info("e2e4", Cp(100), chess.WHITE),
        _info("d2d4", Cp(-50), chess.WHITE),
        _info("g1f3", Cp(20), chess.WHITE),
    ]
    engine = FakeStockfish(infos)
    output = _drive(
        engine,
        WORSTFISH,
        ["position startpos", "go movetime 100", "quit"],
    )

    assert "bestmove d2d4" in output
    # The position was actually analysed (startpos, white to move), with multipv
    # covering every legal move so the policy sees the whole move list.
    assert len(engine.analyse_calls) == 1
    fen, _limit, multipv = engine.analyse_calls[0]
    assert fen == chess.Board().fen()
    assert multipv == 20  # 20 legal moves from the initial position


def test_go_applies_drawfish_policy_avoiding_mate_and_choosing_near_equal():
    """Drawfish (Randomness 0) excludes the mating move and emits the near-equal one.

    Why: verifies the Drawfish policy and `setoption` are wired through the loop.
    How it manifests: if mate exclusion were skipped, bestmove would be the
    Mate(1) move; if it maximised eval, it would pick the +3.00 move instead of
    +0.10; if `setoption` were ignored, the default Randomness (3) would make the
    choice non-deterministic and this assertion would flake.
    """
    infos = [
        _info("d1h5", Mate(1), chess.WHITE),
        _info("e2e4", Cp(300), chess.WHITE),
        _info("a2a3", Cp(10), chess.WHITE),
    ]
    engine = FakeStockfish(infos)
    output = _drive(
        engine,
        DRAWFISH,
        [
            "setoption name Randomness value 0",
            "position startpos",
            "go movetime 100",
            "quit",
        ],
    )

    assert "bestmove a2a3" in output


def test_go_drawfish_randomness_picks_within_the_most_equal_pool():
    """With Randomness 1 the bestmove is one of the two most-equal moves.

    Why: confirms the injected RNG and the option value reach the policy through
    the loop (not just in the pure policy tests). How it manifests: if the option
    or RNG were dropped, either it would always emit a2a3 (R treated as 0) or it
    could emit the far +3.00 move e2e4 (window miscomputed). A fixed seed keeps
    the test deterministic.
    """
    infos = [
        _info("a2a3", Cp(10), chess.WHITE),
        _info("b2b3", Cp(-20), chess.WHITE),
        _info("e2e4", Cp(300), chess.WHITE),
    ]
    engine = FakeStockfish(infos)
    output = _drive(
        engine,
        DRAWFISH,
        [
            "setoption name Randomness value 1",
            "position startpos",
            "go movetime 100",
            "quit",
        ],
        rng=random.Random(1),
    )

    assert ("bestmove a2a3" in output) or ("bestmove b2b3" in output)
    assert "bestmove e2e4" not in output  # the far-from-equal move is out of the pool


def test_position_with_moves_is_tracked_for_analysis():
    """`position startpos moves ...` updates the board handed to analyse.

    Why: the engine must analyse the CURRENT position, not the initial one, and
    the side-to-move POV flips after each ply. How it manifests: if moves were
    ignored, the analysed FEN would be the start position (white to move) and
    the POV conversion would be wrong for black.
    """
    infos = [_info("g8f6", Cp(0), chess.BLACK)]
    engine = FakeStockfish(infos)
    _drive(
        engine,
        WORSTFISH,
        ["position startpos moves e2e4", "go movetime 100", "quit"],
    )

    expected = chess.Board()
    expected.push_uci("e2e4")
    fen, _limit, _multipv = engine.analyse_calls[0]
    assert fen == expected.fen()  # black to move after 1.e4


def test_single_legal_move_is_played_without_analysis():
    """With exactly one legal move the engine plays it and skips analysis.

    Why: analysing a forced move wastes the whole time budget for no choice,
    and this is exactly the position that forces a win against Drawfish (its only
    move is a checkmate). The move must be emitted regardless of policy. How it
    manifests: if the short-circuit were missing, analyse would be called (and,
    for a real Stockfish, burn time); this asserts it is not.
    """
    # Black king g8 is in check from Ra8 along the 8th rank; its own pawns block
    # g7/h7 and f8/h8 stay attacked, leaving g8f7 as the ONLY legal move.
    forced_fen = "R5k1/6pp/8/8/8/8/8/7K b - - 0 1"
    engine = FakeStockfish(infos=[])
    output = _drive(
        engine,
        WORSTFISH,
        [f"position fen {forced_fen}", "go movetime 100", "quit"],
    )

    assert "bestmove g8f7" in output
    assert engine.analyse_calls == []  # forced move: no analysis performed
