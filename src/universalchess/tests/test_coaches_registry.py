"""Tests for the coaches framework registry (discovery + selection).

Why these tests exist
---------------------
The coaches framework is a plugin system: built-in coaches ship in the package and
users add their own Python modules. These tests pin discovery (built-in + user
drop-ins, override-by-id, malformed-file skipping), Auto selection by opponent Elo,
and the whose-move / human-color logic that decides which persona a move gets. A
regression here would drop a user coach, crash discovery on one bad file, select
the wrong coach for a given strength, or send the player-move persona for an
opponent move (and vice versa).
"""

import textwrap

import pytest

from universalchess.coaches import registry
from universalchess.coaches.base import MoveContext


@pytest.fixture(autouse=True)
def _fresh_registry():
    # Each test starts from a clean cache so discovery reflects only what the test
    # sets up; otherwise a cached registry from another test would leak coaches.
    registry.refresh()
    yield
    registry.refresh()


def _write_coach(dir_path, filename, *, class_name, coach_id, name, elo):
    module = textwrap.dedent(
        f"""
        from universalchess.coaches.base import Coach

        class {class_name}(Coach):
            id = "{coach_id}"
            name = "{name}"
            elo = {elo}
            character_type = "Tester"
            description = "test coach"
            player_move_persona = "player persona {coach_id}"
            opponent_move_persona = "opponent persona {coach_id}"
        """
    )
    (dir_path / filename).write_text(module)


def test_builtin_coaches_are_discovered():
    # The four shipped coaches must always be present; a broken discovery or
    # package layout would drop them and leave the selector empty.
    coaches = registry.discover_coaches(include_user=False)
    assert set(coaches) == {"dave", "myron", "sofia", "viktor"}


def test_user_coach_is_discovered_from_directory(tmp_path):
    # A user Python module in the coaches folder must be picked up so the framework
    # is actually expandable; failure means user coaches never appear.
    _write_coach(tmp_path, "zed.py", class_name="Zed", coach_id="zed", name="Zed", elo=1500)
    coaches = registry.discover_coaches(user_dir=str(tmp_path))
    assert "zed" in coaches
    assert coaches["zed"].name == "Zed"


def test_user_coach_overrides_builtin_with_same_id(tmp_path):
    # A user coach sharing an id must override the built-in, so users can customize
    # a shipped coach by shadowing it rather than editing the package.
    _write_coach(
        tmp_path, "dave.py", class_name="MyDave", coach_id="dave", name="Custom Dave", elo=850
    )
    coaches = registry.discover_coaches(user_dir=str(tmp_path))
    assert coaches["dave"].name == "Custom Dave"


def test_malformed_user_module_is_skipped(tmp_path):
    # One bad file must not break discovery; the good coaches (built-in + other
    # user files) must still load. Regression: an import error would abort the
    # whole scan and disable coaching.
    (tmp_path / "broken.py").write_text("import does_not_exist_xyz\n")
    _write_coach(tmp_path, "ok.py", class_name="Ok", coach_id="ok", name="Ok", elo=1000)
    coaches = registry.discover_coaches(user_dir=str(tmp_path))
    assert "ok" in coaches
    assert "dave" in coaches  # built-ins unaffected
    assert "broken" not in coaches


def test_coach_class_with_blank_id_is_skipped(tmp_path):
    # A coach with no id cannot be selected or persisted; it must be skipped rather
    # than registered under an empty key that would shadow another coach.
    module = textwrap.dedent(
        """
        from universalchess.coaches.base import Coach

        class Nameless(Coach):
            name = "Nameless"
            elo = 1000
        """
    )
    (tmp_path / "nameless.py").write_text(module)
    coaches = registry.discover_coaches(user_dir=str(tmp_path))
    assert all(cid != "" for cid in coaches)


def test_list_coaches_sorted_by_elo():
    # The selector shows coaches weakest-first; a sort regression would scramble
    # the list. Uses the known built-in Elos.
    ids = [info["id"] for info in registry.list_coaches()]
    assert ids == ["dave", "myron", "sofia", "viktor"]


@pytest.mark.parametrize(
    "opponent_elo,expected",
    [
        (900, "dave"),      # closest to 800
        (1100, "myron"),    # 1250 is 150 away vs 800 is 300 away
        (1600, "sofia"),    # 1750 is 150 away vs 1250 is 350 away
        (3000, "viktor"),   # highest
        ("Default", "myron"),  # non-numeric -> target 1200 -> myron (50 away)
    ],
)
def test_resolve_coach_auto_picks_nearest_elo(opponent_elo, expected):
    # Auto must match coaching strength to the opponent; picking a far coach would
    # give a beginner an expert (or vice versa).
    coach = registry.resolve_coach(registry.AUTO, opponent_elo)
    assert coach.id == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (1500, 1500),          # already numeric
        (1500.0, 1500),        # float truncated
        ("1500", 1500),        # numeric string
        ("2850", 2850),
        ("1200 ELO", None),    # a profile identity is not a rating
        ("Profile-1", None),   # the case that made digit-scraping dangerous
        ("maia-1100.pb.gz", None),
        ("Default", None),
        ("Attacker", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_elo_only_accepts_an_actual_number(value, expected):
    # Why: this used to return the first run of digits found anywhere in the
    # value, because the seeded ladder spelled the Elo into the section name.
    # Profile identities are now generated, so digits inside one are not a rating
    # -- "Profile-1" returned 1, and Auto then sized the coach against a 1-rated
    # opponent with no error anywhere. Unresolvable values must read as unknown so
    # the caller applies DEFAULT_TARGET_ELO instead of a meaningless number.
    #
    # How a regression manifests: any of the non-numeric cases returns an int, and
    # the Auto coach silently mismatches the opposition.
    assert registry._parse_elo(value) == expected


@pytest.mark.parametrize(
    "profile_elo,expected",
    [
        (800, "dave"),      # 800 -> dave exactly
        (1200, "myron"),    # 1200 -> myron (1250, 50 away)
        (1100, "myron"),    # 1100 -> myron (150) beats dave (300)
        (2850, "viktor"),   # near-max ladder -> strongest coach
        (None, "myron"),    # unresolvable -> default target 1200
    ],
)
def test_resolve_coach_auto_matches_the_profiles_stored_elo(profile_elo, expected):
    # End-to-end: the rating comes from the opponent profile's own values, looked
    # up through the injected resolver, and drives Auto selection. This guards the
    # whole path resolve_opponent_elo -> resolve_coach for what the app actually
    # persists (an identity, not a number); a regression would fall back to the
    # default target for every engine opponent and always coach mid-strength.
    p1 = {"type": "human", "elo": "Default"}
    p2 = {"type": "engine", "engine": "stockfish", "elo": "Profile-a3f19c"}

    opponent_elo = registry.resolve_opponent_elo(
        p1, p2, profile_elo=lambda engine, selection: profile_elo
    )

    assert registry.resolve_coach(registry.AUTO, opponent_elo).id == expected


def test_resolve_coach_explicit_id_wins_over_elo():
    # An explicit selection must be honored regardless of opponent Elo, or the
    # user's choice would be silently ignored.
    coach = registry.resolve_coach("viktor", 400)
    assert coach.id == "viktor"


def test_resolve_coach_unknown_id_falls_back_to_auto():
    # A stale/removed coach id (e.g. a deleted user coach) must not break coaching;
    # it falls back to Elo-based selection.
    coach = registry.resolve_coach("deleted_coach", 900)
    assert coach.id == "dave"


def test_resolve_coach_off_disables_coaching():
    # "off" is the master switch on the Coach selector: it must resolve to no coach
    # regardless of the roster or opponent Elo, so coaching is turned off entirely
    # (distinct from AUTO/an unknown id, which pick an Elo-matched coach). A
    # regression that treated "off" like an unknown id would fall back to Elo-based
    # selection and keep coaching on when the user asked for it off.
    assert registry.resolve_coach(registry.OFF, 900) is None
    assert registry.resolve_coach_info(registry.OFF, 900) is None


def test_resolve_coach_info_returns_display_fields():
    # The coach card shows the resolved coach; info must carry the display fields.
    info = registry.resolve_coach_info(registry.AUTO, 1600)
    assert info["id"] == "sofia"
    assert info["name"] == "Sofia"
    assert info["elo"] == 1750
    assert info["character_type"] == "Silent Partner"


@pytest.mark.parametrize(
    "p1_type,p1_color,p2_type,p2_color,expected",
    [
        ("human", "white", "engine", "black", "white"),
        ("engine", "white", "human", "black", "black"),
        ("engine", "white", "engine", "black", None),
        ("human", "white", "human", "black", None),
    ],
)
def test_resolve_human_color(p1_type, p1_color, p2_type, p2_color, expected):
    # Human color decides player vs opponent persona; wrong detection swaps the
    # coaching voice. Two humans / two engines have no single human perspective.
    p1 = {"type": p1_type, "color": p1_color}
    p2 = {"type": p2_type, "color": p2_color}
    assert registry.resolve_human_color(p1, p2) == expected


@pytest.mark.parametrize(
    "p1,p2,expected",
    [
        ({"type": "human", "elo": "0"}, {"type": "engine", "elo": "1500"}, 1500),
        ({"type": "engine", "elo": "1800"}, {"type": "human", "elo": "0"}, 1800),
        ({"type": "engine", "elo": "1400"}, {"type": "engine", "elo": "1600"}, 1600),
        ({"type": "human", "elo": "0"}, {"type": "human", "elo": "0"}, None),
    ],
)
def test_resolve_opponent_elo_picks_the_engine_sides_rating(p1, p2, expected):
    # Auto selection is driven by the opponent (engine) Elo; picking the wrong
    # side's Elo would match the coach to the human instead of the opposition. A
    # slot storing a bare number needs no profile lookup, so these resolve without
    # one -- and the result is an int, not the raw stored text.
    assert registry.resolve_opponent_elo(p1, p2) == expected


def test_the_opponents_rating_is_read_from_its_engines_profile():
    # A slot stores a profile identity, so the rating has to be looked up in that
    # engine's config -- which is why the engine is part of the lookup. How a
    # regression manifests: the resolver is called with the wrong engine (or not
    # at all) and every engine opponent reads as unknown.
    calls = []

    def profile_elo(engine, selection):
        calls.append((engine, selection))
        return 1700

    p1 = {"type": "human", "elo": "Default"}
    p2 = {"type": "engine", "engine": "maia", "elo": "Profile-7b2e01"}

    assert registry.resolve_opponent_elo(p1, p2, profile_elo=profile_elo) == 1700
    assert calls == [("maia", "Profile-7b2e01")]


@pytest.mark.parametrize(
    "slot",
    [
        {"type": "engine", "engine": "", "elo": "Profile-a3f19c"},  # no engine named
        {"type": "engine", "engine": "maia", "elo": ""},            # no strength stored
        {"type": "engine", "engine": "maia", "elo": "   "},
    ],
)
def test_an_incomplete_opponent_slot_is_reported_as_unknown(slot):
    # The empty cases: with no engine or no selection there is nothing to look up,
    # so no resolver call may happen and the answer must be unknown. Calling the
    # lookup with an empty engine would seed/probe nothing and, worse, any number
    # returned here would be a fabricated rating.
    def fail(engine, selection):
        raise AssertionError(f"resolver must not be called with {engine!r}/{selection!r}")

    assert registry.resolve_opponent_elo(
        {"type": "human", "elo": "0"}, slot, profile_elo=fail
    ) is None


@pytest.mark.parametrize(
    "is_potential,side,human_color,expected",
    [
        (True, "black", "white", MoveContext.PLAYER_MOVE),   # hint is always the player's
        (False, "white", "white", MoveContext.PLAYER_MOVE),  # human's own played move
        (False, "black", "white", MoveContext.OPPONENT_MOVE),  # opponent's move
        (False, "white", None, MoveContext.OPPONENT_MOVE),   # no known human -> opponent
    ],
)
def test_select_move_context(is_potential, side, human_color, expected):
    # This mapping picks which persona a move receives; a regression would coach
    # the opponent's blunder with the player-guidance voice, or vice versa.
    assert registry.select_move_context(is_potential, side, human_color) == expected


def test_resolve_persona_uses_opponent_persona_for_opponent_move():
    # End-to-end: an opponent's played move must yield the coach's opponent persona.
    persona = registry.resolve_persona(
        "dave", 800, human_color="white", is_potential_move=False, side_to_move="black"
    )
    assert persona == registry.get_coach("dave").opponent_move_persona


def test_resolve_persona_uses_player_persona_for_hint():
    # A hint must yield the player persona regardless of side to move.
    persona = registry.resolve_persona(
        "myron", 1250, human_color="black", is_potential_move=True, side_to_move="white"
    )
    assert persona == registry.get_coach("myron").player_move_persona
