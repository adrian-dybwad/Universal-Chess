"""Tests for the Player menu entry composition.

Background / why these tests exist
----------------------------------
Player-type display text was migrated off a private value->label map onto the
shared catalog ``player_type`` option set, so the board and the web render the
same labels from one source. These tests pin that the board entries pick up the
catalog labels (notably the longer "Hand + Brain" form) and that an unknown
type still degrades to legible text rather than blank.
"""

from universalchess.menus.players_menu import _player_type_label, build_player1_menu_entries


def _player(**overrides):
    base = {
        "color": "white",
        "type": "human",
        "name": "",
        "engine": "stockfish",
        "elo": "1500",
        "hand_brain_mode": "normal",
    }
    base.update(overrides)
    return base


def test_player_type_label_comes_from_catalog():
    """_player_type_label must return the catalog labels, not a local map.

    How a regression manifests: if the function reverts to a hardcoded map, the
    Hand+Brain label returns the old abbreviated "H+B" instead of the catalog's
    "Hand + Brain", so this assertion fails. Human/Engine/Lichess are pinned too
    because they share the same lookup path.
    """
    assert _player_type_label("human") == "Human"
    assert _player_type_label("engine") == "Engine"
    assert _player_type_label("lichess") == "Lichess"
    assert _player_type_label("hand_brain") == "Hand + Brain"


def test_player_type_label_unknown_falls_back_to_capitalized():
    """An unrecognised type must degrade to a capitalised form, never blank.

    How a regression manifests: a missing default would return an empty string
    (blank board row) for a type absent from the catalog; this guards that the
    value stays visible.
    """
    assert _player_type_label("martian") == "Martian"


def test_player1_type_row_uses_catalog_label():
    """The Player 1 Type row renders the catalog player-type label.

    Why this test exists: the entry label is built as "Type\\n<label>" where the
    label now comes from the catalog. This enters through the public builder (not
    the private helper) so it guards the rendered row, which is what the user
    sees on the board.

    How a regression manifests: a re-hardcoded map would render "Type\\nH+B"
    instead of "Type\\nHand + Brain", failing this assertion.
    """
    entries = build_player1_menu_entries(_player(type="hand_brain"))
    type_entry = next(e for e in entries if e.key == "Type")
    assert type_entry.label == "Type\nHand + Brain"
