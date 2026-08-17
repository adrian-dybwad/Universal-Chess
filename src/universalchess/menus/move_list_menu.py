"""Move-list long-press-OK overlay: take back or start a new game from a ply.

UP/DOWN during a game highlights a played move. A short OK still pages coach
text or forces a full refresh. A long-press OK (LONG_TICK) opens this overlay
so the highlighted ply can become the live position (takeback) or the start of
a recorded game (the current game stays in history to resume later).
"""

from typing import List, Optional, Tuple

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.i18n import t


def players_support_takeback(player_manager) -> bool:
    """Whether both sides can take back, from a PlayerManager-shaped object.

    PlayerManager.supports_takeback is a @property returning bool. Calling it
    as a method raises TypeError: 'bool' object is not callable; the events
    thread catches that, and the long-press overlay never opens. Absent a
    player manager, takeback is treated as allowed (the row is still gated by
    ply vs tip).
    """
    if player_manager is None:
        return True
    return bool(player_manager.supports_takeback)


def takeback_is_available(
    *,
    selected_ply: Optional[int],
    num_plies: int,
    supports_takeback: bool,
) -> bool:
    """Whether "Take back to this position" can undo later moves.

    The highlighted ply stays as the last remaining move, so there is nothing
    to undo when it is already the tip. Players that cannot take back (Lichess)
    disable the row rather than offering an action that cannot complete.
    Selection 0 is the analysis view, not a move.
    """
    return (
        supports_takeback
        and selected_ply is not None
        and 1 <= selected_ply < num_plies
    )


def should_open_move_list_action_menu(*, reviewing: bool, long_tick: bool) -> bool:
    """True when a LONG_TICK during move-list review should open the overlay.

    A short TICK keeps paging coach text / refreshing. LONG_TICK outside review
    is a no-op (the board-layer abort for other held keys is unchanged).
    """
    return reviewing and long_tick


def history_to_transfer(*, positions: List[dict], ply: int) -> Optional[Tuple[str, List[str]]]:
    """Opening FEN plus UCIs through the highlighted ply, for a forked recorded game.

    ``history_positions()[0]`` is the start (no UCI). Entries 1..ply are the
    moves to keep. Returns None when there is nothing to persist:
    ``create_game_from_moves`` rejects an empty list, and starting from the
    ply's FEN alone drops the PGN and delays the database row until a later
    move is played.
    """
    if ply < 1 or ply >= len(positions):
        return None
    start_fen = positions[0].get("fen")
    if not start_fen:
        return None
    moves: List[str] = []
    for index in range(1, ply + 1):
        uci = positions[index].get("uci")
        if not uci:
            return None
        moves.append(uci)
    return start_fen, moves


def snapshot_analyses_for_positions(
    *,
    positions: List[dict],
    ply: int,
    live_lookup,
    stored_lookup=None,
) -> dict:
    """Analysis for start..ply, captured before the analysis service is reset.

    Resume starts a new game and clears the live cache; the new graph is
    restored from GameMove.eval_score. Live results win over the source
    game's stored rows because a search that just finished may not have
    been backfilled yet. Unanalysed plies are omitted so they stay NULL
    on the new rows rather than a fabricated 0.
    """
    if ply < 0 or not positions:
        return {}
    last = min(ply, len(positions) - 1)
    snapshot = {}
    for index in range(0, last + 1):
        fen = positions[index].get("fen")
        if not fen:
            continue
        result = live_lookup(fen)
        if result is None and stored_lookup is not None:
            result = stored_lookup(fen)
        if result is not None:
            snapshot[fen] = result
    return snapshot


def build_move_list_action_entries(*, takeback_enabled: bool) -> List[IconMenuEntry]:
    """Rows for the long-press-OK overlay on a highlighted move."""
    return [
        IconMenuEntry(
            key="takeback",
            label=t("move_list.take_back"),
            icon_name="undo",
            enabled=takeback_enabled,
        ),
        IconMenuEntry(
            key="new_game",
            label=t("move_list.new_game_from"),
            icon_name="play",
            enabled=True,
        ),
        IconMenuEntry(
            key="cancel",
            label=t("common.cancel"),
            icon_name="cancel",
            enabled=True,
        ),
    ]
