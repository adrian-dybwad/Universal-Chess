"""Tests for the built-in coach definitions.

Why these tests exist
---------------------
The built-in coaches are the shipped defaults and the fallback when no user coach
is chosen. These tests guard that each one carries complete display metadata and
non-empty personas for both move contexts, and that ids are unique. A regression
(a blank persona, a missing character type, or a duplicated id) would surface as an
empty coaching prompt or a coach that silently shadows another in the registry.
"""

from universalchess.coaches import registry
from universalchess.coaches.base import CoachingSituation, MoveContext


def test_builtin_ids_are_unique_and_expected():
    # Duplicate ids would collide in the registry (one shadows another); the set
    # also documents the shipped roster.
    coaches = registry.discover_coaches(include_user=False)
    assert set(coaches) == {"dave", "myron", "sofia", "viktor"}


def test_each_builtin_has_complete_metadata_and_personas():
    # Every shipped coach must have display fields (for the selector) and both
    # personas (so neither the player nor opponent prompt is ever empty).
    coaches = registry.discover_coaches(include_user=False)
    for coach in coaches.values():
        assert coach.id
        assert coach.name
        assert coach.elo > 0
        assert coach.character_type
        assert coach.description
        player = coach.persona(CoachingSituation(move_context=MoveContext.PLAYER_MOVE))
        opponent = coach.persona(CoachingSituation(move_context=MoveContext.OPPONENT_MOVE))
        assert player.strip()
        assert opponent.strip()
        # Player and opponent personas are distinct styles, not the same text.
        assert player != opponent
