"""Record proxy-reconstructed Centaur games into Universal Chess's database.

Replaces the patched ``stockfish_pi.real``'s built-in SQLite logging (which
wrote to ``/opt/DGTCentaurMods/db/centaur.db``) by writing the same shape of
data -- a ``game`` row plus ``gameMove`` rows (move + resulting FEN) -- into UC's
own database via the shared SQLAlchemy models, so Centaur games appear alongside
UC games with no modified engine.
"""

from __future__ import annotations

from typing import Optional

from universalchess.services.centaur_engine_proxy.tracker import GameUpdate


def _default_models():
    """Import the shared SQLAlchemy models lazily.

    Deferred so importing this package does not build the DB engine until a
    recorder is actually constructed (the pure modules can be used without a DB).
    """
    from universalchess.db import models

    return models


class GameRecorder:
    """Persist a stream of GameUpdates as game/move rows.

    One recorder instance follows one Centaur session: it opens a new ``game``
    row on each new-game update (with an initial empty-move row carrying the
    start FEN, matching how UC records games), appends a ``gameMove`` per move
    with the FEN after it, and stamps the ``result`` once the game ends. The
    session is injected so tests can bind it to an in-memory database.
    """

    def __init__(self, session, *, source: str = "centaur", models=None) -> None:
        self._session = session
        self._source = source
        self._models = models if models is not None else _default_models()
        self._game_id: Optional[int] = None
        self._result_recorded = False

    def apply(self, update: GameUpdate) -> None:
        """Fold one GameUpdate into the database and commit it.

        A no-op (other than the commit it would skip) when there is no active
        game and the update is not a new game -- e.g. a stray continuation before
        any startpos, which should not create dangling move rows.
        """
        models = self._models

        if update.is_new_game:
            game = models.Game(source=self._source)
            self._session.add(game)
            self._session.flush()
            self._game_id = game.id
            self._result_recorded = False
            # Initial position row mirrors UC's own recording (empty move, start FEN).
            self._session.add(
                models.GameMove(gameid=self._game_id, move="", fen=update.start_fen)
            )

        if self._game_id is None:
            return

        for uci, fen_after in update.moves_added:
            self._session.add(
                models.GameMove(gameid=self._game_id, move=uci, fen=fen_after)
            )

        if update.result and not self._result_recorded:
            game = self._session.get(models.Game, self._game_id)
            if game is not None:
                game.result = update.result
                self._result_recorded = True

        self._session.commit()
