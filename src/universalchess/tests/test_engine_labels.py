"""Tests for the two engine labels the board's pickers draw.

Both were defined inside the application module, so the engine picker's row text
and the strength shown on the game card could only be checked by reading the
board. They are ordinary functions of their arguments and are tested as such.
"""

import pytest

from universalchess.menus.settings_menu import engine_picker_label
from universalchess.services import uci_schema


@pytest.fixture
def no_compatibility_data(monkeypatch):
    """No engine has a recorded Reverse Hand+Brain compatibility score."""
    from universalchess.players import hand_brain

    monkeypatch.setattr(hand_brain, "get_root_moves_compatibility", lambda name: None)


def test_the_selected_engine_is_marked(no_compatibility_data):
    """The current engine is prefixed so it is identifiable in the list.

    Why: the picker is a flat list with no other selection indicator, so
    without the marker the user cannot see which engine is configured. How a
    regression manifests: every row looks alike and the current choice is
    invisible.
    """
    assert engine_picker_label("stockfish", is_selected=True, show_compat=False) == "* stockfish"
    assert engine_picker_label("stockfish", is_selected=False, show_compat=False) == "stockfish"


def test_reverse_hand_brain_shows_the_compatibility_score(monkeypatch):
    """In Reverse Hand+Brain, a measured engine shows how often it obeyed.

    Why: Reverse mode depends on the engine honouring ``root_moves``, which not
    all do, so the picker states the measured percentage where it is known.
    That number is only meaningful in this context. How a regression manifests:
    the percentage disappears from the Reverse picker, or leaks into the
    ordinary engine picker where it is noise.
    """
    from universalchess.players import hand_brain

    monkeypatch.setattr(hand_brain, "get_root_moves_compatibility", lambda name: 87.4)

    assert engine_picker_label("stockfish", is_selected=False, show_compat=True) == \
        "stockfish (87%)"
    assert engine_picker_label("stockfish", is_selected=False, show_compat=False) == "stockfish"


def test_an_unmeasured_engine_shows_no_score(no_compatibility_data):
    """An engine with no recorded score shows no percentage at all.

    Why: an unmeasured engine is not a 0% engine, and printing a number would
    state a measurement that was never taken. How a regression manifests: newly
    installed engines appear to have failed a test they were never given.
    """
    assert engine_picker_label("newcomer", is_selected=False, show_compat=True) == "newcomer"


def test_the_stored_strength_section_resolves_to_what_the_engine_plays(monkeypatch):
    """The label shows the played strength, while the stored value stays put.

    Why: the player's ``elo`` is persisted as a section name (``Default``), but
    the game card and PGN have to say what that section actually plays -- an
    uncapped Default is "Unlimited". How a regression manifests: the card reads
    "Default", which tells the user nothing about the opponent's strength.
    """
    monkeypatch.setattr(uci_schema, "seed_config", lambda name: "/config/stockfish.uci")
    from universalchess.services import engine_profiles

    monkeypatch.setattr(
        engine_profiles, "strength_section_display", lambda path, section: "Unlimited"
    )

    assert uci_schema.strength_display_for_engine("stockfish", "Default") == "Unlimited"


def test_an_unprobeable_engine_falls_back_to_the_stored_section(monkeypatch):
    """When the config cannot be produced, the raw section is shown.

    Why: an engine whose binary is missing or unprobeable still has a stored
    section, and showing it is honest -- inventing a strength would name a
    rung the engine may not have. How a regression manifests: the card shows a
    fabricated strength, or raises on the game thread while starting a game.
    """
    def refuse(name):
        raise uci_schema.EngineProbeError("no binary")

    monkeypatch.setattr(uci_schema, "seed_config", refuse)

    assert uci_schema.strength_display_for_engine("ghost", "1400 ELO") == "1400 ELO"
