"""Tests for choose_resume_target, the startup resume-selection policy.

The policy decides which game a restart resumes given (a) the game the session
snapshot recorded and (b) the newest in-progress game. It lives in a pure module
so it is testable without importing the hardware-heavy application entrypoint
(main.py), which is why these tests exercise it directly rather than through the
startup path.

Regression guarded: an in-progress game must load after a restart even when the
snapshot still points at the PREVIOUS (finished) game -- the reported bug where a
restart reopened a completed drawn game instead of the live one, because a new
game started in place on the board left the finished game's id in the snapshot.
"""

import pytest

from universalchess.managers.game.resume_policy import (
    choose_resume_target,
    should_persist_game,
)


def _game(game_id: int, result):
    """Minimal resume payload carrying the keys the policy reads."""
    return {"id": game_id, "result": result}


def test_abandoned_recorded_game_is_not_resumed():
    """An abandoned game (result *) must not come back after a restart.

    Why: home-rank confirm marks the leftover correspondence row abandoned,
    but the session snapshot still pointed at that id. * is not 1-0/0-1/draw,
    so startup restored it as Human vs Engine.

    How the regression manifests: recorded * wins and the leftover FEN loads.
    """
    recorded = _game(93, "*")
    assert choose_resume_target(recorded, None) is None


def test_abandoned_recorded_does_not_block_a_newer_local_game():
    """A later local in-progress game must load after an abandoned leftover.

    How the regression manifests: recorded * is honoured and the local game
    is skipped.
    """
    recorded = _game(93, "*")
    incomplete = _game(94, None)
    assert choose_resume_target(recorded, incomplete) is incomplete


def test_lichess_incomplete_is_not_auto_resumed():
    """Correspondence continues from the Lichess menu, not as a local engine game.

    Why: catch-up persisted the live FEN as start_fen; boot resumed it as
    Human vs Koivisto. Ongoing Games on the lobby is how that game continues.

    How the regression manifests: a site=Lichess incomplete row is returned.
    """
    leftover = _game(93, None)
    leftover["site"] = "Lichess"
    assert choose_resume_target(None, leftover) is None
    assert choose_resume_target(leftover, leftover) is None


def test_lichess_source_incomplete_is_not_auto_resumed():
    """source=lichess is the same leftover, even when site is empty.

    How the regression manifests: source is ignored and the row resumes locally.
    """
    leftover = _game(93, None)
    leftover["source"] = "lichess"
    assert choose_resume_target(None, leftover) is None


def test_none_when_nothing_resumable():
    # Neither a recorded nor an in-progress game -> nothing to resume. Regression:
    # returning a stray dict here would make startup try to load a phantom game.
    assert choose_resume_target(None, None) is None


def test_returns_incomplete_when_no_recorded_game():
    # Fresh/upgraded device (no snapshot id): fall back to the live game. If this
    # returned None the in-progress game would be dropped on every boot.
    incomplete = _game(59, None)
    assert choose_resume_target(None, incomplete) is incomplete


def test_returns_recorded_when_no_incomplete_game():
    # Reviewing a finished game with no live game present must restore that exact
    # game (its game-over screen). Regression: losing it would break review resume.
    recorded = _game(58, "1/2-1/2")
    assert choose_resume_target(recorded, None) is recorded


@pytest.mark.parametrize("finished", ["1-0", "0-1", "1/2-1/2"])
def test_newer_in_progress_supersedes_recorded_finished(finished):
    # THE reported bug: snapshot points at a finished game (id 58) but a newer
    # in-progress game (id 59) exists. The live game must win. Manifestation if
    # broken: the restart reopens the finished (drawn) game and the real game is
    # stranded until manually resumed from the Games tab.
    recorded = _game(58, finished)
    incomplete = _game(59, None)
    assert choose_resume_target(recorded, incomplete) is incomplete


def test_older_in_progress_does_not_supersede_recorded_finished():
    # An in-progress game OLDER than the recorded finished game is a stale orphan,
    # not the current game, so the recorded finished game (what the user was
    # viewing) is honoured. Regression: resurrecting an older orphan would yank the
    # user off the game-over screen they were reviewing.
    recorded = _game(58, "1-0")
    incomplete = _game(55, None)
    assert choose_resume_target(recorded, incomplete) is recorded


def test_recorded_in_progress_is_honoured_over_newer_incomplete():
    # When the snapshot itself points at an in-progress game it is the current
    # game and must be kept, even if another NULL-result row is newer. Regression:
    # a finished-only rule that ignored the recorded result could switch to the
    # wrong live game.
    recorded = _game(60, None)
    incomplete = _game(61, None)
    assert choose_resume_target(recorded, incomplete) is recorded


def test_local_games_are_persisted_and_lichess_games_are_not():
    """Lichess games continue from the lobby; local games still save.

    Why: the first move after correspondence catch-up created a local row with
    the live FEN as start_fen, which boot then resumed as Human vs Engine.

    How the regression manifests: is_remote True still returns persist True.
    """
    assert should_persist_game(is_position_game=False, is_remote=False) is True
    assert should_persist_game(is_position_game=False, is_remote=True) is False
    assert should_persist_game(is_position_game=True, is_remote=False) is False
    assert should_persist_game(is_position_game=True, is_remote=True) is False
