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

Base = declarative_base()

class Game(Base):
    # A chess game
    __tablename__ = "game"

    id = Column(Integer, primary_key=True, autoincrement="auto")
    created_at = Column(DateTime, server_default=func.now())
    source = Column(String(255), nullable=False) # centaur, lichess, eboard, ct800, etc
    event = Column(String(255), nullable=True)
    site = Column(String(255), nullable=True)
    round = Column(String(255), nullable=True)
    white = Column(String(255), nullable=True)
    black = Column(String(255), nullable=True)
    result = Column(String(255), nullable=True)

    def __repr__(self):
        return "<Game(id='%s', created_at='%s', source='%s')>" % (str(self.id), str(self.created_at), self.source)

class GameMove(Base):
    # A move/board state in a chess game
    __tablename__ = "gameMove"
    id = Column(Integer, primary_key=True, autoincrement="auto")
    gameid = Column(Integer, ForeignKey("game.id"), index=True)
    move_at = Column(DateTime, server_default=func.now())
    move = Column(String(10), nullable=True)
    fen = Column(String(255), nullable=True)
    # Clock times in seconds remaining after this move (nullable for existing databases)
    white_clock = Column(Integer, nullable=True)
    black_clock = Column(Integer, nullable=True)
    # Analysis score in centipawns from white's perspective (nullable for existing databases)
    eval_score = Column(Integer, nullable=True)
    # AI-generated coach statement about this move (nullable; populated lazily the
    # first time a user reviews the move with a coach service configured). Stored
    # so a statement is fetched from the AI service at most once per move.
    coach_statement = Column(Text, nullable=True)

    game = relationship("Game")

    def __repr__(self):
        return "<GameMove(id='%s', move_at='%s', move='%s', fen='%s')>" % (str(self.id), str(self.move_at), self.move, self.fen)

engine = create_engine(get_database_uri())
Base.metadata.create_all(bind=engine)

# Schema migration: Add clock columns if they don't exist (for existing databases)
# SQLAlchemy's create_all() doesn't add columns to existing tables, so we do it manually
try:
    from sqlalchemy import text, inspect
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('gameMove')]
    
    with engine.connect() as conn:
        if 'white_clock' not in columns:
            conn.execute(text('ALTER TABLE gameMove ADD COLUMN white_clock INTEGER'))
            conn.commit()
        if 'black_clock' not in columns:
            conn.execute(text('ALTER TABLE gameMove ADD COLUMN black_clock INTEGER'))
            conn.commit()
        if 'eval_score' not in columns:
            conn.execute(text('ALTER TABLE gameMove ADD COLUMN eval_score INTEGER'))
            conn.commit()
        if 'coach_statement' not in columns:
            conn.execute(text('ALTER TABLE gameMove ADD COLUMN coach_statement TEXT'))
            conn.commit()
except Exception:  # noqa: BLE001, S110  # nosec B110 - startup migration must never crash import
    # Migration may fail if the table doesn't exist yet (first run) - that's ok;
    # create_all() has already built the current schema and any missing legacy
    # column is retried on the next start. Best-effort, intentionally ignored.
    pass