# Draw Offer Decision
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Decides whether a draw offered by the human at the physical board is accepted
# by the opponent. Kept separate from the UI and game managers so the decision
# is a pure function of the players and position, and is directly testable.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import chess

from universalchess.board.logging import log
from universalchess.players import PlayerManager, PlayerType


def opponent_accepts_draw(player_manager: PlayerManager, board: chess.Board) -> bool:
    """Decide whether the human's draw offer is accepted by the opponent.

    The human plays at the physical board and makes the offer, so the deciding
    side is the opponent. Human opponents (2-player mode) always accept - the
    offer is a mutual agreement made at the board. A non-human opponent (an
    engine) is consulted via ``Player.consider_draw_offer`` so it can refuse a
    draw in a position it is winning.

    Args:
        player_manager: The manager holding both players.
        board: Current position at the time of the offer.

    Returns:
        True to accept the draw (end the game), False to decline (keep playing).
    """
    if player_manager is None:
        # No players wired (should not happen in a live game); accept so the
        # offer is never silently swallowed.
        log.warning("[DrawOffer] No player manager - accepting draw")
        return True

    # The deciding side is the opponent of the human. In a human-vs-engine game
    # that is the engine; in 2-player mode there is no engine and the offer is a
    # mutual agreement. If both sides are engines, the first non-human player
    # decides.
    for player in (player_manager.white_player, player_manager.black_player):
        if player.player_type != PlayerType.HUMAN:
            return player.consider_draw_offer(board)

    return True
