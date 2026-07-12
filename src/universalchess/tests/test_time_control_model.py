"""Tests for the pure time-control model (state/time_control.py).

Why these tests exist
---------------------
The chess clock was extended from "symmetric minutes-per-player sudden death"
to a structured time control supporting Fischer increment, delay (simple/US and
Bronstein), tournament stages, and asymmetric per-side times. This module is the
pure, UI-independent core: it must correctly answer the queries the clock relies
on (initial time per side, per-move increment, stage-boundary base additions) and
resolve game settings (legacy minutes / named preset / custom fields) into a
TimeControl. A regression here silently mis-times every game, so the arithmetic
is pinned deterministically without any clock threading.
"""

from types import SimpleNamespace

import pytest

from universalchess.state.time_control import (
    PRESETS,
    DelayMode,
    Stage,
    TimeControl,
    build_time_control,
    list_presets,
    time_control_change_requires_reconfigure,
)


def _settings(**overrides):
    """Minimal settings stand-in for build_time_control.

    build_time_control reads plain attributes, so a namespace with the same
    field names as GameSettings exercises the real resolution logic without
    constructing the full settings dataclass or touching centaur.ini.
    """
    base = {
        "time_control": 0,
        "time_control_preset": "",
        "tc_custom_base_seconds": 300,
        "tc_custom_increment_seconds": 0,
        "tc_custom_delay_seconds": 0,
        "tc_custom_delay_mode": "none",
        "tc_custom_asymmetric": False,
        "tc_custom_black_base_seconds": 300,
        "tc_custom_black_increment_seconds": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# DelayMode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("none", DelayMode.NONE),
    ("simple", DelayMode.SIMPLE),
    ("bronstein", DelayMode.BRONSTEIN),
    ("SIMPLE", DelayMode.SIMPLE),
    ("", DelayMode.NONE),
    ("garbage", DelayMode.NONE),
])
def test_delay_mode_from_str(raw, expected):
    """DelayMode.from_str must be case-insensitive and default to NONE.

    Why: the mode is persisted as a free string in centaur.ini and chosen from a
    board menu; an unknown/blank value must degrade to "no delay" rather than
    raise, otherwise a stray config value would crash clock configuration.
    """
    assert DelayMode.from_str(raw) is expected


# ---------------------------------------------------------------------------
# Basic timed / untimed / symmetric properties
# ---------------------------------------------------------------------------

def test_untimed_control_is_not_timed():
    """A zero-time sudden-death control is reported untimed.

    Why: the clock only runs a countdown in timed mode; if is_timed were True
    for a 0-second control the countdown thread would immediately flag.
    """
    tc = TimeControl.sudden_death_minutes(0)
    assert tc.is_timed is False
    assert tc.initial_seconds("white") == 0
    assert tc.initial_seconds("black") == 0


def test_sudden_death_symmetric():
    """5-minute sudden death yields 300s per side, no increment/delay.

    Why: this is the legacy behavior every existing game relies on; the new
    model must reproduce it exactly.
    """
    tc = TimeControl.sudden_death_minutes(5)
    assert tc.is_timed is True
    assert tc.is_symmetric is True
    assert tc.initial_seconds("white") == 300
    assert tc.initial_seconds("black") == 300
    assert tc.delay_mode is DelayMode.NONE
    assert tc.increment_after_move("white", 1) == 0
    assert tc.base_added_after_move("white", 1) == 0


def test_fischer_increment_applies_every_move():
    """Fischer increment is returned for each move in the (single) stage.

    Why: Fischer is the most common online control; the clock adds this value to
    the mover after every move. How a regression manifests: if increment were
    tied to a stage boundary instead of every move, move 2+ would return 0 and
    players would lose their increment.
    """
    tc = TimeControl.fischer_minutes(5, increment_seconds=3)
    assert tc.initial_seconds("white") == 300
    for move in (1, 2, 10, 99):
        assert tc.increment_after_move("white", move) == 3
        # Single stage -> never a base addition on any move boundary.
        assert tc.base_added_after_move("white", move) == 0


# ---------------------------------------------------------------------------
# Stages (tournament controls)
# ---------------------------------------------------------------------------

def _tournament():
    """40 moves/90 min, then 30 min sudden death, 30s increment throughout.

    Mirrors the classic FIDE control so stage math is tested against a real
    tournament format.
    """
    return TimeControl.symmetric(
        stages=(Stage(moves=40, base_seconds=5400, increment_seconds=30),
                Stage(moves=0, base_seconds=1800, increment_seconds=30)),
    )


def test_tournament_initial_time_is_first_stage_base():
    """Initial time equals the first stage's base only (not the sum of stages).

    Why: the second-stage 30 minutes must be granted only when a player reaches
    move 40, not up front. How a regression manifests: returning 5400+1800 would
    hand players the whole game's time at the start.
    """
    tc = _tournament()
    assert tc.initial_seconds("white") == 5400


def test_tournament_stage_base_added_on_boundary_move_only():
    """Reaching move 40 adds the next stage's base; other moves add nothing.

    Why: this is the defining behavior of staged controls -- the extra time is
    granted exactly once, when the move requirement is met. How a regression
    manifests: an off-by-one boundary would add the 30 minutes on move 39 or 41,
    or repeatedly.
    """
    tc = _tournament()
    assert tc.base_added_after_move("white", 39) == 0
    assert tc.base_added_after_move("white", 40) == 1800
    assert tc.base_added_after_move("white", 41) == 0
    # Increment still applies on every move including the boundary.
    assert tc.increment_after_move("white", 40) == 30
    assert tc.increment_after_move("white", 41) == 30


def test_multi_stage_boundaries_are_cumulative():
    """40/120, 20/60, then 30 min: bases add at cumulative moves 40 and 60.

    Why: boundaries in tournament play are cumulative per player, not per-stage
    counts. How a regression manifests: if the second boundary were computed as
    move 20 instead of 40+20=60, the final time control would trigger far too
    early.
    """
    tc = TimeControl.symmetric(
        stages=(Stage(moves=40, base_seconds=7200, increment_seconds=0),
                Stage(moves=20, base_seconds=3600, increment_seconds=0),
                Stage(moves=0, base_seconds=1800, increment_seconds=0)),
    )
    assert tc.base_added_after_move("white", 40) == 3600
    assert tc.base_added_after_move("white", 50) == 0
    assert tc.base_added_after_move("white", 60) == 1800
    assert tc.base_added_after_move("white", 61) == 0


# ---------------------------------------------------------------------------
# Asymmetric
# ---------------------------------------------------------------------------

def test_asymmetric_times_differ_per_side():
    """Asymmetric control reports different base times per color.

    Why: time-odds games need each side configured independently. How a
    regression manifests: if initial_seconds ignored color, both sides would get
    white's time.
    """
    tc = TimeControl(
        white_stages=(Stage(moves=0, base_seconds=300, increment_seconds=2),),
        black_stages=(Stage(moves=0, base_seconds=60, increment_seconds=0),),
    )
    assert tc.is_symmetric is False
    assert tc.initial_seconds("white") == 300
    assert tc.initial_seconds("black") == 60
    assert tc.increment_after_move("white", 1) == 2
    assert tc.increment_after_move("black", 1) == 0


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tc,expected", [
    (TimeControl.sudden_death_minutes(0), "Untimed"),
    (TimeControl.sudden_death_minutes(5), "5 min"),
    (TimeControl.fischer_minutes(5, 3), "5 min + 3 sec"),
    (TimeControl.symmetric(
        stages=(Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.SIMPLE),
     "5 min, 3 sec delay"),
    (TimeControl.symmetric(
        stages=(Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.BRONSTEIN),
     "5 min, 3 sec Bronstein"),
])
def test_describe_symmetric(tc, expected):
    """describe() renders human-readable summaries for common controls.

    Why: the board menu and displays show this text; it must be stable and
    correct. How a regression manifests: wrong units or dropped increment/delay
    would mislead the player about the active control.
    """
    assert tc.describe() == expected


def test_describe_asymmetric_shows_both_sides():
    """Asymmetric describe() shows each side distinctly.

    Why: a single summary would hide that the sides differ. The exact format is
    pinned so display code can rely on it.
    """
    tc = TimeControl(
        white_stages=(Stage(0, 300, 0),),
        black_stages=(Stage(0, 60, 0),),
    )
    assert tc.describe() == "White 5 min / Black 1 min"


def test_describe_tournament_shows_stages():
    """Tournament describe() lists the staged requirement.

    Why: staged controls are only available as presets; their label must convey
    the moves/time structure.
    """
    tc = _tournament()
    assert tc.describe() == "40 moves/90 min, then 30 min + 30 sec"


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------

def test_presets_are_registered_and_described():
    """Every preset has a key, label, non-empty description, and TimeControl.

    Why: the board menu provider renders label + description per preset; a blank
    description or missing TimeControl would produce an unusable menu entry or a
    crash when the preset is selected.
    """
    presets = list_presets()
    assert len(presets) == len(PRESETS)
    assert len(presets) >= 15  # covers untimed + bullet/blitz/rapid/classical + delay + tournament
    keys = {p.key for p in presets}
    assert "untimed" in keys
    assert "blitz_5_3" in keys
    for preset in presets:
        assert preset.key
        assert preset.label
        assert preset.description
        assert isinstance(preset.time_control, TimeControl)


def test_preset_blitz_5_3_is_fischer():
    """The 5|3 blitz preset resolves to 300s base + 3s increment.

    Why: pins a representative Fischer preset so registry edits that break the
    mapping are caught.
    """
    tc = PRESETS["blitz_5_3"].time_control
    assert tc.initial_seconds("white") == 300
    assert tc.increment_after_move("white", 1) == 3


# ---------------------------------------------------------------------------
# build_time_control (settings resolution)
# ---------------------------------------------------------------------------

def test_build_falls_back_to_legacy_minutes_when_no_preset():
    """Empty preset uses legacy game.time_control minutes (backward compat).

    Why: existing configs only set time_control; they must keep working
    unchanged. How a regression manifests: ignoring the legacy field would make
    every upgraded board start untimed.
    """
    tc = build_time_control(_settings(time_control=10, time_control_preset=""))
    assert tc.initial_seconds("white") == 600
    assert tc.is_symmetric is True
    assert tc.delay_mode is DelayMode.NONE


def test_build_uses_named_preset():
    """A preset key resolves to the registry's TimeControl.

    Why: the primary configuration path is preset selection; the resolver must
    map the stored key to the correct control.
    """
    tc = build_time_control(_settings(time_control_preset="blitz_5_3"))
    assert tc.initial_seconds("white") == 300
    assert tc.increment_after_move("white", 1) == 3


def test_build_unknown_preset_falls_back_to_legacy():
    """An unknown preset key falls back to legacy minutes rather than crashing.

    Why: a stale/renamed preset value in centaur.ini must not brick clock
    configuration. How a regression manifests: a KeyError here would abort game
    start.
    """
    tc = build_time_control(_settings(time_control_preset="does_not_exist", time_control=3))
    assert tc.initial_seconds("white") == 180


def test_build_custom_symmetric():
    """Custom preset builds from tc_custom_* fields (symmetric).

    Why: the board custom builder writes these scalar fields; the resolver must
    assemble them into base + increment + delay-mode.
    """
    tc = build_time_control(_settings(
        time_control_preset="custom",
        tc_custom_base_seconds=180,
        tc_custom_increment_seconds=2,
        tc_custom_delay_seconds=5,
        tc_custom_delay_mode="bronstein",
    ))
    assert tc.initial_seconds("white") == 180
    assert tc.initial_seconds("black") == 180
    assert tc.increment_after_move("white", 1) == 2
    assert tc.delay_seconds == 5
    assert tc.delay_mode is DelayMode.BRONSTEIN


def test_build_custom_asymmetric_uses_black_fields():
    """Custom asymmetric builds distinct per-side base/increment.

    Why: the asymmetric toggle plus black fields must produce differing sides.
    How a regression manifests: ignoring the black fields would silently mirror
    white's time, defeating time-odds setup.
    """
    tc = build_time_control(_settings(
        time_control_preset="custom",
        tc_custom_asymmetric=True,
        tc_custom_base_seconds=300,
        tc_custom_increment_seconds=0,
        tc_custom_black_base_seconds=120,
        tc_custom_black_increment_seconds=1,
    ))
    assert tc.is_symmetric is False
    assert tc.initial_seconds("white") == 300
    assert tc.initial_seconds("black") == 120
    assert tc.increment_after_move("black", 1) == 1


# ---------------------------------------------------------------------------
# Live-reapply predicate: time_control_change_requires_reconfigure
# ---------------------------------------------------------------------------

_TC_5MIN = TimeControl.sudden_death_minutes(5)
_TC_10MIN = TimeControl.sudden_death_minutes(10)
# Same base time as _TC_5MIN but a different delay ("timer") mode -- the exact
# change the user reports. It must be seen as different so the live clock is
# reconfigured, even though the displayed minutes are unchanged.
_TC_5MIN_BRONSTEIN = TimeControl.symmetric(
    (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.BRONSTEIN
)


@pytest.mark.parametrize(
    "current,desired,game_has_moves,expected",
    [
        # No moves + control differs -> reconfigure so the live clock/display
        # adopts the change made from the web or board menu.
        (_TC_5MIN, _TC_10MIN, False, True),
        # No moves + only the delay/"timer" mode differs (same minutes) -> still
        # reconfigure: this is the reported regression, and value equality on the
        # frozen dataclass must treat the delay_mode change as different.
        (_TC_5MIN, _TC_5MIN_BRONSTEIN, False, True),
        # No moves + control already matches -> nothing to do (avoid needlessly
        # resetting the seeded clock).
        (_TC_5MIN, _TC_5MIN, False, False),
        # Moves played -> never reconfigure, regardless of the change (defer to
        # the next new game so a running clock is not reset mid-game).
        (_TC_5MIN, _TC_10MIN, True, False),
        (_TC_5MIN, _TC_5MIN_BRONSTEIN, True, False),
        (_TC_5MIN, _TC_5MIN, True, False),
    ],
)
def test_time_control_change_requires_reconfigure(current, desired, game_has_moves, expected):
    """The live-reapply predicate gates on both control-mismatch and no-moves.

    Why this exists: a time-control change from the web (notably the delay/timer
    mode) must reach the live e-paper clock, but only when it is safe to reseed
    the clock -- i.e. no moves have been played. It guards two regressions:
      1. Dropping the ``game_has_moves`` guard would reset a running clock
         mid-game (a moves-played row below would flip to True and fail).
      2. Comparing anything but full value-equality (e.g. only minutes) would
         miss a delay-mode-only change, leaving the live clock stale -- the
         (_TC_5MIN, _TC_5MIN_BRONSTEIN, False, True) row fails in that case.
    """
    assert (
        time_control_change_requires_reconfigure(current, desired, game_has_moves)
        is expected
    )
