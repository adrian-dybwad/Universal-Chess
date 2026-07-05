"""Positions menu helpers."""

from typing import Dict, List, Callable, Optional, Tuple

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result

# Maps a positions.ini section name to the icon shown for that category.
# The themed endgame sections all reuse the endgame icon; any section not listed
# here falls back to the generic "positions" icon.
CATEGORY_ICONS = {
    "test": "positions_test",
    "puzzles": "positions_puzzles",
    "endgames": "positions_endgames",
    "pawn_endgames": "positions_endgames",
    "rook_endgames": "positions_endgames",
    "queen_endgames": "positions_endgames",
    "minor_piece_endgames": "positions_endgames",
    "basic_mates": "positions_endgames",
    "endgame_studies": "positions_endgames",
    "custom": "positions_custom",
}


def build_category_entries(positions: Dict[str, Dict[str, Tuple[str, str]]]) -> List[IconMenuEntry]:
    """Build category menu entries."""
    category_icons = CATEGORY_ICONS
    category_entries: List[IconMenuEntry] = []
    for category in positions.keys():
        display_name = category.replace("_", " ").title()
        count = len(positions[category])
        icon_name = category_icons.get(category, "positions")
        category_entries.append(
            IconMenuEntry(
                key=category,
                label=f"{display_name}\n({count})",
                icon_name=icon_name,
                enabled=True,
                font_size=14,
                height_ratio=1.5,
            )
        )
    return category_entries


def _wrap_display_name(display_name: str) -> Tuple[str, int, float]:
    """Wrap display name into lines and return text, line count, and height ratio."""
    if len(display_name) <= 11:
        return display_name, 1, 1.0

    max_line_width = 10
    wrapped_lines: List[str] = []
    words = display_name.split()
    current_line = ""

    for word in words:
        if not current_line:
            current_line = word
        elif len(current_line) + 1 + len(word) <= max_line_width:
            current_line += " " + word
        else:
            wrapped_lines.append(current_line)
            current_line = word
    if current_line:
        wrapped_lines.append(current_line)

    num_lines = len(wrapped_lines)
    if num_lines <= 1:
        height_ratio = 1.0
    elif num_lines == 2:
        height_ratio = 1.5
    else:
        height_ratio = 2.0

    return "\n".join(wrapped_lines), num_lines, height_ratio


def build_position_entries(
    category: str, positions: Dict[str, Tuple[str, str]], category_icons: Dict[str, str]
) -> List[IconMenuEntry]:
    """Build position entries for a category."""
    entries: List[IconMenuEntry] = []
    for name, fen in positions.items():
        display_name = name.replace("_", " ").title()
        wrapped_text, _, height_ratio = _wrap_display_name(display_name)

        if category == "test":
            if "en_passant" in name:
                position_icon = "en_passant"
            elif "castling" in name:
                position_icon = "castling"
            elif "promotion" in name:
                position_icon = "promotion"
            else:
                position_icon = "positions_test"
        else:
            position_icon = category_icons.get(category, "positions")

        entries.append(
            IconMenuEntry(
                key=name,
                label=wrapped_text,
                icon_name=position_icon,
                enabled=True,
                font_size=12,
                height_ratio=height_ratio,
            )
        )
    return entries


def _confirm_end_running_game(
    show_menu: Callable[[List[IconMenuEntry], int], str],
):
    """Ask the player to confirm ending the in-progress game.

    Returns the raw menu result so the caller can both detect confirmation
    (``"confirm"``) and propagate break/shutdown results unchanged. The cursor
    defaults to Cancel so an accidental confirm cannot silently discard a game.
    """
    entries = [
        IconMenuEntry(key="confirm", label="End Game?", icon_name="exit", enabled=True),
        IconMenuEntry(key="cancel", label="Cancel", icon_name="cancel", enabled=True),
    ]
    return show_menu(entries, initial_index=1)


def handle_positions_menu(
    ctx,
    load_positions_config: Callable[[], Dict[str, Dict[str, Tuple[str, str]]]],
    start_from_position: Callable[[str, str, Optional[str]], bool],
    show_menu: Callable[[List[IconMenuEntry], int], str],
    find_entry_index: Callable[[List[IconMenuEntry], str], int],
    board,
    log,
    last_position_category_index_ref: List[int],
    last_position_index_ref: List[int],
    last_position_category_ref: List[Optional[str]],
    return_to_last_position: bool = False,
    is_game_in_progress: Optional[Callable[[], bool]] = None,
    abort_game: Optional[Callable[[], None]] = None,
) -> Optional[bool]:
    """Handle the Positions submenu.

    When ``is_game_in_progress`` reports a resumable game, selecting a position
    first asks for confirmation (setting up a position discards that game). On
    confirm the running game is recorded as aborted via ``abort_game`` (DB
    result = "*") before the new position is set up; on cancel the running game
    is left untouched and the menu stays open.
    """
    positions = load_positions_config()
    if not positions:
        log.warning("[Positions] No positions available")
        board.beep(board.SOUND_WRONG_MOVE, event_type="error")
        return False

    category_entries = build_category_entries(positions)
    category_icons = CATEGORY_ICONS

    last_category_index = last_position_category_index_ref[0]
    skip_category_menu = return_to_last_position and last_position_category_ref[0] is not None

    while True:
        if skip_category_menu:
            category_result = last_position_category_ref[0]
            skip_category_menu = False
        else:
            category_result = show_menu(category_entries, initial_index=last_category_index)
            if is_break_result(category_result):
                return category_result
            if category_result in ["BACK", "SHUTDOWN", "HELP"]:
                return False

        last_category_index = find_entry_index(category_entries, category_result)
        last_position_category_index_ref[0] = last_category_index

        category = category_result
        if category not in positions:
            continue

        position_entries = build_position_entries(category, positions[category], category_icons)

        if return_to_last_position and category == last_position_category_ref[0]:
            initial_position_index = last_position_index_ref[0]
        else:
            initial_position_index = 0

        position_result = show_menu(position_entries, initial_index=initial_position_index)

        if is_break_result(position_result):
            return position_result
        if position_result in ["BACK", "HELP"]:
            last_position_category_ref[0] = None
            continue
        elif position_result == "SHUTDOWN":
            return False

        if position_result in positions[category]:
            fen, hint_move = positions[category][position_result]
            display_name = position_result.replace("_", " ").title()
            last_position_category_ref[0] = category
            last_position_index_ref[0] = find_entry_index(position_entries, position_result)

            if is_game_in_progress is not None and is_game_in_progress():
                confirm_result = _confirm_end_running_game(show_menu)
                if is_break_result(confirm_result):
                    return confirm_result
                if confirm_result != "confirm":
                    # Player declined; keep the running game and stay in the menu.
                    continue
                if abort_game is not None:
                    abort_game()

            if start_from_position(fen, display_name, hint_move):
                return True

    return False

