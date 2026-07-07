"""Tests for time-control behavior in the chess clock state and service.

Why these tests exist
---------------------
The clock gained Fischer increment, simple/US delay, Bronstein delay, tournament
stages, and asymmetric per-side times. This module verifies the runtime effects
on the actual remaining time:

- ``ChessClockState.tick`` freezes the main clock during a simple delay and
  otherwise decrements the mover while tracking time used (for Bronstein).
- ``ChessClockState.apply_move_completed`` grants increment, Bronstein giveback,
  and stage base time to the correct side and resets per-turn tracking.
- ``ChessClockService.configure`` seeds per-side initial times (including
  asymmetric) and ``notify_move_completed`` applies effects once per completed
  ply, idempotently, driven by the game's move stack.

The logic is exercised deterministically (no real sleeping); ``tick`` is called
directly to simulate whole seconds elapsing.
"""

from types import SimpleNamespace

import pytest

from universalchess.state.chess_clock import ChessClockState, reset_chess_clock
from universalchess.state.time_control import DelayMode, Stage, TimeControl


def _fake_game(turn="white", plies=0):
    """Stand-in for ChessGameState: exposes turn_name and move_stack length.

    The clock reads only ``turn_name`` (active side) and ``move_stack`` (ply
    count) from the game state, so this covers the real coupling without a full
    board.
    """
    return SimpleNamespace(turn_name=turn, move_stack=[None] * plies)


def _state(tc, turn="white"):
    """A ChessClockState wired to a fake game and configured with ``tc``."""
    state = ChessClockState()
    state.set_game_state(_fake_game(turn))
    state.set_time_control(tc)
    state.set_timed_mode(tc.is_timed)
    state.set_times(tc.initial_seconds("white"), tc.initial_seconds("black"))
    return state


# ---------------------------------------------------------------------------
# Fischer increment
# ---------------------------------------------------------------------------

def test_fischer_increment_added_to_mover_only():
    """A completed move grants the mover their increment; the opponent unchanged.

    Why: Fischer increment is added to whoever moved. How a regression manifests:
    adding to the wrong side (or both) would let a player's clock grow on the
    opponent's move.
    """
    state = _state(TimeControl.fischer_minutes(5, 3))
    for _ in range(5):
        state.tick()  # white spends 5 seconds
    assert state.white_time == 295
    state.apply_move_completed("white", 1)
    assert state.white_time == 298  # 295 + 3 increment
    assert state.black_time == 300  # opponent untouched


# ---------------------------------------------------------------------------
# Simple / US delay
# ---------------------------------------------------------------------------

def test_simple_delay_freezes_main_clock_then_counts():
    """Simple delay must freeze the main clock for delay_seconds each move.

    Why: US delay does not deduct main time until the delay is used. How a
    regression manifests: if tick decremented main time during the delay, a
    player would lose their delay seconds every move.
    """
    state = _state(TimeControl.symmetric(
        (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.SIMPLE))
    for _ in range(3):
        state.tick()  # consumes the 3-second delay, not main time
    assert state.white_time == 300
    state.tick()  # delay exhausted -> main time counts
    assert state.white_time == 299


def test_simple_delay_resets_each_move():
    """Delay is refreshed for the side to move after every completed move.

    Why: the delay is per-move, not per-game. How a regression manifests: if the
    delay were not reset, only the first move would benefit from it.
    """
    state = _state(TimeControl.symmetric(
        (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.SIMPLE))
    for _ in range(4):
        state.tick()  # 3 delay + 1 main -> white 299
    assert state.white_time == 299
    # White completes the move; play switches to black then back would reset,
    # but the per-turn tracking is reset for the next side to move here.
    state.apply_move_completed("white", 1)
    state.set_game_state(_fake_game(turn="white"))  # simulate white to move again
    for _ in range(3):
        state.tick()
    assert state.white_time == 299  # fresh 3-second delay absorbed the ticks


# ---------------------------------------------------------------------------
# Bronstein delay
# ---------------------------------------------------------------------------

def test_bronstein_gives_back_time_used_capped_at_delay():
    """Bronstein returns min(delay, time used) after the move.

    Why: Bronstein runs the main clock but refunds the used time up to the delay.
    How a regression manifests: refunding the full delay regardless of use would
    let a fast mover gain time; refunding nothing would make it sudden death.
    """
    state = _state(TimeControl.symmetric(
        (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.BRONSTEIN))
    for _ in range(2):
        state.tick()  # used 2 seconds (< delay) -> white 298
    assert state.white_time == 298
    state.apply_move_completed("white", 1)
    assert state.white_time == 300  # min(3, 2) = 2 returned


def test_bronstein_giveback_capped():
    """Using more than the delay only refunds up to the delay.

    Why: the refund is capped at delay_seconds. How a regression manifests: an
    uncapped refund would return all used time, making the clock never decrease.
    """
    state = _state(TimeControl.symmetric(
        (Stage(0, 300, 0),), delay_seconds=3, delay_mode=DelayMode.BRONSTEIN))
    for _ in range(5):
        state.tick()  # used 5 seconds -> white 295
    assert state.white_time == 295
    state.apply_move_completed("white", 1)
    assert state.white_time == 298  # min(3, 5) = 3 returned


# ---------------------------------------------------------------------------
# Tournament stages
# ---------------------------------------------------------------------------

def test_stage_base_time_added_at_boundary_move():
    """Reaching the stage move requirement adds the next stage's base time.

    Why: staged controls grant extra time once the move count is met. How a
    regression manifests: no base addition would leave the player short after
    move 40; adding it every move would inflate the clock.
    """
    tc = TimeControl.symmetric((
        Stage(moves=40, base_seconds=5400, increment_seconds=30),
        Stage(moves=0, base_seconds=1800, increment_seconds=30),
    ))
    state = _state(tc)
    state.set_times(1000, 5400)  # white low on time at the boundary
    state.apply_move_completed("white", 40)
    # White gets 30s increment + 1800s stage base = 1830 added.
    assert state.white_time == 1000 + 30 + 1800
    assert state.black_time == 5400


# ---------------------------------------------------------------------------
# Untimed passthrough
# ---------------------------------------------------------------------------

def test_untimed_apply_move_is_noop():
    """In an untimed control, completing a move changes no clock value.

    Why: untimed mode shows only the turn indicator. How a regression manifests:
    a spurious increment/base would turn an untimed game into a timed one.
    """
    state = _state(TimeControl.sudden_death_minutes(0))
    assert state.timed_mode is False
    state.apply_move_completed("white", 1)
    assert state.white_time == 0
    assert state.black_time == 0


# ---------------------------------------------------------------------------
# Service: configure + notify_move_completed
# ---------------------------------------------------------------------------

@pytest.fixture()
def clock_service():
    """A ChessClockService bound to a fresh singleton state and fake game.

    reset_chess_clock() gives a clean singleton so move counters and times do
    not leak between tests; the service reads that same singleton.
    """
    state = reset_chess_clock()
    game = _fake_game()
    state.set_game_state(game)
    from universalchess.services.chess_clock import ChessClockService
    service = ChessClockService()
    return service, state, game


def test_service_configure_sets_asymmetric_initial_times(clock_service):
    """configure seeds different per-side times for an asymmetric control.

    Why: time-odds games must start with each side's own base. How a regression
    manifests: symmetric seeding would give both sides white's time.
    """
    service, state, _game = clock_service
    tc = TimeControl(
        white_stages=(Stage(0, 300, 0),),
        black_stages=(Stage(0, 60, 0),),
    )
    service.configure(tc)
    assert state.white_time == 300
    assert state.black_time == 60
    assert state.timed_mode is True


def test_service_notify_move_completed_applies_increment_once_per_ply(clock_service):
    """notify_move_completed applies increment exactly once per new ply.

    Why: the hook fires on turn events which can repeat (e.g. resume); driving
    from the move stack makes it idempotent and attributes each move to the
    correct side. How a regression manifests: double counting would inflate the
    clock; wrong side attribution would credit the opponent.
    """
    service, state, game = clock_service
    service.configure(TimeControl.fischer_minutes(5, 3))

    # White has completed move 1 (ply 1); black to move.
    game.move_stack = [None]
    game.turn_name = "black"
    service.notify_move_completed()
    assert state.white_time == 303  # +3 for white's move
    assert state.black_time == 300

    # Duplicate event with no new ply must be a no-op (idempotent).
    service.notify_move_completed()
    assert state.white_time == 303

    # Black completes move 1 (ply 2); white to move.
    game.move_stack = [None, None]
    game.turn_name = "white"
    service.notify_move_completed()
    assert state.black_time == 303
    assert state.white_time == 303


def test_service_reset_clears_move_counters(clock_service):
    """reset returns to initial times and clears per-side move counters.

    Why: a new game must not inherit the previous game's move count (which drives
    stage transitions) or elapsed time. How a regression manifests: stale
    counters would trigger a stage change early in the next game.
    """
    service, state, game = clock_service
    service.configure(TimeControl.fischer_minutes(5, 3))
    game.move_stack = [None]
    game.turn_name = "black"
    service.notify_move_completed()
    assert state.white_time == 303

    # A new game resets the board to the starting position (ply 0) before the
    # clock is reset; reflect that so the ply baseline is cleared correctly.
    game.move_stack = []
    game.turn_name = "white"
    service.reset()
    assert state.white_time == 300
    assert state.black_time == 300
    # After reset, the first observed ply is attributed to move 1 again.
    game.move_stack = [None]
    game.turn_name = "black"
    service.notify_move_completed()
    assert state.white_time == 303
