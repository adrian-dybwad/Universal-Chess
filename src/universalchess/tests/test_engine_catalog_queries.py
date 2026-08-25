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
        lambda path, projection: [{"value": "Profile-a1b2c3", "label": "1400 ELO"}],
    )

    sections = uci_schema.strength_sections_for_engine("stockfish")

    assert sections[0] == {"value": "Default", "label": "Default"}
    assert {"value": "Profile-a1b2c3", "label": "1400 ELO"} in sections


@pytest.fixture
def seeded_engine_configs(tmp_path, monkeypatch):
    """Two engines with real ``.uci`` files, counting probes per engine."""
    for engine in ("stockfish", "maia"):
        (tmp_path / f"{engine}.uci").write_text(
            "[DEFAULT]\nThreads = 1\n\n"
            "[Default]\nUCI_LimitStrength = false\n\n"
            "[1200 ELO]\nUCI_LimitStrength = true\nUCI_Elo = 1200\n",
            encoding="utf-8",
        )
    probes = []

    def seed(name):
        probes.append(name)
        return str(tmp_path / f"{name}.uci")

    monkeypatch.setattr(uci_schema, "seed_config", seed)
    return tmp_path, probes


def test_editing_an_engines_profiles_drops_its_cached_strength_rows(
    seeded_engine_configs,
):
    """A profile write invalidates the picker rows built from that file.

    Why this exists: the rows were cached for the life of the process and nothing
    invalidated them, so deleting or renaming a profile left the on-device
    strength picker offering the pre-edit ladder until the app restarted --
    selecting a removed rung then stored a reference resolving to nothing.

    How a regression manifests: the deleted rung is still listed after the edit,
    which is what the value assertion below catches (a count check would not, the
    row count changing for other reasons).
    """
    tmp_path, _ = seeded_engine_configs
    from universalchess.services import engine_profiles

    before = uci_schema.strength_sections_for_engine("stockfish")
    assert "1200 ELO" in [row["value"] for row in before]

    engine_profiles.delete_profile(str(tmp_path / "stockfish.uci"), "1200 ELO")

    after = uci_schema.strength_sections_for_engine("stockfish")
    assert [row["value"] for row in after] == ["Default"]


def test_editing_one_engine_keeps_another_engines_cached_rows(seeded_engine_configs):
    """Only the edited engine's rows are dropped.

    Why this exists: rebuilding rows can require probing an engine binary (a
    process launch), so invalidating every engine on any write would put that
    cost on the menu thread for engines that did not change.

    How a regression manifests: the probe count for the untouched engine rises
    above one after an unrelated engine is edited.
    """
    tmp_path, probes = seeded_engine_configs
    from universalchess.services import engine_profiles

    uci_schema.strength_sections_for_engine("maia")
    engine_profiles.delete_profile(str(tmp_path / "stockfish.uci"), "1200 ELO")
    uci_schema.strength_sections_for_engine("maia")

    assert probes == ["maia"]


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
