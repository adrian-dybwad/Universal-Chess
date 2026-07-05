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
        ("1200 ELO", 1200),    # seeded ELO-ladder section name
        ("800 ELO", 800),
        ("maia-1100.pb.gz", 1100),  # net file level: first number in the name
        ("weights_1900.pb.gz", 1900),
        ("Default", None),     # no number -> unknown (falls back to target)
        ("Attacker", None),    # custom personality name -> unknown
        ("", None),
        (None, None),
    ],
)
def test_parse_elo_derives_number_from_selection(value, expected):
    # The app stores the strength *selection* (section name), not a bare Elo.
    # _parse_elo is the single place that turns "1200 ELO"/a Maia filename into a
    # number for Auto coach matching. A regression here would send every
    # non-numeric selection to the default target, so a 800-ELO or 2850-ELO
    # opponent would wrongly get the mid-strength coach.
    assert registry._parse_elo(value) == expected


@pytest.mark.parametrize(
    "opponent_elo,expected",
    [
        ("800 ELO", "dave"),        # 800 -> dave exactly
        ("1200 ELO", "myron"),      # 1200 -> myron (1250, 50 away)
        ("maia-1100.pb.gz", "myron"),  # 1100 -> myron (150) beats dave (300)
        ("2850 ELO", "viktor"),     # near-max ladder -> strongest coach
        ("Attacker", "myron"),      # unnumbered selection -> default target 1200
    ],
)
def test_resolve_coach_auto_matches_section_name_strength(opponent_elo, expected):
    # End-to-end: a stored section name (the seeded ELO ladder or a Maia net
    # level) must drive Auto selection just like a bare number. This guards the
    # whole path resolve_coach -> _parse_elo for the values the app actually
    # persists; a regression would ignore the ladder and mis-match the coach.
    coach = registry.resolve_coach(registry.AUTO, opponent_elo)
    assert coach.id == expected


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
        ({"type": "human", "elo": "0"}, {"type": "engine", "elo": "1500"}, "1500"),
        ({"type": "engine", "elo": "1800"}, {"type": "human", "elo": "0"}, "1800"),
        ({"type": "engine", "elo": "1400"}, {"type": "engine", "elo": "1600"}, "1600"),
        ({"type": "human", "elo": "0"}, {"type": "human", "elo": "0"}, None),
    ],
)
def test_resolve_opponent_elo(p1, p2, expected):
    # Auto selection is driven by the opponent (engine) Elo; picking the wrong
    # side's Elo would match the coach to the human instead of the opposition.
    assert registry.resolve_opponent_elo(p1, p2) == expected


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
