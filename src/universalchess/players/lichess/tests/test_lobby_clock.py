"""Clock belongs to the lobby, beside Rated, and lists only Board API clocks.

The Game clock still offers Blitz. Board API seeks of 5+0 are rejected as
Invalid time control, so the lobby holds a closed list: Rapid, Classical, and
None for correspondence. These tests pin the row, the picker, and the write.
"""

from unittest.mock import MagicMock

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    build_lichess_clock_picker_entries,
    build_lichess_seek_menu_entries,
    handle_lichess_menu,
)
from universalchess.players.lichess.match import LICHESS_CLOCKS


class _FakeConnection:
    """Menu connection stand-in; counts closes like the real one is asked to."""

    def __init__(self):
        self.client = MagicMock()
        self.closes = 0

    def close(self):
        self.closes += 1


def _run_lobby(selections, picker_key=None, **kwargs):
    """Drive the lobby, then the Seek New Game submenu, answering Clock with ``picker_key``."""
    drawn = []
    remaining = list(selections)

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **_kwargs):
            drawn.append(build_entries())
            while remaining:
                key = remaining[0]
                visible = {e.key for e in build_entries()}
                if key not in visible:
                    break
                remaining.pop(0)
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


def test_clock_sits_directly_under_rated_in_the_seek_submenu():
    """Clock is the row under Rated, before Color, on Seek New Game.

    Why: the seek clock was the Game setting, so a Blitz Game clock posted 5+0
    and Lichess never listed a hook. How a regression manifests: Clock is missing,
    or it sits on the Game tab instead of beside Rated.
    """
    entries = build_lichess_seek_menu_entries(rated=False, clock="rapid_10_0")

    assert [entry.key for entry in entries] == [
        "Rated",
        "Clock",
        "Color",
        "Seek",
    ]


def test_the_clock_row_states_the_stored_choice():
    """The row's second line is the catalog label for the stored key.

    How a regression manifests: the label is fixed (every clock renders the
    same), or it shows a Game-clock value such as 5 min (Blitz).
    """
    row = next(
        e
        for e in build_lichess_seek_menu_entries(clock="classical_30_0")
        if e.key == "Clock"
    )

    assert row.label == "Clock\n30|0 Classical"
    assert row.icon_name == "timer_checked"
    assert row.selectable is True


def test_none_clock_uses_the_untimed_icon():
    """Correspondence (None) must not look like a running clock.

    How a regression manifests: None still uses timer_checked, so it reads as a
    real-time seek on the lobby.
    """
    row = next(
        e for e in build_lichess_seek_menu_entries(clock="none") if e.key == "Clock"
    )

    assert row.label == "Clock\nNone"
    assert row.icon_name == "timer"


def test_clock_picker_lists_only_board_api_clocks():
    """The picker has None plus Rapid and Classical, and no Blitz or Bullet.

    How a regression manifests: blitz_5_0 appears, or none is missing, so the
    board can post a clock Lichess will reject.
    """
    keys = [entry.key for entry in build_lichess_clock_picker_entries("rapid_10_0")]

    assert keys == list(LICHESS_CLOCKS)
    assert "none" in keys
    assert "blitz_5_0" not in keys
    assert "bullet_1_0" not in keys


def test_selecting_clock_writes_the_picked_key():
    """Choosing the row opens the picker and persists the selection.

    How a regression manifests: nothing is written, or the Game clock key is
    written instead of lichess_clock.
    """
    written = []

    _run_lobby(
        ["NewGame", "Clock"],
        picker_key="none",
        clock_fn=lambda: "rapid_10_0",
        set_clock_fn=written.append,
    )

    assert written == ["none"]


def test_the_lobby_redraws_clock_from_the_stored_value_after_a_pick():
    """The row shows the new clock without leaving the lobby.

    How a regression manifests: the label is captured once when the menu
    opens, so the pick appears to do nothing until the user re-enters.
    """
    stored = {"clock": "rapid_10_0"}

    def set_clock(value):
        stored["clock"] = value

    drawn = _run_lobby(
        ["NewGame", "Clock"],
        picker_key="none",
        clock_fn=lambda: stored["clock"],
        set_clock_fn=set_clock,
    )

    labels = [
        next(e.label for e in entries if e.key == "Clock")
        for entries in drawn
        if any(e.key == "Clock" for e in entries)
    ]
    assert labels == ["Clock\n10|0 Rapid", "Clock\nNone"]


def test_a_lobby_with_no_clock_writer_leaves_the_row_inert():
    """Without an injected writer the row is drawn but changes nothing.

    How a regression manifests: an unwired lobby raises on selection and takes
    the menu thread down with it.
    """
    drawn = _run_lobby(["NewGame", "Clock"])

    assert all(
        any(e.key == "Clock" for e in entries)
        for entries in drawn
        if any(e.key == "Seek" for e in entries)
    )
