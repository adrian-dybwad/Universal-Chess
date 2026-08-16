#!/usr/bin/env python3
"""The Lichess top menu omits empty sections instead of disabling them.

Why these tests exist
---------------------
The menu previously used ``enabled=False`` to hide the Ongoing/Challenges rows
when the account had none, and marked the username header ``enabled=False`` --
which, under the old widget behavior, hid the header entirely (a latent bug: the
"User\\n<name>" line never showed). Hiding a row must be done by omission, and a
header must be a visible, non-selectable row. These tests pin both.
"""

from universalchess.players.lichess.lobby import build_lichess_menu_entries


def test_sections_present_when_account_has_them():
    """With ongoing games and challenges, all five rows build in order.

    Regression: dropping a row (or re-adding an enabled-based hide) changes the
    key list, so the user cannot reach Ongoing/Challenges.
    """
    entries = build_lichess_menu_entries("alice", ongoing_games=True, has_challenges=True)
    assert [e.key for e in entries] == ["User", "NewGame", "Ongoing", "Challenges", "Token"]


def test_empty_sections_are_omitted_not_disabled():
    """With no ongoing games or challenges, those rows are absent (not greyed).

    Regression: if the rows are added disabled instead of omitted, they appear
    greyed with nothing to open; if the header hide-bug returns, "User" vanishes.
    """
    entries = build_lichess_menu_entries("alice", ongoing_games=False, has_challenges=False)
    assert [e.key for e in entries] == ["User", "NewGame", "Token"]


def test_username_header_is_a_visible_nonselectable_row():
    """The username header renders (enabled) but cannot be focused (selectable=False).

    Regression: marking it ``enabled=False`` hid it under the old widget rules;
    this asserts it is a proper header -- shown, but skipped in navigation.
    """
    header = build_lichess_menu_entries("alice", ongoing_games=True, has_challenges=True)[0]
    assert header.key == "User"
    assert header.label == "User\nalice"
    assert header.enabled is True
    assert header.selectable is False
