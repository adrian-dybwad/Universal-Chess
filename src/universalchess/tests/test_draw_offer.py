"""Tests for draw-offer accept/decline decisions.

Background / why these tests exist
----------------------------------
Offering a draw from the back menu used to record a draw unconditionally, so an
engine opponent would "agree" to a draw even in a position it was clearly
winning. The fix makes a draw an OFFER that the opponent evaluates:
- EnginePlayer.consider_draw_offer analyses the position from its own colour and
  declines while it is winning (by more than a small margin, or with a mate).
- opponent_accepts_draw routes the offer to the (non-human) opponent, while
  human-vs-human stays a mutual agreement.

Each test pins one branch of that decision. The engine's evaluation is supplied
via a fake handle so the outcome is deterministic (no real engine, no search
time variance).
"""

from types import SimpleNamespace

import chess
import chess.engine

from universalchess.players.engine import EnginePlayer
from universalchess.players import PlayerType
from universalchess.managers.game.draw_offer import opponent_accepts_draw


class _AnalyseHandle:
    """Fake engine handle whose analyse() returns a preset PovScore."""

    def __init__(self, pov_score):
        self._pov_score = pov_score
        self.analyse_calls = 0

    def analyse(self, board, limit, multipv=None):
        self.analyse_calls += 1
        return {"score": self._pov_score}

    def configure(self, options):
        pass


class _RaisingHandle:
    """Fake engine handle whose analyse() raises, to exercise the fallback."""

    def analyse(self, board, limit, multipv=None):
        raise RuntimeError("engine crashed during analysis")


class _EmptyHandle:
    """Fake engine handle whose analyse() returns no score key."""

    def analyse(self, board, limit, multipv=None):
        return {}


def _engine(color, handle):
    """Build an EnginePlayer wired with a colour and a fake handle."""
    player = EnginePlayer()
    player._color = color
    player._engine_handle = handle
    return player


def _cp(centipawns_white_pov):
    """PovScore of the given centipawns, expressed from White's perspective."""
    return chess.engine.PovScore(chess.engine.Cp(centipawns_white_pov), chess.WHITE)


def _mate(mate_in_white_pov):
    """PovScore of the given mate distance, expressed from White's perspective."""
    return chess.engine.PovScore(chess.engine.Mate(mate_in_white_pov), chess.WHITE)


# ---------------------------------------------------------------------------
# Base Player default
# ---------------------------------------------------------------------------


def test_base_player_accepts_by_default():
    """A generic player accepts a draw (mutual agreement at the board).

    Why: 2-player mode and any non-overriding player must keep the historical
    "Draw = agree" behaviour so an offer is never silently dropped.

    How the regression manifests: the default returns False and human-vs-human
    draws stop working.
    """
    human = SimpleNamespace()
    # Bind the unbound base method to avoid constructing an abstract subclass.
    from universalchess.players.base import Player
    assert Player.consider_draw_offer(human, chess.Board()) is True


# ---------------------------------------------------------------------------
# EnginePlayer.consider_draw_offer
# ---------------------------------------------------------------------------


def test_engine_declines_when_clearly_winning():
    """A winning engine refuses the draw.

    Why: this is the reported bug - offering a draw while the engine is up ~+2
    must NOT be accepted.

    How the regression manifests: consider_draw_offer returns True and the
    losing human is handed a draw the engine should never accept.
    """
    player = _engine(chess.WHITE, _AnalyseHandle(_cp(200)))
    assert player.consider_draw_offer(chess.Board()) is False


def test_engine_accepts_when_equal():
    """An engine in an equal position accepts the draw.

    Why: a roughly level position is a legitimate draw; the engine should agree.

    How the regression manifests: an equal-position offer is declined, so draws
    can never be agreed against the engine.
    """
    player = _engine(chess.WHITE, _AnalyseHandle(_cp(0)))
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_accepts_when_losing():
    """A losing engine gladly accepts the draw.

    Why: when the engine is worse, a draw is a better-than-expected result.

    How the regression manifests: a losing engine declines, forcing play on in a
    position it should be happy to halve.
    """
    player = _engine(chess.WHITE, _AnalyseHandle(_cp(-300)))
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_accepts_exactly_at_threshold():
    """At exactly +0.5 (the threshold), the engine still accepts.

    Why: pins the inclusive boundary (accept iff <= threshold); a tiny edge is
    not "clearly winning".

    How the regression manifests: an off-by-one on the comparison flips the
    boundary and the engine declines a near-equal draw.
    """
    threshold = EnginePlayer.DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS
    player = _engine(chess.WHITE, _AnalyseHandle(_cp(threshold)))
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_declines_just_above_threshold():
    """One centipawn above the threshold, the engine declines.

    Why: confirms the boundary is exclusive on the decline side, complementing
    the accept-at-threshold test.

    How the regression manifests: the comparison uses >= instead of >, so the
    engine wrongly accepts a position past the cutoff (or vice-versa).
    """
    threshold = EnginePlayer.DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS
    player = _engine(chess.WHITE, _AnalyseHandle(_cp(threshold + 1)))
    assert player.consider_draw_offer(chess.Board()) is False


def test_engine_evaluates_from_its_own_colour():
    """A Black engine uses Black's perspective, not White's.

    Why: the score from analyse is White-relative; the engine must convert to
    its own colour. If Black is winning (White-relative score negative), the
    Black engine must decline.

    How the regression manifests: the sign/perspective is dropped, so a winning
    Black engine reads its own position as losing and accepts a draw it is
    winning (or a losing Black engine refuses).
    """
    # White-relative -300 means Black is ahead by +3 from Black's perspective.
    player = _engine(chess.BLACK, _AnalyseHandle(_cp(-300)))
    assert player.consider_draw_offer(chess.Board()) is False


def test_engine_declines_when_it_has_forced_mate():
    """An engine with a mate in hand refuses the draw.

    Why: a forced mate is the strongest possible advantage; accepting a draw
    would throw away a win.

    How the regression manifests: mate scores are mishandled and the engine
    accepts a draw while mating.
    """
    player = _engine(chess.WHITE, _AnalyseHandle(_mate(3)))
    assert player.consider_draw_offer(chess.Board()) is False


def test_engine_accepts_when_being_mated():
    """An engine getting mated accepts the draw.

    Why: when the engine is the one being mated, a draw is a reprieve it should
    take.

    How the regression manifests: negative mate distance is treated like a
    winning mate and the engine declines a draw that saves it.
    """
    player = _engine(chess.WHITE, _AnalyseHandle(_mate(-2)))
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_accepts_when_no_engine_handle():
    """With no engine ready, the offer is accepted (prior behaviour).

    Why: without an engine to judge, fabricating a decline is worse than falling
    back to the historical accept.

    How the regression manifests: consider_draw_offer raises or returns False
    when the handle is missing, breaking offers made before the engine loads.
    """
    player = EnginePlayer()
    player._color = chess.WHITE
    player._engine_handle = None
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_accepts_when_analysis_raises():
    """An analysis failure falls back to accepting.

    Why: a transient engine error must not strand the game; accepting matches
    the pre-existing behaviour rather than inventing a decline.

    How the regression manifests: the exception propagates out of the menu
    handler, or a decline is fabricated with no evaluation behind it.
    """
    player = _engine(chess.WHITE, _RaisingHandle())
    assert player.consider_draw_offer(chess.Board()) is True


def test_engine_accepts_when_analysis_has_no_score():
    """A scoreless analysis result falls back to accepting.

    Why: without a score there is no basis to refuse; accept per the fallback
    contract.

    How the regression manifests: a KeyError/None dereference on the missing
    score, or a fabricated decline.
    """
    player = _engine(chess.WHITE, _EmptyHandle())
    assert player.consider_draw_offer(chess.Board()) is True


# ---------------------------------------------------------------------------
# opponent_accepts_draw routing
# ---------------------------------------------------------------------------


class _FakePlayer:
    """Minimal stand-in exposing player_type and consider_draw_offer."""

    def __init__(self, player_type, accepts):
        self.player_type = player_type
        self._accepts = accepts
        self.consider_calls = 0

    def consider_draw_offer(self, board):
        self.consider_calls += 1
        return self._accepts


def _manager(white, black):
    return SimpleNamespace(white_player=white, black_player=black)


def test_two_human_game_accepts_without_consulting():
    """Human-vs-human draws are agreed without any engine consultation.

    Why: 2-player mode has no engine; the offer is a mutual agreement and must
    be accepted directly.

    How the regression manifests: the routing consults a (non-existent) engine
    or declines, so two humans can no longer agree a draw.
    """
    white = _FakePlayer(PlayerType.HUMAN, accepts=False)
    black = _FakePlayer(PlayerType.HUMAN, accepts=False)
    assert opponent_accepts_draw(_manager(white, black), chess.Board()) is True
    # Neither human should have been asked to evaluate.
    assert white.consider_calls == 0
    assert black.consider_calls == 0


def test_engine_opponent_decline_is_propagated():
    """When the engine opponent declines, the routing returns False.

    Why: the whole point of the fix - a declining engine must keep the game
    going.

    How the regression manifests: the routing ignores the engine's decision and
    returns True, re-introducing the original bug.
    """
    human = _FakePlayer(PlayerType.HUMAN, accepts=True)
    engine = _FakePlayer(PlayerType.ENGINE, accepts=False)
    assert opponent_accepts_draw(_manager(human, engine), chess.Board()) is False
    assert engine.consider_calls == 1


def test_engine_opponent_accept_is_propagated():
    """When the engine opponent accepts, the routing returns True.

    Why: an equal/losing engine that accepts must actually end the game as a
    draw.

    How the regression manifests: the routing drops the accept and the draw is
    never recorded.
    """
    human = _FakePlayer(PlayerType.HUMAN, accepts=True)
    engine = _FakePlayer(PlayerType.ENGINE, accepts=True)
    assert opponent_accepts_draw(_manager(human, engine), chess.Board()) is True
    assert engine.consider_calls == 1


def test_missing_manager_accepts():
    """A missing player manager falls back to accepting.

    Why: an offer must never be silently swallowed by a wiring gap; accepting
    matches the historical default.

    How the regression manifests: a None manager raises instead of returning a
    safe accept.
    """
    assert opponent_accepts_draw(None, chess.Board()) is True
