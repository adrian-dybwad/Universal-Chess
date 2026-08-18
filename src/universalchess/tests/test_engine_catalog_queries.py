"""Tests for the two engine questions the board's pickers ask.

"Which engines can I play against?" and "how strong can this one be set?" were
both answered inside the board's application module, each behind a module-level
cache, so neither could be asked from a test and neither was available to the
web. They live with the engines now.
"""

import pytest

from universalchess.managers import engine_manager
from universalchess.services import uci_schema


@pytest.fixture(autouse=True)
def forget_cached_answers():
    """Clear the process-lifetime caches so each test sees its own fixture."""
    engine_manager.forget_playable_engines()
    uci_schema.forget_strength_sections()
    yield
    engine_manager.forget_playable_engines()
    uci_schema.forget_strength_sections()


@pytest.fixture
def one_available_catalog_engine(monkeypatch):
    """A catalog where only Stockfish is available on this device."""
    manager = type("Manager", (), {"is_available": staticmethod(lambda name: name == "stockfish")})()
    monkeypatch.setattr(engine_manager, "get_engine_manager", lambda: manager)
    return manager


@pytest.fixture
def no_custom_engines(monkeypatch):
    """An empty custom-engine store."""
    from universalchess.services import custom_engine_registry

    monkeypatch.setattr(custom_engine_registry.CUSTOM_ENGINE_STORE, "list", lambda: [])


def test_only_engines_this_device_can_run_are_playable(
    one_available_catalog_engine, no_custom_engines
):
    """An engine in the catalog but not on the device is not offered.

    Why: selecting an engine with no binary starts a game that cannot make a
    move, and the board offers no way back except abandoning it. How a
    regression manifests: the picker lists every engine the project knows about,
    most of which are not installed.
    """
    assert engine_manager.playable_engines() == ["stockfish"]


def test_a_custom_engine_with_a_binary_is_playable(
    one_available_catalog_engine, monkeypatch
):
    """An operator-added engine is offered once its binary is present.

    Why: custom engines are not in the catalog, so the availability rule that
    covers catalog engines cannot see them; a present binary is what makes one
    selectable. How a regression manifests: an engine the operator installed by
    hand never appears in the picker.
    """
    from universalchess.services import custom_engine_registry
    from universalchess import paths

    custom = type("Custom", (), {"id": "myengine", "display_name": "My Engine"})()
    monkeypatch.setattr(custom_engine_registry.CUSTOM_ENGINE_STORE, "list", lambda: [custom])
    monkeypatch.setattr(paths, "get_engine_path", lambda name: "/engines/myengine")

    assert engine_manager.playable_engines() == ["myengine", "stockfish"]


def test_a_custom_engine_without_a_binary_is_not_playable(
    one_available_catalog_engine, monkeypatch
):
    """A custom engine whose binary is gone is not offered.

    Why: its registry entry outlives the file, so the entry alone does not mean
    the engine can play. How a regression manifests: a removed engine stays in
    the picker and the game it starts never moves.
    """
    from universalchess.services import custom_engine_registry
    from universalchess import paths

    custom = type("Custom", (), {"id": "ghost", "display_name": "Ghost"})()
    monkeypatch.setattr(custom_engine_registry.CUSTOM_ENGINE_STORE, "list", lambda: [custom])
    monkeypatch.setattr(paths, "get_engine_path", lambda name: None)

    assert engine_manager.playable_engines() == ["stockfish"]


def test_the_playable_set_is_computed_once(one_available_catalog_engine, no_custom_engines):
    """The answer is cached for the life of the process.

    Why: it probes the filesystem for every engine, and the picker is rebuilt on
    every menu redraw. How a regression manifests: the Players menu stutters
    while it re-reads the engines directory on each keypress.
    """
    calls = []
    original = one_available_catalog_engine.is_available
    one_available_catalog_engine.is_available = lambda name: (calls.append(name), original(name))[1]

    engine_manager.playable_engines()
    after_first = len(calls)
    engine_manager.playable_engines()

    assert after_first > 0
    assert len(calls) == after_first


def test_strength_sections_always_offer_a_default(monkeypatch):
    """Every engine offers Default, whatever the probe returned.

    Why: Default is the stored strength of a freshly configured player, so a
    list without it cannot render the current selection at all. How a regression
    manifests: a player set to Default shows a blank or wrong strength row.
    """
    monkeypatch.setattr(uci_schema, "seed_config", lambda name: "/config/x.uci")
    from universalchess.services import engine_profiles

    monkeypatch.setattr(
        engine_profiles, "strength_level_choices",
        lambda path: [{"value": "1400 ELO", "label": "1400 ELO"}],
    )

    sections = uci_schema.strength_sections_for_engine("stockfish")

    assert sections[0] == {"value": "Default", "label": "Default"}
    assert {"value": "1400 ELO", "label": "1400 ELO"} in sections


def test_an_unprobeable_engine_still_offers_default(monkeypatch):
    """An engine that cannot be probed offers Default and nothing invented.

    Why: the probe fails when the binary is missing or will not launch, and the
    picker must still render rather than raise on the menu thread. Listing
    strengths the engine may not support would be worse than listing only the
    one that always resolves. How a regression manifests: opening the strength
    picker for a broken engine crashes the menu, or offers rungs that do
    nothing.
    """
    def refuse(name):
        raise uci_schema.EngineProbeError("no binary")

    monkeypatch.setattr(uci_schema, "seed_config", refuse)

    assert uci_schema.strength_sections_for_engine("ghost") == [
        {"value": "Default", "label": "Default"}
    ]
