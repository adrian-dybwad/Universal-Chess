"""Lichess player plugin: hosts, credentials, seek, lobby, and the Player.

The game talks to :class:`LichessPlayer`. Hosts, tokens, identity, seek, and
the board lobby live in this package. Chess.com (or any other provider) does
not import these modules. Plugin tests live in ``tests/``.
"""

from .player import (
    LichessGameMode,
    LichessPlayer,
    LichessPlayerConfig,
    create_lichess_player,
    lichess_player_from_seek,
)

__all__ = [
    "LichessGameMode",
    "LichessPlayer",
    "LichessPlayerConfig",
    "create_lichess_player",
    "lichess_player_from_seek",
]
