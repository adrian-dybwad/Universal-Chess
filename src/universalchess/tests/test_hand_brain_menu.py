"""Tests for the Hand+Brain mode selection entries.

Background / why these tests exist
----------------------------------
The mode-selection list was migrated onto the shared catalog ``hand_brain_mode``
option set, so the board and the web present the same modes and labels from one
source. These tests pin the keys, order, catalog labels, and the checked-icon
state that the board renderer relies on.
"""

from universalchess.menus.hand_brain_menu import build_hand_brain_mode_entries


def test_hand_brain_entries_use_catalog_keys_order_and_labels():
    """Entries must mirror the catalog hand_brain_mode set in order/keys/labels.

    How a regression manifests: reverting to the local map would render the
    short "Normal"/"Reverse" labels instead of the catalog's
    "Normal (Engine = Brain)"/"Reverse (Human = Brain)", or reorder/rekey the
    rows -- any of which changes this exact list.
    """
    entries = build_hand_brain_mode_entries("normal")

    assert [e.key for e in entries] == ["normal", "reverse"]
    assert [e.label for e in entries] == [
        "Normal (Engine = Brain)",
        "Reverse (Human = Brain)",
    ]


def test_hand_brain_entries_mark_current_mode_checked():
    """Only the active mode shows the checked icon; the other shows empty.

    How a regression manifests: an inverted or dropped current-mode check would
    leave the selected mode unmarked (or mark both), so the user could not tell
    which mode is active.
    """
    by_key = {e.key: e for e in build_hand_brain_mode_entries("reverse")}

    assert by_key["reverse"].icon_name == "checkbox_checked"
    assert by_key["normal"].icon_name == "checkbox_empty"
