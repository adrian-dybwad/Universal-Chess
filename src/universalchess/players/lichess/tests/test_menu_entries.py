#!/usr/bin/env python3
"""The Lichess Settings menu is the lobby: Account first, then always-visible play rows.

Why these tests exist
---------------------
Lichess Settings opens this list directly (no nested Play page). Account is the
first row and opens the account picker. Ongoing Games and Challenges are always
listed (selecting them shows how they work, then the live list). Seek New Game
opens Rated, Clock, Color, and Seek. Accounts (add/delete) is the last row of
the picker, not a lobby sibling. These tests pin order and that Account can be
activated.
"""

from unittest.mock import MagicMock

from universalchess.menus.catalog.loader import load_catalog
from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    ACCOUNTS_MENU_KEY,
    DEFAULT_ACCOUNT_MENU_KEY,
    build_lichess_account_picker_entries,
    build_lichess_menu_entries,
    build_lichess_seek_menu_entries,
    handle_lichess_menu,
)


class _FakeConnection:
    """Menu connection stand-in; the lobby closes it on the way out."""

    def __init__(self):
        self.client = MagicMock()
        self.closes = 0

    def close(self):
        self.closes += 1


def test_lichess_settings_rows_are_always_in_order():
    """Account, Ongoing, Challenges, New Game — none of them vanish.

    Why: ongoing/challenges used to vanish when the account had none, so they
    could not be opened to read how they work, and Rated/Clock/Color sat on
    the lobby mixed with join. Regression: a row drops, Play/Accounts
    returns as a lobby sibling, Token returns, or Rated sits on the lobby.
    """
    entries = build_lichess_menu_entries("alice")
    assert [e.key for e in entries] == [
        "Account",
        "Ongoing",
        "Challenges",
        "NewGame",
    ]


def test_seek_new_game_opens_rated_clock_color_and_seek():
    """Seek New Game is a submenu, not an immediate seek.

    Why: Rated, Clock and Color used to sit on the lobby, so posting a seek
    mixed with join and the settings could be changed without intending to
    seek. How a regression manifests: NewGame posts a seek without opening
    the submenu, or the submenu loses Rated/Clock/Color/Seek.
    """
    started = []
    drawn = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            entries = build_entries()
            drawn.append([e.key for e in entries])
            if "Seek" in {e.key for e in entries}:
                return None
            return handle_selection(MenuSelection.from_key("NewGame"))

    handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda config: started.append(config) or True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert started == []
    assert drawn == [
        ["Account", "Ongoing", "Challenges", "NewGame"],
        ["Rated", "Clock", "Color", "Seek"],
    ]


def test_ongoing_and_challenges_carry_how_they_work_help():
    """Ongoing and Challenges must expose help copy for the help screen.

    Why: selecting either row (or HELP on it) shows how the feature works. The
    copy is the catalog's, which the web card also renders, so the board cannot
    drift from it or keep an untranslated second version. How a regression
    manifests: help is None, or it is a copy of the text rather than the node's.
    """
    catalog = load_catalog()
    by_key = {e.key: e for e in build_lichess_menu_entries("alice")}
    seek = {e.key: e for e in build_lichess_seek_menu_entries()}
    assert by_key["Ongoing"].help == catalog.get_node("lichess.ongoing")["help"]
    assert by_key["Challenges"].help == catalog.get_node("lichess.challenges")["help"]
    assert seek["Rated"].help == catalog.get_node("field.lichess.rated")["help"]
    assert seek["Clock"].help == catalog.get_node("field.lichess.clock")["help"]
    assert seek["Color"].help == catalog.get_node("field.lichess.color")["help"]
    assert seek["Seek"].help == catalog.get_node("lichess.seek")["help"]
    assert by_key["Ongoing"].selectable is True
    assert by_key["Challenges"].selectable is True
    assert by_key["NewGame"].selectable is True
    assert seek["Seek"].selectable is True


def test_account_row_is_selectable_account_picker():
    """Account is shown, focused, and activatable so it can open the picker.

    Why: a non-selectable header cannot open the picker. Regression: selectable
    False (skipped in navigation), enabled False (hidden/greyed), or the label
    dropping the username.
    """
    account = next(
        e
        for e in build_lichess_menu_entries("alice")
        if e.key == "Account"
    )
    assert account.label == "Account\nalice"
    assert account.enabled is True
    assert account.selectable is True


def test_account_picker_marks_the_bound_choice_and_maps_default_key():
    """The picker is a radio list plus Accounts; unbound Default uses a non-empty key.

    Why: IconMenuEntry cannot use an empty key, but the slot stores Default as
    "". Accounts is last and is not a radio (it opens the credential manager).
    Regression: Default missing, radio on the wrong row, empty key, or Accounts
    missing / treated as a bindable choice.
    """
    entries = build_lichess_account_picker_entries(
        [
            ("", "Default account", True),
            ("org:bob", "lichess.org:Bob", False),
        ]
    )
    assert [e.key for e in entries] == [
        DEFAULT_ACCOUNT_MENU_KEY,
        "org:bob",
        ACCOUNTS_MENU_KEY,
    ]
    assert entries[0].label == "Default account"
    assert entries[0].trailing_icon_name == "radio_checked"
    assert entries[1].trailing_icon_name == "radio_empty"
    assert entries[-1].label == "Accounts"
    assert entries[-1].trailing_icon_name is None
    assert all(e.selectable is True and e.enabled is True for e in entries)


def test_account_picker_still_lists_accounts_when_there_are_no_credentials():
    """An empty choice list must still offer Accounts so a login can be added.

    Why: the picker used to return immediately with no rows. How a regression
    manifests: the Accounts row is absent when choices is empty.
    """
    entries = build_lichess_account_picker_entries([])
    assert [e.key for e in entries] == [ACCOUNTS_MENU_KEY]
    assert entries[0].label == "Accounts"
