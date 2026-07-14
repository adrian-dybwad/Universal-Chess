"""Tests for the in-process policy engine player (Worstfish / Drawfish).

Background / why these tests exist
----------------------------------
Worstfish and Drawfish used to run as a separate UCI subprocess that opened its
OWN Stockfish. On the low-RAM board that meant a second Stockfish, a cold start,
and a ~40-130s first move. `PolicyEnginePlayer` instead runs the selection
policy in-process on the app's shared pooled Stockfish. These tests pin the
three seams that make that correct and safe:

* it acquires Stockfish (not the derived-engine shim), so the registry shares
  one pooled engine;
* it never pushes its policy options (Randomness/AvoidCaptures) onto the shared
  Stockfish (they are not Stockfish options and would disturb other consumers);
* it computes the move by a multi-PV analyse + the derived policy, reading the
  user's option values from the saved `.uci` section.

The engine handle is the external boundary and is faked; everything else
(option coercion, candidate construction, policy selection) is the real code.
"""

import random

import chess
import chess.engine
from chess.engine import Cp, Mate, PovScore

from universalchess.players import policy_engine
from universalchess.players.engine import EnginePlayerConfig
from universalchess.players.policy_engine import PolicyEnginePlayer
from universalchess.services.derived_engines.spec import SPECS


class FakeHandle:
    """Stand-in for an EngineHandle at the analyse/configure boundary.

    Records analyse and configure calls so tests can assert the shared engine is
    analysed (once, multi-PV over all legal moves) and NEVER configured with
    policy options.
    """

    def __init__(self, infos):
        self._infos = infos
        self.analyse_calls = []
        self.configure_calls = []

    def analyse(self, board, limit, multipv=None):
        self.analyse_calls.append((board.fen(), limit, multipv))
        return self._infos

    def configure(self, options):
        self.configure_calls.append(options)


def _info(uci: str, score: chess.engine.Score, turn: chess.Color):
    """Build one multipv InfoDict for ``uci`` scored from ``turn``'s POV."""
    return {"pv": [chess.Move.from_uci(uci)], "score": PovScore(score, turn)}


def _player(engine_name: str, uci_options=None, rng_seed=None) -> PolicyEnginePlayer:
    """Build a PolicyEnginePlayer for a derived engine with given saved options."""
    config = EnginePlayerConfig(
        name=engine_name, color=chess.WHITE, engine_name=engine_name
    )
    player = PolicyEnginePlayer(config, SPECS[engine_name])
    player._uci_options = dict(uci_options or {})
    if rng_seed is not None:
        player._rng = random.Random(rng_seed)
    return player


def test_resolve_engine_path_returns_stockfish(monkeypatch):
    """The player must back onto Stockfish, not the derived-engine shim.

    Why: acquiring Stockfish's path is what makes the registry hand back the one
    pooled, already-warm Stockfish (no second process). How it manifests: if this
    returned the shim path (as the base class does), the registry would spawn the
    subprocess + a second Stockfish again -- the exact regression this feature
    removes.
    """
    monkeypatch.setattr(policy_engine, "resolve_stockfish_path", lambda: "/usr/games/stockfish")
    player = _player("worstfish")
    assert str(player._resolve_engine_path()) == "/usr/games/stockfish"


def test_resolve_engine_path_none_when_stockfish_missing(monkeypatch):
    """Missing Stockfish resolves to None so start() reports unavailability.

    Why: the base start() treats a None path as "engine not found"; fabricating a
    path would defer the failure into a confusing registry error at play time.
    """
    monkeypatch.setattr(policy_engine, "resolve_stockfish_path", lambda: "")
    assert _player("worstfish")._resolve_engine_path() is None


def test_configure_handle_does_not_touch_shared_engine():
    """Policy options must never be pushed onto the shared Stockfish.

    Why: Randomness/AvoidCaptures are not Stockfish options; configuring the
    pooled engine with them would be dropped at best and disturb other consumers
    (analysis, coach) at worst. How it manifests: if the base _configure_handle
    ran, configure_calls would be non-empty.
    """
    handle = FakeHandle(infos=[])
    player = _player("drawfish", uci_options={"Randomness": "5", "AvoidCaptures": "false"})
    player._configure_handle(handle)
    assert handle.configure_calls == []


def test_ponder_is_disabled_for_policy_engine():
    """Ponder is forced off so start() shares the pooled Stockfish.

    Why: a policy engine does not use play()/go-ponder, and leaving ponder on
    would make start() acquire a DEDICATED Stockfish, defeating the sharing that
    is the whole point. How it manifests: ponder left True would spawn a private
    second Stockfish per game.
    """
    config = EnginePlayerConfig(
        name="worstfish", color=chess.WHITE, engine_name="worstfish", ponder=True
    )
    player = PolicyEnginePlayer(config, SPECS["worstfish"])
    assert player._engine_config.ponder is False


def test_worstfish_compute_move_plays_worst_via_multipv():
    """Worstfish analyses every legal move (multi-PV) and plays the worst.

    Why: this is the end-to-end in-process behaviour on the shared engine. How it
    manifests: without POV handling or with a single-line search it would play
    the best move e2e4 (+1.00) instead of the worst d2d4 (-0.50); the multipv
    assertion guards that all legal moves are offered to the policy.
    """
    infos = [
        _info("e2e4", Cp(100), chess.WHITE),
        _info("d2d4", Cp(-50), chess.WHITE),
        _info("g1f3", Cp(20), chess.WHITE),
    ]
    handle = FakeHandle(infos)
    player = _player("worstfish", uci_options={"Randomness": "0"})

    move = player._compute_move(handle, chess.Board())

    assert move == chess.Move.from_uci("d2d4")
    assert len(handle.analyse_calls) == 1
    _fen, _limit, multipv = handle.analyse_calls[0]
    assert multipv == 20  # every legal move from the start position


def test_drawfish_compute_move_avoids_mate_and_steers_to_equality():
    """Drawfish (Randomness 0) excludes the mate and plays the near-equal move.

    Why: verifies the Drawfish policy runs in-process with the saved option
    value. How it manifests: skipping mate exclusion would play the Mate(1) move;
    maximising eval would play +3.00; ignoring the option would use the default
    Randomness (3) and flake.
    """
    infos = [
        _info("d1h5", Mate(1), chess.WHITE),
        _info("e2e4", Cp(300), chess.WHITE),
        _info("a2a3", Cp(10), chess.WHITE),
    ]
    handle = FakeHandle(infos)
    player = _player("drawfish", uci_options={"Randomness": "0"})

    move = player._compute_move(handle, chess.Board())

    assert move == chess.Move.from_uci("a2a3")


def test_string_option_values_are_coerced_and_reach_the_policy():
    """A saved Randomness string reaches the policy as an int window.

    Why: profile values are stored as strings in the `.uci`; the player must
    coerce them exactly like the UCI setoption path or the policy would treat
    Randomness as 0. How it manifests: with Randomness "1" and a seeded RNG the
    move is one of the two most-equal moves; a dropped/uncoerced option would
    always emit a2a3, and a miscomputed window could emit the far +3.00 move.
    """
    infos = [
        _info("a2a3", Cp(10), chess.WHITE),
        _info("b2b3", Cp(-20), chess.WHITE),
        _info("e2e4", Cp(300), chess.WHITE),
    ]
    handle = FakeHandle(infos)
    player = _player("drawfish", uci_options={"Randomness": "1"}, rng_seed=1)

    move = player._compute_move(handle, chess.Board())

    assert move in (chess.Move.from_uci("a2a3"), chess.Move.from_uci("b2b3"))
    assert move != chess.Move.from_uci("e2e4")  # far-from-equal move is out of pool


def test_single_legal_move_skips_analysis():
    """A forced position is played without analysing the shared engine.

    Why: analysing a forced move wastes the shared Stockfish's time and is the
    position that mates Drawfish. How it manifests: a missing short-circuit would
    record an analyse call.
    """
    forced_fen = "R5k1/6pp/8/8/8/8/8/7K b - - 0 1"  # g8f7 is the only legal move
    handle = FakeHandle(infos=[])
    player = _player("worstfish")

    move = player._compute_move(handle, chess.Board(forced_fen))

    assert move == chess.Move.from_uci("g8f7")
    assert handle.analyse_calls == []


def test_spec_resolve_options_coerces_and_defaults():
    """`DerivedEngineSpec.resolve_options` coerces valid values, else keeps defaults.

    Why: this is the shared coercion the in-process player relies on to match the
    UCI setoption path. How it manifests: a valid spin is clamped/parsed to int, a
    check maps true/false -> 1/0, an invalid value keeps the default, and an
    absent option keeps its default -- any drift here would feed the policy wrong
    integers.
    """
    drawfish = SPECS["drawfish"]
    resolved = drawfish.resolve_options(
        {"Randomness": "99", "AvoidCaptures": "false", "Bogus": "x"}
    )
    assert resolved == {"Randomness": 10, "AvoidCaptures": 0}  # 99 clamped to max 10

    # Invalid Randomness keeps the Drawfish default (3); absent AvoidCaptures too (1).
    assert drawfish.resolve_options({"Randomness": "abc"}) == {"Randomness": 3, "AvoidCaptures": 1}


def test_no_usable_lines_falls_back_to_first_legal():
    """When analyse yields no scored lines, play a legal move rather than fail.

    Why: the engine must always return a move; a scoreless result must not crash
    or return None mid-game. How it manifests: without the fallback the policy
    would receive an empty candidate list.
    """
    handle = FakeHandle(infos=[{"pv": [], "score": None}])
    player = _player("worstfish")

    move = player._compute_move(handle, chess.Board())

    assert move in set(chess.Board().legal_moves)
    assert len(handle.analyse_calls) == 1  # it did attempt analysis
