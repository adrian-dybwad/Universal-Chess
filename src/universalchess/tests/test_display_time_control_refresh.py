"""Tests for refreshing the live clock's time control on an in-place new game.

Background / why these tests exist
----------------------------------
The full time-control spec (minutes, increment, and delay *mode* -- the setting
users call "timer mode") is resolved once when the ``DisplayManager`` is built at
game start and cached in ``_time_control_spec``. A board-reset new game (pieces
returned to the start position) and the physical setup-mode adoption both restart
play *in place*, reusing that same ``DisplayManager`` and only calling
``reset_clock()`` -- which re-applies the cached spec.

The reported regression: after changing a time-control setting (e.g. the delay
mode) and then starting a game by setting up the start position, the change was
ignored because the cached spec was never re-resolved. ``set_time_control_spec``
lets the app layer push a freshly resolved spec into the running ``DisplayManager``
so the next ``reset_clock()`` seeds the clock from the new control.

The clock service and clock state are real here (not mocked) so the assertions
observe the actual seeded times and active delay mode; only the widget/engine
initialization is stubbed, since it needs a display panel this headless test has
no use for.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from universalchess.state.chess_clock import reset_chess_clock
from universalchess.services.chess_clock import ChessClockService
from universalchess.state.time_control import DelayMode, Stage, TimeControl


@pytest.fixture()
def display_manager_with_real_clock():
    """A DisplayManager wired to a real clock service, widgets stubbed out.

    Yields ``(dm, service)``. The clock service reads the freshly reset singleton
    clock state, so seeded times and the active time control are observable after
    ``reset_clock()``. ``get_chess_game`` is stubbed to a minimal game state (the
    manager only needs ``on_alert_clear`` and a position) and ``_init_widgets`` /
    ``_load_widgets`` are patched so no display panel is required.
    """
    reset_chess_clock()
    service = ChessClockService()

    fake_game = SimpleNamespace(
        on_alert_clear=lambda *_a, **_k: None,
        set_position=lambda *_a, **_k: None,
    )

    import universalchess.managers.display as display_module
    from universalchess.managers.display import DisplayManager

    with patch.object(DisplayManager, "_init_widgets"), \
         patch.object(display_module, "_load_widgets"), \
         patch.object(display_module, "get_chess_game", return_value=fake_game), \
         patch.object(display_module, "get_chess_clock_service", return_value=service):
        dm = DisplayManager(time_control_spec=TimeControl.sudden_death_minutes(5))
        yield dm, service


def test_reset_clock_uses_spec_captured_at_construction(display_manager_with_real_clock):
    """Baseline: without a refresh, reset_clock seeds from the start-of-game spec.

    Guards the efficient path -- a new game with no setting change must keep the
    control the game was built with. If reset_clock ignored the cached spec, the
    seeded time here would not be 300 seconds.
    """
    dm, service = display_manager_with_real_clock
    dm.reset_clock()
    assert service.white_time == 300
    assert service.black_time == 300


def test_set_time_control_spec_reseeds_clock_on_next_reset(display_manager_with_real_clock):
    """A refreshed spec must drive the clock on the next in-place new game.

    This is the reported regression: the game started at 5 minutes, the user then
    changed the control to 1 minute, and a board-reset new game must start at 60
    seconds -- not the stale 300. Before the fix there was no way to update the
    cached spec, so reset_clock re-applied 300 and this assertion failed.
    """
    dm, service = display_manager_with_real_clock

    dm.set_time_control_spec(TimeControl.sudden_death_minutes(1))
    dm.reset_clock()

    assert service.white_time == 60
    assert service.black_time == 60


def test_set_time_control_spec_applies_new_delay_mode(display_manager_with_real_clock):
    """Changing only the delay *mode* ("timer mode") must reach the live clock.

    The reported setting is delay mode. The game started as plain sudden death
    (DelayMode.NONE); switching to Bronstein must be reflected in the clock's
    active time control after the in-place new game. If the cached spec were
    reused, delay_mode would remain NONE and this assertion would fail.
    """
    dm, service = display_manager_with_real_clock

    bronstein = TimeControl.symmetric(
        (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.BRONSTEIN
    )
    dm.set_time_control_spec(bronstein)
    dm.reset_clock()

    active = service._state.time_control
    assert active.delay_mode is DelayMode.BRONSTEIN
    assert active.delay_seconds == 3


def test_set_time_control_spec_updates_cached_spec(display_manager_with_real_clock):
    """The manager's own cached spec must reflect the refresh.

    reset_clock and the resume/suspend paths read _time_control_spec (e.g. for the
    reset log line). If the setter reconfigured the clock but left _time_control_spec
    stale, those readers would report the wrong control; this pins them in sync.
    """
    dm, _service = display_manager_with_real_clock

    new_spec = TimeControl.fischer_minutes(3, 2)
    dm.set_time_control_spec(new_spec)

    assert dm._time_control_spec is new_spec


def test_time_control_spec_property_exposes_current_control(display_manager_with_real_clock):
    """The public getter must return the control the widgets are built from.

    The settings-apply path compares the live control against freshly resolved
    settings to decide whether to reconfigure the clock. It reads this property
    rather than the private field, so the getter must track set_time_control_spec.
    If it returned a stale value the change-detection would misfire (reconfigure
    when nothing changed, or skip a real change).
    """
    dm, _service = display_manager_with_real_clock

    assert dm.time_control_spec == TimeControl.sudden_death_minutes(5)

    new_spec = TimeControl.fischer_minutes(3, 2)
    dm.set_time_control_spec(new_spec)
    assert dm.time_control_spec is new_spec


def test_layout_needs_rebuild_false_when_settings_unchanged(display_manager_with_real_clock):
    """A new game with no layout-affecting change must not rebuild the layout.

    Guards the common path: an in-place new game (board reset) with the same
    settings must reuse the existing widgets, avoiding a needless full-screen
    e-paper refresh. If the signature compared unequal here, every new game would
    flash a rebuild.
    """
    dm, _service = display_manager_with_real_clock
    # Simulate a completed _init_widgets build (patched in the fixture) by
    # recording the signature the built widgets would carry.
    dm._layout_signature = dm._compute_layout_signature()

    assert dm.layout_needs_rebuild() is False


def test_layout_needs_rebuild_true_when_timed_mode_flips(display_manager_with_real_clock):
    """Flipping timed<->untimed via a deferred change must schedule a rebuild.

    This is the reported gap: a time-control change deferred while a game was in
    progress is applied at the next new game via set_time_control_spec, which can
    flip is_timed without recreating the widgets. The clock widget's timed/untimed
    layout and height are fixed at build time, so the built layout no longer
    matches. If is_timed were excluded from the signature this would return False
    and the untimed layout would never appear until a full game start.
    """
    dm, _service = display_manager_with_real_clock
    dm._layout_signature = dm._compute_layout_signature()  # built timed (5 min)

    dm.set_time_control_spec(TimeControl.sudden_death_minutes(0))  # untimed

    assert dm.layout_needs_rebuild() is True


def test_layout_needs_rebuild_false_for_timed_to_timed_change(display_manager_with_real_clock):
    """A timed->timed control change must NOT force a layout rebuild.

    Changing minutes/increment/delay mode keeps the clock timed, so the widget
    layout is unchanged -- times and increment/delay annotations update live from
    the reconfigured clock state. Rebuilding here would be a needless refresh; if
    the signature keyed on the full control (not just is_timed) this would return
    True and over-rebuild on every clock tweak.
    """
    dm, _service = display_manager_with_real_clock
    dm._layout_signature = dm._compute_layout_signature()  # built timed (5 min)

    dm.set_time_control_spec(TimeControl.fischer_minutes(10, 5))  # still timed

    assert dm.layout_needs_rebuild() is False
