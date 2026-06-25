"""Tests for the player-config signature that gates rebuilding a running game.

Background / why these tests exist
----------------------------------
A board-reset new game (pieces returned to the start position) and a menu PLAY
that resumes a suspended game both reuse the player objects built when the game
started. Those objects capture the engine/type/elo at build time, so when a
player-defining setting is changed elsewhere (notably the engine, changed from
the web Settings page) the running game keeps using the OLD engine even though
the persisted settings and the in-memory ``AllSettings`` were refreshed.

The fix records the running game's player configuration as a signature
(``AllSettings.player_config_signature``) at game start and compares it against
the current settings when a new game is initiated; a difference forces a full
rebuild instead of reusing the stale players. These tests pin that signature:
the regression was that changing the engine produced NO observable difference,
so the new game silently reused the old engine.

The signature lives on ``AllSettings`` (pure settings logic) precisely so it is
testable without importing ``universalchess.main``, which performs heavy
hardware/display initialization at import time.
"""

import pytest

from universalchess.players.settings import AllSettings, GameSettings, PlayerSettings


def _make_settings(p1_overrides=None, p2_overrides=None) -> AllSettings:
    """Build an AllSettings with explicit player fields for signature checks."""
    p1 = dict(type="human", color="white", name="", engine="stockfish",
              elo="Default", hand_brain_mode="normal")
    p2 = dict(type="engine", color="black", name="", engine="stockfish",
              elo="Default", hand_brain_mode="normal")
    p1.update(p1_overrides or {})
    p2.update(p2_overrides or {})
    return AllSettings(
        player1=PlayerSettings(section="PlayerOne", **p1),
        player2=PlayerSettings(section="PlayerTwo", **p2),
        game=GameSettings(section="Game"),
    )


def test_signature_changes_when_player2_engine_changes():
    """Changing a player's engine must change the signature.

    This is the reported regression: switching Player 2 from stockfish to ct800
    produced no detectable difference, so the new game reused the old engine. If
    the engine field were dropped from the signature these would compare equal
    and the assertion fails.
    """
    stockfish = _make_settings(p2_overrides={"engine": "stockfish"})
    ct800 = _make_settings(p2_overrides={"engine": "ct800"})
    assert stockfish.player_config_signature() != ct800.player_config_signature()


def test_signature_ignores_name():
    """A name-only change must NOT change the signature.

    Names are cosmetic; rebuilding/abandoning a game because a player was renamed
    would be surprising. If name leaked into the signature, a rename would force a
    needless rebuild and this equality assertion fails.
    """
    a = _make_settings(p2_overrides={"engine": "ct800", "name": "Alice"})
    b = _make_settings(p2_overrides={"engine": "ct800", "name": "Bob"})
    assert a.player_config_signature() == b.player_config_signature()


# Each game-defining field, for both players, must affect the signature. Drift
# (forgetting to include a field) would let that field change silently reuse the
# old players, the exact failure mode the fix prevents.
_PLAYER_DEFINING_CHANGES = [
    ("p1", {"type": "engine"}),
    ("p1", {"color": "black"}),
    ("p1", {"engine": "ct800"}),
    ("p1", {"elo": "1500"}),
    ("p1", {"hand_brain_mode": "reverse"}),
    ("p2", {"type": "human"}),
    ("p2", {"color": "white"}),
    ("p2", {"engine": "ct800"}),
    ("p2", {"elo": "1500"}),
    ("p2", {"hand_brain_mode": "reverse"}),
]


@pytest.mark.parametrize("which,override", _PLAYER_DEFINING_CHANGES)
def test_signature_changes_for_each_player_defining_field(which, override):
    """Every game-defining field, for either player, changes the signature."""
    base = _make_settings()
    changed = (
        _make_settings(p1_overrides=override)
        if which == "p1"
        else _make_settings(p2_overrides=override)
    )
    assert base.player_config_signature() != changed.player_config_signature()


def test_identical_settings_have_equal_signatures():
    """Unchanged settings compare equal so a new game reuses players.

    Guards the efficient path: a board-reset new game with no settings change
    must not be seen as "changed" (which would force a needless teardown).
    """
    a = _make_settings(p2_overrides={"engine": "ct800", "elo": "1500"})
    b = _make_settings(p2_overrides={"engine": "ct800", "elo": "1500"})
    assert a.player_config_signature() == b.player_config_signature()


def test_signature_models_the_changed_detection():
    """A captured start signature differs from settings after an engine swap.

    Mirrors _player_config_changed_since_game_start at the pure level: the game
    started on stockfish (captured signature), settings were later reloaded to
    ct800, and the comparison must report a difference so the next new game
    rebuilds with ct800 instead of silently reusing stockfish.
    """
    started_on = _make_settings(p2_overrides={"engine": "stockfish"})
    captured = started_on.player_config_signature()
    reloaded = _make_settings(p2_overrides={"engine": "ct800"})
    assert reloaded.player_config_signature() != captured
