"""Resume of a game played from the website board.

Why these tests exist
---------------------
A move on the website board tab applies the opponent's reply without anyone
touching a piece. After a restart, resume used to compare that position to the
piece scan and enter correction mode, so the game waited for pieces that were
never moved. The saved position, reply included, is the position to assume.
A later move on the physical board clears the mark, and a physical game must
still be corrected.
"""

import chess
import pytest

from universalchess.app.board_app import resume_assumes_saved_position
from universalchess.managers.game.move_persistence import (
    persist_move_and_maybe_create_game,
)


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session, same isolation as the Chess960 persistence tests."""
    import universalchess.db.uri as uri

    monkeypatch.setattr(uri, "get_database_uri", lambda: "sqlite:///:memory:")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from universalchess.db import models

    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.models = models

    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)
    yield db
    db.close()


def _persist(session, *, is_first_move, game_db_id, move, web_play):
    board = chess.Board()
    if not is_first_move:
        board.push_uci("e2e4")
    fen_before = board.fen()
    board.push_uci(move)
    return persist_move_and_maybe_create_game(
        session=session,
        is_first_move=is_first_move,
        current_game_db_id=game_db_id,
        source_file="test",
        game_info={},
        fen_before_move=fen_before,
        move_uci=move,
        fen_after_move=board.fen(),
        white_clock=None,
        black_clock=None,
        web_play=web_play,
    )


def test_website_board_move_is_what_resume_assumes(session):
    """The first website move marks the game, and a later board move clears it.

    Why: resume reads this flag to decide whether the saved position is already
    the game. If the website move left it unset, resume would enter correction
    mode. If a later physical move left it set, resume would skip correction
    for a board the player is using.
    """
    game_db_id, committed = _persist(
        session, is_first_move=True, game_db_id=-1, move="e2e4", web_play=True,
    )
    assert committed is True
    game = session.get(session.models.Game, game_db_id)
    assert game.web_play is True
    assert resume_assumes_saved_position(game.web_play) is True

    _persist(
        session, is_first_move=False, game_db_id=game_db_id, move="e7e5", web_play=False,
    )
    session.refresh(game)
    assert game.web_play is False
    assert resume_assumes_saved_position(game.web_play) is False


def test_physical_game_is_not_assumed(session):
    """A game started on the board must still be checked against the pieces.

    Why: skipping correction for every resume would leave a physical game
    running while the pieces are somewhere else. The flag stays unset, and
    resume_assumes_saved_position is false.
    """
    game_db_id, _committed = _persist(
        session, is_first_move=True, game_db_id=-1, move="d2d4", web_play=False,
    )
    game = session.get(session.models.Game, game_db_id)
    assert game.web_play is False
    assert resume_assumes_saved_position(False) is False
