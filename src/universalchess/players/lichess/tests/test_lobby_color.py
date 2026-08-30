"""Color belongs to the lobby, beside Clock, and is White, Black, or Random.

The Players color control still swaps sides for engine games. A lobby Seek
New Game posts whatever the slots say, so a player-slot color could not
govern that seek -- the same defect Rated and Clock had. These tests pin the
row, the picker, and the write.
"""

from unittest.mock import MagicMock

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    build_lichess_color_picker_entries,
    build_lichess_menu_entries,
    handle_lichess_menu,
)
from universalchess.players.lichess.match import LICHESS_COLORS


class _FakeConnection:
    """Menu connection stand-in; counts closes like the real one is asked to."""

    def __init__(self):
        self.client = MagicMock()
        self.closes = 0

    def close(self):
        self.closes += 1


def _run_lobby(selections, picker_key=None, **kwargs):
    """Drive the lobby, answering Color with ``picker_key`` when a picker opens."""
    drawn = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **_kwargs):
            drawn.append(build_entries())
            for key in selections:
                handle_selection(MenuSelection.from_key(key))
                drawn.append(build_entries())
            return None

        def show_menu(self, entries, initial_index=0, **_kwargs):
            assert picker_key is not None
            return MenuSelection.from_key(picker_key)

    handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        **kwargs,
    )
    return drawn


def test_color_sits_directly_under_clock_in_the_lobby():
    """Color is the row under Clock, before Ongoing.

    Why: seek color was the Players control, so a lobby seek over two engines
    posted random and White/Black could not be chosen unless a slot was
    Lichess. How a regression manifests: Color is missing, or it sits on a
    player card instead of beside Clock.
    """
    entries = build_lichess_menu_entries(
        "alice", rated=False, clock="rapid_10_0", color="random"
    )

    assert [entry.key for entry in entries] == [
        "Account",
        "Rated",
        "Clock",
        "Color",
        "Ongoing",
        "Challenges",
        "NewGame",
    ]


def test_the_color_row_states_the_stored_choice():
    """The row's second line is the catalog label for the stored key.

    How a regression manifests: the label is fixed (every color renders the
    same), or it shows a Players-slot value such as player 1 White.
    """
    row = next(
        e
        for e in build_lichess_menu_entries("alice", color="white")
        if e.key == "Color"
    )

    assert row.label == "Color\nWhite"
    assert row.icon_name == "white_piece"
    assert row.selectable is True


def test_black_and_random_use_their_own_icons():
    """Black and Random must not share the White king icon.

    How a regression manifests: every choice uses white_piece, so the row
    cannot be read at a glance.
    """
    black = next(
        e for e in build_lichess_menu_entries("alice", color="black") if e.key == "Color"
    )
    random = next(
        e
        for e in build_lichess_menu_entries("alice", color="random")
        if e.key == "Color"
    )

    assert black.label == "Color\nBlack"
    assert black.icon_name == "black_piece"
    assert random.label == "Color\nRandom"
    assert random.icon_name == "random"


def test_color_picker_lists_random_white_and_black():
    """The picker is Random, White, Black -- the Board API color field.

    How a regression manifests: random is missing, or a fourth value appears
    that Lichess will reject.
    """
    keys = [entry.key for entry in build_lichess_color_picker_entries("random")]

    assert keys == list(LICHESS_COLORS)
    assert keys == ["random", "white", "black"]


def test_selecting_color_writes_the_picked_key():
    """Choosing the row opens the picker and persists the selection.

    How a regression manifests: nothing is written, or a player-slot color
    key is written instead of lichess_color.
    """
    written = []

    _run_lobby(
        ["Color"],
        picker_key="white",
        color_fn=lambda: "random",
        set_color_fn=written.append,
    )

    assert written == ["white"]


def test_the_lobby_redraws_color_from_the_stored_value_after_a_pick():
    """The row shows the new color without leaving the lobby.

    How a regression manifests: the label is captured once when the menu
    opens, so the pick appears to do nothing until the user re-enters.
    """
    stored = {"color": "random"}

    def set_color(value):
        stored["color"] = value

    drawn = _run_lobby(
        ["Color"],
        picker_key="black",
        color_fn=lambda: stored["color"],
        set_color_fn=set_color,
    )

    labels = [next(e.label for e in entries if e.key == "Color") for entries in drawn]
    assert labels == ["Color\nRandom", "Color\nBlack"]


def test_a_lobby_with_no_color_writer_leaves_the_row_inert():
    """Without an injected writer the row is drawn but changes nothing.

    How a regression manifests: an unwired lobby raises on selection and takes
    the menu thread down with it.
    """
    drawn = _run_lobby(["Color"])

    assert all(any(e.key == "Color" for e in entries) for entries in drawn)
