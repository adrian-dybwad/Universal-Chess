"""Resume restores the clock that was still counting.

Why these tests exist
---------------------
The move row stores each side's time when that move was played. The side to
move keeps counting after that. Resume used to read the move row, so the
thinking side came back at the time their previous move left them (a full 5
minutes) while the opponent, whose clock had stopped, matched the screen.
The game row holds the live remainders, and resume must prefer those.
"""

import chess
import pytest

from universalchess.app.board_app import _build_resume_data
from universalchess.managers.game.move_persistence import (
    persist_move_and_maybe_create_game,
    update_live_clocks,
)


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session, same isolation as the other persistence tests."""
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


def _play(session, *, move, white_clock, black_clock, game_db_id=-1, is_first_move=True):
    board = chess.Board()
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
        white_clock=white_clock,
        black_clock=black_clock,
    )


def test_resume_keeps_the_time_the_side_to_move_has_already_used(session):
    """Thinking time after the last move must survive resume.

    Why: the move row stays at the time the move was played (here white still
    had 300). The live clock then counts white down to 292. A resume that
    reads the move row gives white those 8 seconds back and leaves black, who
    was not thinking, at 290. The restored pair must be 292 and 290.
    """
    game_db_id, committed = _play(
        session, move="e2e4", white_clock=300, black_clock=290,
    )
    assert committed is True
    assert update_live_clocks(session, game_db_id, 292, 290) is True

    game = session.get(session.models.Game, game_db_id)
    resumed = _build_resume_data(session.models, session, game)
    assert resumed["white_clock"] == 292
    assert resumed["black_clock"] == 290


def test_resume_uses_the_move_row_when_no_live_clock_was_stored(session):
    """Games saved before the live columns existed still resume.

    Why: those rows have NULL live clocks. Ignoring the move row then would
    restore no clock at all. The move row's 300 and 290 must be used.
    """
    game_db_id, _committed = _play(
        session, move="e2e4", white_clock=300, black_clock=290,
    )
    game = session.get(session.models.Game, game_db_id)
    game.live_white_clock = None
    game.live_black_clock = None
    session.commit()

    resumed = _build_resume_data(session.models, session, game)
    assert resumed["white_clock"] == 300
    assert resumed["black_clock"] == 290
