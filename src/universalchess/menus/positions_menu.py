"""Positions menu helpers."""

from typing import Dict, List, Callable, Optional, Tuple

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.i18n import t
from universalchess.managers.menu import MenuSelection, is_break_result

# Overlay / user-authored positions live in this INI section. Their names are
# the user's, so they are never looked up in the bundle.
CUSTOM_CATEGORY = "custom"

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


def prettify_ini_id(ident: str) -> str:
    """Turn an INI section or entry key into the English title-case it used to show.

    Packaged ids are translated instead; this is the fallback for a custom
    overlay entry or any id the bundle has not seen, so the panel never shows
    the raw lookup key.
    """
    return ident.replace("_", " ").title()


def _localized_or_prettify(prefix: str, ident: str) -> str:
    """Translate ``prefix.ident`` when the bundle has it, else title-case ``ident``.

    ``t()`` returns the key itself when nothing is in the bundle, which would
    draw ``positions.item.my_trap`` on a custom row. Title-case is the previous
    rendering and is what the user typed, once normalised to an INI key.
    """
    key = f"{prefix}.{ident}"
    translated = t(key)
    if translated == key:
        return prettify_ini_id(ident)
    return translated


def localized_category_label(category: str) -> str:
    """Category name for the Positions menu, in the device language."""
    return _localized_or_prettify("positions.category", category)


def localized_position_label(name: str, *, category: str = "") -> str:
    """Position name for a Positions row, in the device language.

    Custom overlay names are the user's and are title-cased rather than looked
    up, so a saved "My trap" cannot pick up a packaged translation that happens
    to share the key.
    """
    if category == CUSTOM_CATEGORY:
        return prettify_ini_id(name)
    return _localized_or_prettify("positions.item", name)


def position_unavailable_message() -> str:
    """Why a stored position cannot be started, in the device language."""
    return t("positions.unavailable_with_lichess")


def position_unavailable_with_lichess(player1_type: str, player2_type: str) -> bool:
    """True when a custom FEN cannot be started because a slot is Lichess.

    Lichess seek, challenge, and ongoing join always start from the opening.
    A Positions setup or Play-from-here would put this board on a different
    game than the remote opponent.
    """
    return player1_type == "lichess" or player2_type == "lichess"


def build_category_entries(positions: Dict[str, Dict[str, Tuple[str, str]]]) -> List[IconMenuEntry]:
    """Build category menu entries."""
    category_icons = CATEGORY_ICONS
    category_entries: List[IconMenuEntry] = []
    for category in positions.keys():
        display_name = localized_category_label(category)
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
        display_name = localized_position_label(name, category=category)
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
        IconMenuEntry(key="confirm", label=t("positions.end_game"), icon_name="exit", enabled=True),
        IconMenuEntry(key="cancel", label=t("common.cancel"), icon_name="cancel", enabled=True),
    ]
    return show_menu(entries, initial_index=1)


def handle_positions_menu(
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
    lichess_as_player: Optional[Callable[[], bool]] = None,
    show_alert: Optional[Callable[[str], None]] = None,
) -> Optional[bool]:
    """Handle the Positions submenu.

    When ``is_game_in_progress`` reports a resumable game, selecting a position
    first asks for confirmation (setting up a position discards that game). On
    confirm the running game is recorded as aborted via ``abort_game`` (DB
    result = "*") before the new position is set up; on cancel the running game
    is left untouched and the menu stays open.

    When ``lichess_as_player`` is true, selecting a position shows
    :func:`position_unavailable_message` and does not start or abort.
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
            display_name = localized_position_label(position_result, category=category)
            last_position_category_ref[0] = category
            last_position_index_ref[0] = find_entry_index(position_entries, position_result)

            if lichess_as_player is not None and lichess_as_player():
                if show_alert is not None:
                    show_alert(position_unavailable_message())
                continue

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

