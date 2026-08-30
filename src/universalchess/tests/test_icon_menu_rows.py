#!/usr/bin/env python3
"""Entries that share an ``epaper.row`` sit side by side on one visual line.

Why these tests exist
---------------------
The e-paper main menu puts Positions and Settings on one row as half-width
buttons. IconMenuWidget was a vertical stack: each entry was a full-width
band, UP/DOWN walked that stack, and height was the sum of every entry's
ratio. Sharing a row without that being a first-class layout would either
stack the pair (no half-width) or invent a one-off in the main-menu builder.

How a regression manifests
-------------------------
Positions and Settings stack full-width again, or they share a y but the
menu starts scrolling because the pair still consumed two height units, or
UP/DOWN skips one of them.
"""

import sys
from unittest.mock import MagicMock

for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget


def _menu(entries, height=296):
    return IconMenuWidget(
        0, 0, 128, height, update_callback=lambda *a, **k: None, entries=entries
    )


def test_shared_row_places_two_entries_side_by_side():
    """Two consecutive entries with the same row sit on one line at half width.

    How the regression manifests: both buttons have x=0 and stacked y, or
    widths still equal the panel.
    """
    menu = _menu(
        [
            IconMenuEntry(key="play", label="PLAY", icon_name="home", height_ratio=2.0),
            IconMenuEntry(
                key="Positions", label="Positions", icon_name="positions", row="secondary"
            ),
            IconMenuEntry(
                key="Settings", label="Settings", icon_name="settings", row="secondary"
            ),
        ]
    )
    by_key = {button.key: button for button in menu._buttons}
    left = by_key["Positions"]
    right = by_key["Settings"]
    assert left.y == right.y
    assert left.x == 0
    assert right.x == left.width
    assert left.width + right.width == 128
    assert left.width == right.width
    play = by_key["play"]
    assert play.x == 0
    assert play.width == 128
    assert play.y < left.y


def test_shared_row_counts_as_one_height_unit():
    """A pair consumes one height_ratio, so PLAY stays twice as tall.

    How the regression manifests: the pair is laid out as two stacked 1.0
    rows, PLAY shrinks toward a third of the panel, and a later entry may
    fall off the screen.
    """
    menu = _menu(
        [
            IconMenuEntry(key="play", label="PLAY", icon_name="home", height_ratio=2.0),
            IconMenuEntry(
                key="Positions", label="Positions", icon_name="positions", row="secondary"
            ),
            IconMenuEntry(
                key="Settings", label="Settings", icon_name="settings", row="secondary"
            ),
        ]
    )
    by_key = {button.key: button for button in menu._buttons}
    assert by_key["play"].height >= 2 * by_key["Positions"].height
    assert by_key["Positions"].height == by_key["Settings"].height
    assert len(menu._buttons) == 3
    assert menu._visible_count == 2


def test_up_down_still_visits_both_halves_of_a_shared_row():
    """The board has no left/right keys, so DOWN still lands on each half.

    How the regression manifests: DOWN from PLAY jumps to Settings and
    Positions cannot be chosen, or the pair is one entry with two labels.
    """
    menu = _menu(
        [
            IconMenuEntry(key="play", label="PLAY", icon_name="home"),
            IconMenuEntry(key="Positions", label="Positions", icon_name="positions", row="pair"),
            IconMenuEntry(key="Settings", label="Settings", icon_name="settings", row="pair"),
        ]
    )
    assert menu.selected_index == 0
    assert menu._find_next_selectable(0, 1) == 1
    assert menu._find_next_selectable(1, 1) == 2
    assert menu._find_next_selectable(2, 1) == 0
