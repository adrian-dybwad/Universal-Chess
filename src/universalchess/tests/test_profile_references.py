"""Tests for locating and repairing settings references to engine profiles.

Why these tests exist
---------------------
Three settings keys name an engine's ``.uci`` profile section (both player slots'
``elo`` and the Centaur proxy's ``level``), and nothing enforced the
relationship: deleting or re-seeding a profile left those keys naming a section
that no longer existed. The failure was silent --
``EnginePlayer._load_uci_options`` finds no section and falls back to the
engine-wide ``[DEFAULT]``, so the board played at a strength nobody chose with no
error anywhere. Each test below states the specific dangling case it guards and
how the regression would show.

The settings reads/writes are injected, so these exercise the reference logic
against an in-memory store rather than a ``centaur.ini`` on disk.
"""

import json

import pytest

from universalchess.services import profile_references as pr
from universalchess.services.centaur_engine_proxy.config import (
    CONFIG_SECTION as CENTAUR_SECTION,
    ENGINE_KEY as CENTAUR_ENGINE_KEY,
    LEVEL_KEY as CENTAUR_LEVEL_KEY,
    OPTIONS_KEY as CENTAUR_OPTIONS_KEY,
)
from universalchess.players.settings import PLAYER1_SECTION, PLAYER2_SECTION

ENGINE = "stockfish"
OTHER_ENGINE = "maia"
RUNG = "1400 ELO"
CUSTOM = "Attacker"
DEFAULT = "Default"


class FakeSettings:
    """An in-memory ``centaur.ini`` exposing the read/write pair under test."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def read(self, section, key, default=""):
        return self.values.get((section, key), default)

    def write(self, section, key, value):
        self.values[(section, key)] = str(value)
        self.writes.append((section, key, str(value)))
        return True

    def kwargs(self):
        """Injection kwargs for the module's public functions."""
        return {"read_setting": self.read, "write_setting": self.write}


def slot_pointing_at(section, engine, profile):
    """Settings entries for a player slot bound to ``engine`` at ``profile``."""
    return {(section, "engine"): engine, (section, "elo"): profile}


def centaur_pointing_at(engine, level, options=None):
    """Settings entries for the Centaur card, including its resolved-options cache."""
    return {
        (CENTAUR_SECTION, CENTAUR_ENGINE_KEY): engine,
        (CENTAUR_SECTION, CENTAUR_LEVEL_KEY): level,
        (CENTAUR_SECTION, CENTAUR_OPTIONS_KEY): json.dumps(options or {}),
    }


class TestFindReferences:
    def test_all_three_referrer_sites_are_found(self):
        # The whole point of the module is that the referrer set is complete; a
        # site dropped from REFERENCE_SITES would leave that key dangling forever,
        # and the regression shows here as fewer than three references.
        store = FakeSettings({
            **slot_pointing_at(PLAYER1_SECTION, ENGINE, RUNG),
            **slot_pointing_at(PLAYER2_SECTION, ENGINE, RUNG),
            **centaur_pointing_at(ENGINE, RUNG),
        })

        found = pr.find_references(ENGINE, RUNG, read_setting=store.read)

        assert [reference.description for reference in found] == [
            f"{PLAYER1_SECTION}.elo",
            f"{PLAYER2_SECTION}.elo",
            f"{CENTAUR_SECTION}.{CENTAUR_LEVEL_KEY}",
        ]
        assert {reference.profile for reference in found} == {RUNG}
        assert {reference.engine for reference in found} == {ENGINE}
        assert all(reference.repointed_to is None for reference in found)

    def test_a_slot_on_another_engine_is_not_a_reference(self):
        # Profile names are only unique within an engine: both Stockfish and Maia
        # can have a "1400 ELO". Ignoring the engine key would repoint an
        # unrelated slot, which shows as that slot silently dropping to Default.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, OTHER_ENGINE, RUNG))

        assert pr.find_references(ENGINE, RUNG, read_setting=store.read) == []

    def test_a_reference_stored_in_a_different_case_is_found(self):
        # The .uci writers resolve a section name case-insensitively, so a slot
        # storing "1400 elo" really does play section "1400 ELO". Matching only
        # exactly would leave that slot dangling after the section is renamed.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, ENGINE, "1400 elo"))

        found = pr.find_references(ENGINE, RUNG, read_setting=store.read)

        assert len(found) == 1
        assert found[0].profile == "1400 elo"

    @pytest.mark.parametrize(
        ("engine", "profile"), [("", RUNG), (ENGINE, ""), ("", "")]
    )
    def test_an_empty_engine_or_profile_matches_nothing(self, engine, profile):
        # An unset engine/profile must not match the empty stored values of an
        # unconfigured slot: doing so would repoint slots that were never bound to
        # the mutated profile at all.
        store = FakeSettings({(PLAYER1_SECTION, "engine"): "", (PLAYER1_SECTION, "elo"): ""})

        assert pr.find_references(engine, profile, read_setting=store.read) == []


class TestRepairDangling:
    def test_a_deleted_profile_is_repointed_to_default(self):
        # The original bug: deleting a profile left the slot naming a missing
        # section, and the engine fell back to [DEFAULT] at game start with no
        # error. The regression shows as the slot keeping the deleted name.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, ENGINE, CUSTOM))

        changed = pr.repair_dangling(ENGINE, [DEFAULT, RUNG], **store.kwargs())

        assert [reference.description for reference in changed] == [f"{PLAYER1_SECTION}.elo"]
        assert changed[0].profile == CUSTOM
        assert changed[0].repointed_to == DEFAULT
        assert store.read(PLAYER1_SECTION, "elo") == DEFAULT

    def test_a_reference_that_still_resolves_is_untouched(self):
        # "Reset profiles" re-derives the ladder from a fresh probe, which usually
        # regenerates the same rungs. Repointing unconditionally would reset every
        # slot to Default on a reset that changed nothing for them.
        store = FakeSettings({
            **slot_pointing_at(PLAYER1_SECTION, ENGINE, RUNG),
            **slot_pointing_at(PLAYER2_SECTION, ENGINE, DEFAULT),
        })

        assert pr.repair_dangling(ENGINE, [DEFAULT, RUNG], **store.kwargs()) == []
        assert store.writes == []

    def test_case_differences_do_not_count_as_dangling(self):
        # A slot storing "1400 elo" resolves to section "1400 ELO" at game start,
        # so it is not dangling. A case-sensitive comparison would repoint a
        # working slot to Default on any unrelated profile mutation.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, ENGINE, "1400 elo"))

        assert pr.repair_dangling(ENGINE, [DEFAULT, RUNG], **store.kwargs()) == []

    def test_every_dangling_site_is_repaired_in_one_pass(self):
        # A reset discards custom profiles, so all three sites can dangle at once.
        # Repairing only the first would leave the rest silently on [DEFAULT].
        store = FakeSettings({
            **slot_pointing_at(PLAYER1_SECTION, ENGINE, CUSTOM),
            **slot_pointing_at(PLAYER2_SECTION, ENGINE, "Defender"),
            **centaur_pointing_at(ENGINE, CUSTOM, {"OwnAttack": 125}),
        })

        changed = pr.repair_dangling(
            ENGINE,
            [DEFAULT, RUNG],
            resolve_options=lambda engine, profile: {},
            **store.kwargs(),
        )

        assert [reference.description for reference in changed] == [
            f"{PLAYER1_SECTION}.elo",
            f"{PLAYER2_SECTION}.elo",
            f"{CENTAUR_SECTION}.{CENTAUR_LEVEL_KEY}",
        ]
        assert all(reference.repointed_to == DEFAULT for reference in changed)
        assert store.read(PLAYER1_SECTION, "elo") == DEFAULT
        assert store.read(PLAYER2_SECTION, "elo") == DEFAULT
        assert store.read(CENTAUR_SECTION, CENTAUR_LEVEL_KEY) == DEFAULT
        assert json.loads(store.read(CENTAUR_SECTION, CENTAUR_OPTIONS_KEY)) == {}

    def test_without_a_resolver_the_option_cache_is_left_alone(self):
        # Overwriting the cache with an empty map would mean "the engine's own
        # defaults", silently changing how the proxy plays. Leaving the stale
        # cache is the lesser evil, so no options write may be issued.
        store = FakeSettings(centaur_pointing_at(ENGINE, CUSTOM, {"OwnAttack": 125}))

        pr.repair_dangling(ENGINE, [DEFAULT], **store.kwargs())

        assert store.writes == [(CENTAUR_SECTION, CENTAUR_LEVEL_KEY, DEFAULT)]
        assert json.loads(store.read(CENTAUR_SECTION, CENTAUR_OPTIONS_KEY)) == {"OwnAttack": 125}

    def test_an_unset_reference_is_not_repaired(self):
        # A slot with no strength stored has nothing to dangle; writing Default
        # into it would make an untouched slot look configured and would fire a
        # settings broadcast on every profile edit.
        store = FakeSettings({
            (PLAYER1_SECTION, "engine"): ENGINE,
            (PLAYER1_SECTION, "elo"): "",
        })

        assert pr.repair_dangling(ENGINE, [DEFAULT], **store.kwargs()) == []
        assert store.writes == []

    def test_another_engines_dangling_reference_is_left_for_that_engine(self):
        # Mutating Stockfish's profiles says nothing about Maia's. Repairing
        # across engines would repoint a slot whose own .uci was never touched.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, OTHER_ENGINE, CUSTOM))

        assert pr.repair_dangling(ENGINE, [DEFAULT], **store.kwargs()) == []

    def test_repairing_with_no_profiles_left_still_repoints_to_default(self):
        # The empty case: an engine whose config could not be re-seeded reports no
        # profiles. Default is seeded for every probed engine, so it remains the
        # correct target; returning early on an empty list would leave the
        # dangling reference in place.
        store = FakeSettings(slot_pointing_at(PLAYER1_SECTION, ENGINE, CUSTOM))

        changed = pr.repair_dangling(ENGINE, [], **store.kwargs())

        assert len(changed) == 1
        assert store.read(PLAYER1_SECTION, "elo") == DEFAULT
