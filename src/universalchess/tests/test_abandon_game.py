"""Tests for GameManager.abandon_current_game.

Web "set up position" / "abort game" (and the on-board starting-position reset)
must record an in-progress game as abandoned using the existing DB result = "*"
convention, and must not double-mark or touch a finished/absent game. These
tests cover that logic with a fake session so no real database is needed.
"""

import pytest

import universalchess.managers.game.game_manager as gm_module
from universalchess.managers.game.game_manager import GameManager


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _FakeSession:
    """Minimal session returning a preset game and recording commits."""

    def __init__(self, game):
        self._game = game
        self.commits = 0

    def query(self, _model):
        return _FakeQuery(self._game)

    def commit(self):
        self.commits += 1


class _FakeGame:
    def __init__(self, result=None):
        self.id = 7
        self.result = result


class _FakeModels:
    class Game:
        id = None  # Class attribute; comparison in filter() is ignored by the fake.


@pytest.fixture(autouse=True)
def _patch_models(monkeypatch):
    # abandon_current_game resolves the ORM models via _get_models(); the fake
    # avoids importing the real model layer for these pure-logic tests.
    monkeypatch.setattr(gm_module, "_get_models", lambda: _FakeModels)


def _manager_with(session, game_db_id):
    """Build a GameManager without running __init__, wiring only what's used."""
    mgr = GameManager.__new__(GameManager)
    mgr.database_session = session
    mgr.game_db_id = game_db_id
    return mgr


def test_abandons_in_progress_game_and_resets_id():
    """An in-progress game (result None) must be marked "*" and id reset to -1.

    "*" is the abandoned/unfinished result code the history relies on. Resetting
    game_db_id to -1 prevents a later cleanup from marking the same row twice.
    Both effects are asserted; a regression that skipped the commit or the id
    reset would surface here.
    """
    game = _FakeGame(result=None)
    session = _FakeSession(game)
    mgr = _manager_with(session, game_db_id=7)

    assert mgr.abandon_current_game() is True
    assert game.result == "*"
    assert session.commits == 1
    assert mgr.game_db_id == -1


def test_does_not_remark_finished_game():
    """A finished game (result already set) must not be overwritten.

    Aborting after the game ended naturally must preserve the real result; only
    games still in progress (result None) become "*". Asserts the result is
    untouched and no commit happens.
    """
    game = _FakeGame(result="1-0")
    session = _FakeSession(game)
    mgr = _manager_with(session, game_db_id=7)

    assert mgr.abandon_current_game() is False
    assert game.result == "1-0"
    assert session.commits == 0


def test_no_op_without_active_db_game():
    """With no DB game (id < 0) or no session, abandon must be a safe no-op.

    Position/practice games and the no-game-yet state have no row to abandon;
    the method must return False without querying. Covers both guard conditions.
    """
    assert _manager_with(_FakeSession(_FakeGame()), game_db_id=-1).abandon_current_game() is False
    assert _manager_with(None, game_db_id=5).abandon_current_game() is False
