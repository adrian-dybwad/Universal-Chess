# This file is part of the DGTCentaur Mods open source software
# ( https://github.com/EdNekebno/DGTCentaur )
#
# DGTCentaur Mods is free software: you can redistribute
# it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# DGTCentaur Mods is distributed in the hope that it will
# be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this file.  If not, see
#
# https://github.com/EdNekebno/DGTCentaur/blob/master/LICENSE.md
#
# This and any other notices must remain intact and unaltered in any
# distribution, modification, variant, or derivative of this software.

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from universalchess.db.uri import get_database_uri
from universalchess.utils.timeutils import utcnow_naive

Base = declarative_base()

class Game(Base):
    # A chess game
    __tablename__ = "game"

    id = Column(Integer, primary_key=True, autoincrement="auto")
    # Stored as naive UTC. The Python-side default guarantees UTC on every engine
    # (not just SQLite's UTC CURRENT_TIMESTAMP) and stays UTC regardless of the
    # device's configured OS timezone; server_default is kept as a raw-INSERT
    # safety net. See universalchess.utils.timeutils.
    created_at = Column(DateTime, default=utcnow_naive, server_default=func.now())
    source = Column(String(255), nullable=False) # centaur, lichess, eboard, ct800, etc
    event = Column(String(255), nullable=True)
    site = Column(String(255), nullable=True)
    round = Column(String(255), nullable=True)
    white = Column(String(255), nullable=True)
    black = Column(String(255), nullable=True)
    result = Column(String(255), nullable=True)
    # How the game ended (e.g. 'Termination.CHECKMATE', 'Termination.RESIGN').
    # Board-terminal endings (checkmate/stalemate) are re-derivable from the
    # final position, but manual endings (resign/draw/time forfeit) are not, so
    # the reason is stored to reproduce the exact game-over state when a finished
    # game is resumed across a restart. Nullable for in-progress games and for
    # existing databases created before this column.
    termination = Column(String(255), nullable=True)
    # Starting FEN of the game. NULL means the standard start position (the vast
    # majority of games, and every game created before this column existed).
    # Populated for Chess960 games so the exact generated start can be restored
    # when the game is resumed. Move replay begins from here.
    start_fen = Column(String(255), nullable=True)
    # True when this is a Chess960 (Fischer Random) game. Required to rebuild the
    # board with the chess960 flag on resume so castling rules and the
    # king-onto-rook move encoding match how the game was played. Nullable/False
    # for standard games and pre-existing databases.
    chess960 = Column(Boolean, nullable=True, default=False)
    # The game's time control in the PGN standard's TimeControl format ("300",
    # "300+5", "40/5400:1800+30", "-" for untimed, "?" when the two sides differ).
    # See universalchess.services.pgn_time.pgn_time_control_tag.
    #
    # Also the only signal that separates an untimed game from a flagged one:
    # an untimed control seeds the clock to zero and never runs it, so
    # white_clock/black_clock below are a literal 0 rather than NULL. Without
    # this column every casual game would export as "[%clk 0:00:00]" on every
    # move, reading as both players out of time from the first move. NULL for
    # games recorded before this column existed.
    time_control = Column(String(64), nullable=True)

    def __repr__(self):
        return "<Game(id='%s', created_at='%s', source='%s')>" % (str(self.id), str(self.created_at), self.source)

class GameMove(Base):
    # A move/board state in a chess game
    __tablename__ = "gameMove"
    id = Column(Integer, primary_key=True, autoincrement="auto")
    gameid = Column(Integer, ForeignKey("game.id"), index=True)
    # Naive UTC, guaranteed UTC by the Python default (see Game.created_at).
    move_at = Column(DateTime, default=utcnow_naive, server_default=func.now())
    move = Column(String(10), nullable=True)
    fen = Column(String(255), nullable=True)
    # Clock times in seconds remaining after this move (nullable for existing databases)
    white_clock = Column(Integer, nullable=True)
    black_clock = Column(Integer, nullable=True)
    # Elapsed wall time in milliseconds from the previous move's confirmation
    # (or the start of the game, for the first move) to this move's confirmation,
    # measured on a *monotonic* clock. Exported as the PGN [%emt] command.
    #
    # Not derivable from move_at: that is stamped by the ORM default when the row
    # is inserted, which happens on the background task worker behind board
    # validation and engine work, so the lag between confirmation and insert is
    # unbounded and varies with load. A monotonic source is required because the
    # device's wall clock is stepped by NTP shortly after boot, and a step of a
    # few seconds is indistinguishable from a real think time.
    #
    # NULL means "not measured" -- every row written before this column existed,
    # and games built by replaying moves rather than playing them. Distinct from
    # 0, which is a real measurement of an effectively instant move.
    move_duration_ms = Column(Integer, nullable=True)
    # Analysis score in centipawns from white's perspective, for the position
    # *after* this move. NULL means the position was never analysed (analysis
    # off, or the search had not finished) -- distinct from 0, which is a real
    # evaluation meaning "dead equal". Forced mate is stored as the +/-10000
    # sentinel (universalchess.services.analysis.MATE_SCORE_CP).
    eval_score = Column(Integer, nullable=True)
    # The engine's best move (UCI) in the position after this move, taken from
    # the first move of the analysis principal variation. Persisted so the web
    # UI can draw the best-move arrow when reviewing a past game without
    # re-running an engine. NULL when unanalysed or when the engine reported no
    # principal variation.
    best_move = Column(String(10), nullable=True)
    # AI-generated coach statement about this move (nullable; populated lazily the
    # first time a user reviews the move with a coach service configured). Stored
    # so a statement is fetched from the AI service at most once per move.
    coach_statement = Column(Text, nullable=True)

    game = relationship("Game")

    def __repr__(self):
        return "<GameMove(id='%s', move_at='%s', move='%s', fen='%s')>" % (str(self.id), str(self.move_at), self.move, self.fen)

# Columns added after the original schema, as (table, column, DDL type). Applied
# by ALTER TABLE because create_all() only creates missing *tables*, never
# missing columns on a table that already exists. Each entry is guarded by a
# column-existence check, so the whole set is idempotent and may be re-run.
_ADDED_COLUMNS = (
    ('gameMove', 'white_clock', 'INTEGER'),
    ('gameMove', 'black_clock', 'INTEGER'),
    ('gameMove', 'eval_score', 'INTEGER'),
    ('gameMove', 'best_move', 'VARCHAR(10)'),
    ('gameMove', 'coach_statement', 'TEXT'),
    ('gameMove', 'move_duration_ms', 'INTEGER'),
    ('game', 'termination', 'VARCHAR(255)'),
    ('game', 'start_fen', 'VARCHAR(255)'),
    ('game', 'chess960', 'BOOLEAN'),
    ('game', 'time_control', 'VARCHAR(64)'),
)


def apply_pending_migrations(target_engine) -> None:
    """Add any columns missing from an existing database.

    Idempotent: every column is guarded by an existence check, so this is safe
    to call repeatedly and on a database that create_all() just built.

    Kept as a named function rather than import-time inline code so the upgrade
    path can be tested against a database created by an older release -- the
    failure mode it prevents (OperationalError "no such column" on every write)
    only appears on upgrade, never on a fresh install.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(target_engine)
    existing = {
        table: {col['name'] for col in inspector.get_columns(table)}
        for table in {name for name, _, _ in _ADDED_COLUMNS}
        if inspector.has_table(table)
    }

    with target_engine.connect() as conn:
        for table, column, ddl_type in _ADDED_COLUMNS:
            if table in existing and column not in existing[table]:
                # The three identifiers come only from the _ADDED_COLUMNS literal
                # above; nothing here is reachable from a request, a setting or a
                # file. Interpolation is also the only option available: a table
                # or column name is an identifier, not a value, and SQL bind
                # parameters cannot carry identifiers. Keeping every entry a
                # literal is therefore what makes this safe, so a future entry
                # must not be built from input.
                conn.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'))
                conn.commit()


engine = create_engine(get_database_uri())
Base.metadata.create_all(bind=engine)

try:
    apply_pending_migrations(engine)
except Exception:  # noqa: BLE001, S110  # nosec B110 - startup migration must never crash import
    # Migration may fail if the table doesn't exist yet (first run) - that's ok;
    # create_all() has already built the current schema and any missing legacy
    # column is retried on the next start. Best-effort, intentionally ignored.
    pass