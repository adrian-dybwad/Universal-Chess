"""Building a player from the settings of one player slot.

This mapping was a closure inside the game builder, so the decisions it makes
could not be checked anywhere: the label an unnamed engine is given, that a
derived novelty engine runs its policy on the shared Stockfish rather than
starting a second process, and that an unreadable player type still yields a
player rather than a side that can never move.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import chess

from universalchess.players.base import Player
from universalchess.players.settings import PlayerSettings, default_player_name

log = logging.getLogger(__name__)


def build_player(
    slot: PlayerSettings,
    color: chess.Color,
    *,
    ponder: bool = False,
    lichess_seek: Optional[Any] = None,
    lichess_join: Optional[dict] = None,
) -> Player:
    """The player described by one slot's settings, playing ``color``.

    Args:
        slot: The slot's settings: type, name, engine, strength, mode, think time.
        color: ``chess.WHITE`` or ``chess.BLACK``. Which slot gets which is the
            caller's decision, from Player 1 Color for both local and Lichess
            games. Lichess then remaps who occupies those colours after the
            stream names the account's side.
        ponder: Whether an engine may think on the opponent's clock. A game setting
            rather than a slot setting, so it is passed in.
        lichess_seek: The seek to post for a ``lichess`` slot.
        lichess_join: An existing Lichess game to attach to instead of seeking.

    Returns:
        A player for that side. An unrecognised type yields a human, because a
        config left behind by a downgrade must not leave the game with a side that
        cannot move -- on the board that reads as a game frozen on the first move.
    """
    from universalchess.players import (
        EnginePlayer,
        EnginePlayerConfig,
        HandBrainConfig,
        HandBrainMode,
        HandBrainPlayer,
        HumanPlayer,
        HumanPlayerConfig,
    )

    if slot.type == "human":
        return HumanPlayer(
            HumanPlayerConfig(
                name=slot.name or default_player_name(slot.slot),
                color=color,
                engine=slot.engine,
                elo=slot.elo,
            )
        )

    if slot.type == "engine":
        from universalchess.managers.engine_manager import engine_display_name
        from universalchess.services import uci_schema

        # The strength label rather than the raw section, so an uncapped "Default"
        # reads as "Unlimited" and the game card never shows a bare "(Default)".
        strength = uci_schema.strength_display_for_engine(slot.engine, slot.elo)
        config = EnginePlayerConfig(
            name=slot.name or f"{engine_display_name(slot.engine)} ({strength})",
            color=color,
            engine_name=slot.engine,
            elo_section=slot.elo,
            time_limit_seconds=float(slot.think_time),
            ponder=ponder,
        )
        # Derived novelty engines (Worstfish/Drawfish) run their selection policy
        # in-process against the shared pooled Stockfish. Building them as an
        # ordinary EnginePlayer would start a second Stockfish, which on a 512MB
        # board ends the game with the OOM killer rather than a bad move.
        from universalchess.services.derived_engines.spec import SPECS
        if slot.engine in SPECS:
            from universalchess.players.policy_engine import PolicyEnginePlayer
            return PolicyEnginePlayer(config, SPECS[slot.engine])
        return EnginePlayer(config)

    if slot.type == "lichess":
        from universalchess.players.lichess import lichess_player_from_seek
        return lichess_player_from_seek(lichess_seek, color=color, join=lichess_join)

    if slot.type == "hand_brain":
        from universalchess.managers.engine_manager import engine_display_name

        mode = (
            HandBrainMode.NORMAL
            if slot.hand_brain_mode == "normal"
            else HandBrainMode.REVERSE
        )
        mode_label = "N" if mode == HandBrainMode.NORMAL else "R"
        engine_display = engine_display_name(slot.engine)
        return HandBrainPlayer(
            HandBrainConfig(
                name=slot.name or f"H+B {mode_label} ({engine_display})",
                color=color,
                mode=mode,
                engine_name=slot.engine,
                elo_section=slot.elo,
                time_limit_seconds=float(slot.think_time),
            )
        )

    log.warning(f"[Players] Unknown player type: {slot.type}, defaulting to human")
    return HumanPlayer(
        HumanPlayerConfig(name=default_player_name(slot.slot), color=color)
    )
