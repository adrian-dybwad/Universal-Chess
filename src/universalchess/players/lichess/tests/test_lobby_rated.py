"""Rated belongs to the lobby, beside the account that plays the rated game.

Rated was a row on the Players slot, shown only while that slot was set to
Lichess, even though it has always been stored globally
(``game.lichess_rated``). A lobby Seek New Game posts a seek whatever the slots
say, so with no Lichess slot the toggle governing that seek could not be seen or
changed anywhere -- the same defect the account had before it moved here.

These tests pin the row to the lobby, directly under Account, and pin its
reads/writes to the stored value.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    build_lichess_menu_entries,
    handle_lichess_menu,
)


class _FakeConnection:
    """Menu connection stand-in; counts closes like the real one is asked to."""

    def __init__(self):
        self.client = MagicMock()
        self.closes = 0

    def close(self):
        self.closes += 1


def _run_lobby(selections, **kwargs):
    """Drive the lobby menu through ``selections``, returning the built entries.

    ``run_menu_loop`` is given a builder it calls once per iteration, so the
    entries are captured before and after each selection -- which is how a
    toggle that does not redraw is caught.
    """
    drawn = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **_kwargs):
            drawn.append(build_entries())
            for key in selections:
                handle_selection(MenuSelection.from_key(key))
                drawn.append(build_entries())
            return None

    handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        **kwargs,
    )
    return drawn


def test_rated_sits_directly_under_account_in_the_lobby():
    """The lobby lists Account, Rated, Ongoing, Challenges, Seek New Game.

    Rated is what the seek is posted as, so it belongs beside the account that
    posts it rather than on a player slot that a lobby seek does not consult.

    How a regression manifests: the key list loses Rated (the toggle is back on
    the player card, unreachable when no slot is Lichess) or it drifts away from
    the account it qualifies.
    """
    entries = build_lichess_menu_entries("alice", rated=False)

    assert [entry.key for entry in entries] == [
        "Account",
        "Rated",
        "Ongoing",
        "Challenges",
        "NewGame",
    ]


@pytest.mark.parametrize(
    "rated,expected_label,expected_icon",
    [
        (True, "Rated\nOn", "checkbox_checked"),
        (False, "Rated\nOff", "checkbox_empty"),
    ],
)
def test_the_rated_row_states_what_the_next_seek_will_be(
    rated, expected_label, expected_icon
):
    """The row reads its stored value, in text and in the checkbox icon.

    A toggle that renders the same either way is a control the user cannot read:
    rated and casual games affect the account's rating, so the state has to be
    visible before Seek New Game is pressed.

    How a regression manifests: the label or icon is fixed (both states render
    identically), or the two disagree.
    """
    row = next(e for e in build_lichess_menu_entries("alice", rated=rated) if e.key == "Rated")

    assert row.label == expected_label
    assert row.icon_name == expected_icon
    assert row.selectable is True


def test_selecting_rated_writes_the_opposite_of_the_stored_value():
    """Choosing the row toggles the persisted setting.

    How a regression manifests: nothing is written (the row looks dead), or the
    current value is written back, which leaves casual games casual forever.
    """
    written = []

    _run_lobby(
        ["Rated"],
        rated_fn=lambda: False,
        set_rated_fn=written.append,
    )

    assert written == [True]


def test_the_lobby_redraws_rated_from_the_stored_value_after_a_toggle():
    """The row shows the new state without leaving the lobby.

    The value is read through the getter on every redraw, so the row reflects
    what was just written. How a regression manifests: the label is captured
    once when the menu opens, so the toggle appears to do nothing until the user
    leaves and re-enters the lobby.
    """
    stored = {"rated": False}

    def set_rated(value):
        stored["rated"] = value

    drawn = _run_lobby(
        ["Rated"],
        rated_fn=lambda: stored["rated"],
        set_rated_fn=set_rated,
    )

    labels = [next(e.label for e in entries if e.key == "Rated") for entries in drawn]
    assert labels == ["Rated\nOff", "Rated\nOn"]


def test_a_lobby_with_no_rated_writer_leaves_the_row_inert():
    """Without an injected writer the row is drawn but changes nothing.

    Matches the Account row, which is listed whether or not a binder was
    injected. How a regression manifests: an unwired lobby raises on selection
    and takes the menu thread down with it.
    """
    drawn = _run_lobby(["Rated"])

    assert all(any(e.key == "Rated" for e in entries) for entries in drawn)
