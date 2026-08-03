"""Tests for chess-clock countdown-thread lifecycle and resume ply baselining.

Why this module exists
----------------------
Two reported "the clock is wrong" defects, both rooted in the clock service
tracking things indirectly instead of owning them:

1. Countdown-thread ownership. ``ChessClockService`` decided whether a countdown
   thread existed by reading the shared observable flag
   ``ChessClockState._is_running``, which ``configure()`` clears directly without
   signalling ``_stop_event`` or joining the thread. The thread was orphaned but
   still alive (idling on its poll), ``stop()`` then early-returned on the same
   flag so it could never reap it, and ``start()`` -- guarded by that flag too --
   spawned an additional thread and cleared ``_stop_event``. The board's in-place
   new-game sequence (``set_time_control_spec`` then ``reset_clock`` then
   ``start_clock`` on the first turn) hits this every time, so each new game added
   one more thread and the clock burned one extra second per real second.
   Users saw both clocks snap back to the initial time and then count down in
   two-second steps.

2. Resume ply baselining. ``configure()`` baselines the applied-ply cursor to the
   position on the board so a resumed game does not retroactively earn
   increments. The resume flow defeats that: the DisplayManager (and therefore
   ``configure()``) is built while the board is still empty, and only afterwards
   are the stored moves replayed. The baseline is 0 while the real ply count is
   N, so the single post-replay turn event credited an increment for every
   historical ply -- a rapid climb of one increment per ply, alternating sides,
   leaving both clocks N * increment seconds too high.

The thread tests assert thread ownership directly (deterministic, no sleeping)
plus one real-time rate check for the user-visible symptom. The baselining tests
are pure state manipulation.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from universalchess.services.chess_clock import ChessClockService
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.time_control import Stage, TimeControl

# The control from the field report: 30 minutes plus a 2-second Fischer
# increment. The increment value is what made the resume defect visible (each
# replayed ply stepped a clock by exactly this much).
BASE_MINUTES = 30
INCREMENT_SECONDS = 2
BASE_SECONDS = BASE_MINUTES * 60

# Countdown-thread name set by ChessClockService.start().
COUNTDOWN_THREAD_NAME = "clock-service"

# Real-time window for the tick-rate check. One countdown thread yields 2-3
# decrements over this span; two threads yield 5-6, so MAX_EXPECTED_TICKS
# separates correct behaviour from the leak without being timing-fragile.
RATE_WINDOW_SECONDS = 2.5
MAX_EXPECTED_TICKS = 3


def _fake_game(turn="white", plies=0):
    """Stand-in for ChessGameState: exposes turn_name and move_stack length.

    The clock reads only ``turn_name`` (active side) and ``move_stack`` (ply
    count), so this covers the real coupling without a board.
    """
    return SimpleNamespace(turn_name=turn, move_stack=[None] * plies)


def _live_countdown_threads():
    """Every countdown thread currently alive, across all service instances."""
    return [t for t in threading.enumerate()
            if t.name == COUNTDOWN_THREAD_NAME and t.is_alive()]


@pytest.fixture()
def service_and_game():
    """A ChessClockService on a fresh singleton state, with guaranteed teardown.

    Teardown must not rely on ``service.stop()``: the defects under test leave
    orphaned threads that ``stop()`` cannot reap, and a leaked daemon thread
    would keep decrementing the singleton state and corrupt later tests.
    """
    state = reset_chess_clock()
    game = _fake_game()
    state.set_game_state(game)
    service = ChessClockService()
    yield service, state, game

    service._stop_event.set()
    state.set_running(False)
    for thread in _live_countdown_threads():
        thread.join(timeout=2.0)
    assert not _live_countdown_threads(), "test leaked a countdown thread"


def _time_control():
    return TimeControl.fischer_minutes(BASE_MINUTES, INCREMENT_SECONDS)


def _start_first_game(service, game):
    """A full game start: DisplayManager.__init__ (configure) then the first move."""
    game.move_stack = []
    game.turn_name = "white"
    service.configure(_time_control())
    service.start()


def _play_in_place_new_game(service, game):
    """The board's EVENT_NEW_GAME sequence, as main.py._on_game_event runs it.

    set_time_control_spec (configure) -> reset_clock (reset) -> start_clock on
    the first turn event of the new game. Assumes a game is already running,
    which is what makes the previous countdown thread available to orphan.
    """
    game.move_stack = []
    game.turn_name = "white"
    service.configure(_time_control())
    service.reset()
    service.start()


# ---------------------------------------------------------------------------
# Countdown-thread ownership
# ---------------------------------------------------------------------------

def test_configure_stops_the_running_countdown_thread(service_and_game):
    """configure() must not leave an orphaned countdown thread behind.

    Why: configure() clears the observable running flag directly. stop() and
    start() both branch on that same flag, so a thread orphaned here can never
    be reaped and start() will add another alongside it.

    How the regression manifests: the thread count stays at 1 instead of
    dropping to 0, because the thread is still looping on its idle poll with
    _stop_event clear.
    """
    service, _state, _game = service_and_game
    service.configure(_time_control())
    service.start()
    assert len(_live_countdown_threads()) == 1

    service.configure(_time_control())

    assert _live_countdown_threads() == []


def test_in_place_new_game_does_not_accumulate_countdown_threads(service_and_game):
    """Repeated in-place new games must never leave more than one live thread.

    Why: this is the exact board sequence behind the report. configure() orphans
    the previous thread, reset()'s stop() early-returns on the already-cleared
    running flag, and start() both spawns a new thread and clears _stop_event
    (reviving anything a prior stop() failed to join).

    How the regression manifests: the thread count climbs by one per new game
    (1, then 2, then 3), which is the clock's speed multiplier.
    """
    service, _state, game = service_and_game
    _start_first_game(service, game)
    assert len(_live_countdown_threads()) == 1

    _play_in_place_new_game(service, game)
    assert len(_live_countdown_threads()) == 1

    _play_in_place_new_game(service, game)
    assert len(_live_countdown_threads()) == 1


def test_clock_decrements_one_second_per_second_after_new_game(service_and_game):
    """The user-visible symptom: time must burn at real-time rate, not faster.

    Why: duplicate countdown threads each decrement the active side once per
    second, and both re-phase their tick anchor to the move instant, so their
    decrements land together and the display steps down two seconds at a time.

    How the regression manifests: with two threads the active side loses 5-6
    seconds over the measurement window instead of 2-3.
    """
    service, state, game = service_and_game
    _start_first_game(service, game)
    _play_in_place_new_game(service, game)

    before = state.white_time
    time.sleep(RATE_WINDOW_SECONDS)
    elapsed_charged = before - state.white_time

    assert 0 < elapsed_charged <= MAX_EXPECTED_TICKS, (
        f"charged {elapsed_charged}s over {RATE_WINDOW_SECONDS}s of real time"
    )
    assert state.black_time == BASE_SECONDS, "the idle side must not be charged"


def test_stop_reaps_a_thread_whose_running_flag_was_already_cleared(service_and_game):
    """stop() must key off the thread it owns, not the shared running flag.

    Why: ChessClockState._is_running is mutated by configure() (and is an
    observable other code writes), so it is not a reliable record of whether the
    service has a thread to shut down. Conflating the two is what makes the leak
    unreachable.

    How the regression manifests: stop() returns immediately without setting
    _stop_event or joining, so the thread stays alive.
    """
    service, state, _game = service_and_game
    service.configure(_time_control())
    service.start()
    assert len(_live_countdown_threads()) == 1

    state.set_running(False)  # what configure() does to the shared flag
    service.stop()

    assert _live_countdown_threads() == []


# ---------------------------------------------------------------------------
# Resume: applied-ply baselining
# ---------------------------------------------------------------------------

def test_resume_does_not_credit_increments_for_replayed_history(service_and_game):
    """Resuming an advanced game must not grant an increment per historical ply.

    Why: the resume flow configures the clock against an empty board, replays
    the stored moves, restores the persisted times, and then fires one turn
    event. Without re-baselining after the replay, that single event walks every
    ply and credits each one.

    How the regression manifests: each side gains (plies / 2) * increment
    seconds -- 80 seconds each for a 40-move game on a 2-second increment -- and
    the walk emits one observer notification (one e-paper repaint) per ply.
    """
    service, state, game = service_and_game
    plies = 80
    persisted_white = 1334
    persisted_black = 1443

    # Game mode starts: the clock is configured while the board is still empty.
    service.configure(_time_control())
    assert state.white_time == BASE_SECONDS

    # The stored moves are replayed straight onto the game state.
    game.move_stack = [None] * plies
    game.turn_name = "white"

    # The resume restores the persisted times and re-baselines the clock against
    # the position now on the board.
    service.set_times(persisted_white, persisted_black)
    service.sync_move_counters_to_position()

    repaints = []
    state.on_state_change(lambda: repaints.append((state.white_time,
                                                   state.black_time)))
    service.notify_move_completed()  # the single post-resume turn event

    assert state.white_time == persisted_white
    assert state.black_time == persisted_black
    assert repaints == []


def test_first_move_after_resume_earns_exactly_one_increment(service_and_game):
    """Re-baselining must not swallow the increment for genuinely new moves.

    Why: an over-broad baseline (e.g. suppressing increments for the rest of the
    game) would be as wrong as the original defect, just in the other direction.

    How the regression manifests: white's clock stays flat after their real
    move, or is credited more than once.
    """
    service, state, game = service_and_game
    plies = 80
    persisted_white = 1334

    service.configure(_time_control())
    game.move_stack = [None] * plies
    game.turn_name = "white"
    service.set_times(persisted_white, 1443)
    service.sync_move_counters_to_position()
    service.notify_move_completed()

    # White plays the first move after the resume; black is now to move.
    game.move_stack = [None] * (plies + 1)
    game.turn_name = "black"
    service.notify_move_completed()

    assert state.white_time == persisted_white + INCREMENT_SECONDS
    assert state.black_time == 1443


def test_resume_baseline_preserves_stage_progress(service_and_game):
    """Re-baselining must restore per-side move counts, not just the ply cursor.

    Why: staged (tournament) controls grant the next stage's base time when a
    side reaches the stage move requirement, looked up from the per-side
    completed-move count. Baselining only the ply cursor leaves those counts at
    zero, so a resumed game would award the 40-move bonus 40 moves late.

    How the regression manifests: white's move 40 is treated as move 1, so no
    stage base time is added and white silently plays the rest of the game on
    stage-one time.
    """
    service, state, game = service_and_game
    stage_moves = 40
    stage_two_base = 1800
    tc = TimeControl.symmetric((
        Stage(moves=stage_moves, base_seconds=5400, increment_seconds=30),
        Stage(moves=0, base_seconds=stage_two_base, increment_seconds=30),
    ))

    service.configure(tc)
    # Resumed at 39 completed moves each (78 plies), white to move.
    game.move_stack = [None] * 78
    game.turn_name = "white"
    service.set_times(1000, 2000)
    service.sync_move_counters_to_position()

    # White completes move 40 -- the stage boundary.
    game.move_stack = [None] * 79
    game.turn_name = "black"
    service.notify_move_completed()

    assert state.white_time == 1000 + 30 + stage_two_base
    assert state.black_time == 2000
