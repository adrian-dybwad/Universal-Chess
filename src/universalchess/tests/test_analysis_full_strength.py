"""Tests that position analysis always searches at the engine's full strength.

Why these tests exist
---------------------
``EngineRegistry`` pools one engine process per binary path, and a player engine
configures that shared process from its ELO profile (``UCI_LimitStrength=true``,
``UCI_Elo=1500``, ``Skill Level=3``). Nothing ever restored those options, and
``EngineHandle.analyse`` took no options at all. Because the default player
engine and the default ``analysis_engine`` are both ``stockfish``, playing a
reduced-ELO opponent silently weakened the eval graph, the persisted
``eval_score`` and the ``?`` hint -- all of which are meant to be objective.

How a regression manifests
--------------------------
The analysis path stops sending strength-clearing options, so whatever the
opponent last configured stays active on the shared process. The evals reported
for a game against a 1350-rated Stockfish would come from a 1350-rated search
rather than a full-strength one, and the hint would suggest a deliberately
weakened move. Nothing raises; the numbers are just quietly wrong.
"""

import threading
from collections import namedtuple

import chess
import chess.engine
import pytest

from universalchess.services.analysis import AnalysisService
from universalchess.services.engine_registry import EngineHandle
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


# Mirrors the fields of chess.engine.Option that the derivation reads. A real
# Option carries more, but only these drive the full-strength values.
FakeOption = namedtuple("FakeOption", "name type default min max var")


def _option(name, type_, default, min_=None, max_=None):
    return FakeOption(name, type_, default, min_, max_, None)


# A Stockfish-shaped option set: the three knobs that limit playing strength,
# plus one unrelated option that must never be touched by the derivation.
STOCKFISH_OPTIONS = {
    "UCI_LimitStrength": _option("UCI_LimitStrength", "check", False),
    "UCI_Elo": _option("UCI_Elo", "spin", 1320, 1320, 3190),
    "Skill Level": _option("Skill Level", "spin", 20, 0, 20),
    "Threads": _option("Threads", "spin", 1, 1, 32),
}

# A derived policy engine (Worstfish/Drawfish) advertises none of the strength
# knobs -- forwarding options it never declared aborts its initialization.
POLICY_ENGINE_OPTIONS = {
    "Randomness": _option("Randomness", "spin", 0, 0, 100),
    "AvoidCaptures": _option("AvoidCaptures", "check", False),
}


class RecordingEngine:
    """Minimal stand-in for chess.engine.SimpleEngine.

    Records every ``configure`` call in order so a test can assert what reached
    the engine and when, relative to the search.
    """

    def __init__(self, options=None):
        self.options = dict(options if options is not None else STOCKFISH_OPTIONS)
        self.configure_calls = []
        self.analyse_calls = []
        self.play_calls = []
        # Options in force at the moment each search started, which is what
        # actually determines the strength of that search.
        self.options_at_search = []
        self._active = {}

    def configure(self, options):
        self.configure_calls.append(dict(options))
        self._active.update(options)

    def analyse(self, board, limit, multipv=None):
        self.analyse_calls.append((board, limit, multipv))
        self.options_at_search.append(dict(self._active))
        return {"score": chess.engine.PovScore(chess.engine.Cp(25), chess.WHITE)}

    def play(self, board, limit, root_moves=None, ponder=False, game=None):
        self.play_calls.append((board, limit))
        self.options_at_search.append(dict(self._active))
        return chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None)


def _handle(options=None):
    engine = RecordingEngine(options)
    return EngineHandle(path="/usr/games/stockfish", engine=engine), engine


# ---------------------------------------------------------------------------
# full_strength_options derivation
# ---------------------------------------------------------------------------


def test_full_strength_options_derived_from_advertised_limits():
    """Values come from the engine's own advertised metadata, not constants.

    Regression: hardcoding ``Skill Level=20`` would be wrong for any engine
    whose ladder tops out lower, and python-chess rejects an out-of-range spin
    value -- so the search would raise instead of merely being weak.
    """
    handle, _ = _handle()

    assert handle.full_strength_options() == {
        "UCI_LimitStrength": False,
        "UCI_Elo": 3190,      # the advertised maximum, not a guessed number
        "Skill Level": 20,    # the advertised maximum
    }


def test_full_strength_options_empty_when_engine_has_no_strength_limits():
    """An engine advertising no strength knobs yields no options at all.

    Regression: returning a non-empty dict here would send Skill Level/UCI_Elo
    to the derived policy engines, which abort initialization on any option
    they did not declare -- Worstfish would stop being able to move.
    """
    handle, _ = _handle(POLICY_ENGINE_OPTIONS)

    assert handle.full_strength_options() == {}


def test_full_strength_options_ignores_unrelated_options():
    """Only strength-limiting options are touched.

    Regression: sweeping every spin option to its maximum would set Threads=32
    on a Pi Zero 2 W, exhausting the 415 MiB the board actually has.
    """
    handle, _ = _handle()

    assert "Threads" not in handle.full_strength_options()


def test_full_strength_options_omits_spin_without_declared_maximum():
    """A spin option with no max is skipped rather than given an invented value.

    Regression: substituting a plausible-looking default would push an
    out-of-range value at an engine whose real ceiling is unknown.
    """
    handle, _ = _handle({"Skill Level": _option("Skill Level", "spin", 20, 0, None)})

    assert handle.full_strength_options() == {}


# ---------------------------------------------------------------------------
# EngineHandle.analyse options plumbing
# ---------------------------------------------------------------------------


def test_analyse_applies_options_before_searching():
    """Options must be configured before the search they are meant to govern.

    Regression: configuring after (or not at all) leaves the previous
    consumer's weakened settings in force for this search, which is exactly the
    bug -- the eval is produced at the opponent's ELO.
    """
    handle, engine = _handle()

    handle.analyse(chess.Board(), chess.engine.Limit(time=0.1),
                   options={"UCI_LimitStrength": False})

    assert engine.options_at_search == [{"UCI_LimitStrength": False}]


def test_analyse_drops_options_the_engine_did_not_advertise():
    """Unadvertised options are filtered, matching play()'s existing contract.

    Regression: forwarding them makes python-chess raise EngineError, so
    analysis of every position would fail on limited engines instead of simply
    running at their only strength.
    """
    handle, engine = _handle(POLICY_ENGINE_OPTIONS)

    handle.analyse(chess.Board(), chess.engine.Limit(time=0.1),
                   options={"UCI_LimitStrength": False, "Skill Level": 20})

    assert engine.configure_calls == []
    assert len(engine.analyse_calls) == 1


def test_analyse_without_options_does_not_configure():
    """Passing no options must not touch engine configuration.

    Regression: an unconditional configure() would issue setoption on every
    analysis, disrupting a shared engine for no reason.
    """
    handle, engine = _handle()

    handle.analyse(chess.Board(), chess.engine.Limit(time=0.1))

    assert engine.configure_calls == []


def test_analyse_still_returns_single_infodict_when_multipv_is_none():
    """The multipv contract is unchanged by the new parameter.

    Regression: python-chess returns a list for any int multipv, and callers
    here index the result as a dict -- a silent break that empties the score.
    """
    handle, engine = _handle()

    info = handle.analyse(chess.Board(), chess.engine.Limit(time=0.1),
                          options={"UCI_LimitStrength": False})

    assert isinstance(info, dict)
    assert engine.analyse_calls[0][2] is None


# ---------------------------------------------------------------------------
# AnalysisService uses full strength
# ---------------------------------------------------------------------------


def _run_one_analysis(service, handle):
    """Drive the worker through exactly one queued request."""
    service.set_engine_handle(handle)
    done = threading.Event()

    original = handle.analyse

    def watched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    handle.analyse = watched
    service._start_worker()
    try:
        assert done.wait(timeout=5.0), "worker never ran the queued analysis"
    finally:
        service._stop_worker()


def test_analysis_service_searches_at_full_strength_after_a_weak_opponent():
    """The eval must not inherit the opponent's ELO cap from the shared process.

    This is the reported bug, reproduced end to end: a player engine configures
    the pooled process down to 1350, then analysis runs on that same handle.

    Regression manifests as UCI_LimitStrength still True at search time, so the
    recorded evaluation comes from a deliberately weakened engine.
    """
    game = reset_chess_game()
    reset_analysis()
    service = AnalysisService()
    handle, engine = _handle()

    # A reduced-ELO player engine configures the shared process first.
    handle.configure({"UCI_LimitStrength": True, "UCI_Elo": 1350, "Skill Level": 3})

    game.push_uci("e2e4")
    service._queue_position(add_to_history=False, is_new_ply=True)
    _run_one_analysis(service, handle)

    in_force = engine.options_at_search[-1]
    assert in_force["UCI_LimitStrength"] is False
    assert in_force["UCI_Elo"] == 3190
    assert in_force["Skill Level"] == 20


def test_analysis_service_does_not_break_engines_without_strength_options():
    """Analysis still runs on an engine that advertises no strength knobs.

    Regression manifests as an EngineError swallowed by the worker's handler,
    leaving AnalysisState never updated -- the graph would stay flat.
    """
    game = reset_chess_game()
    analysis = reset_analysis()
    service = AnalysisService()
    handle, engine = _handle(POLICY_ENGINE_OPTIONS)

    game.push_uci("e2e4")
    service._queue_position(add_to_history=True, is_new_ply=True)
    _run_one_analysis(service, handle)

    assert engine.configure_calls == []
    assert len(engine.analyse_calls) == 1
    assert analysis.history_length == 1
