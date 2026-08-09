"""Tests for the engine-move clock hand-off (transcription window) behavior.

Why these tests exist
---------------------
In an engine game the human physically transcribes the engine's move. Before
this feature the engine's clock kept counting for the whole transcription
window because the clock's active side was derived purely from the board turn
(still the engine's until the move is physically placed). The user wants the
engine to "press its clock" when it shows its move: the engine's clock stops
immediately, neither side counts for a short configurable grace delay, then the
human's clock starts -- all while the board turn is still the engine's.

The mechanism is a *forced active color* override on the clock:

- ``ChessClockState.begin_forced_active_color(color, delay_seconds)`` makes the
  clock report ``None`` (nobody counts) until ``delay_seconds`` have elapsed,
  then ``color`` (the human), regardless of the board turn.
- The override is cleared automatically once the board turn actually reaches the
  forced color (the engine's move was transcribed), so it never leaks into the
  following engine turn.
- ``ChessClockService`` stores the configured delay and exposes
  ``begin_opponent_turn`` / ``clear_forced_active_color``.
- ``GameSettings`` persists ``engine_move_clock_delay_seconds`` (default 1).

Time is injected (``_now_fn``) so the grace window is exercised deterministically
without real sleeping.
"""

from types import SimpleNamespace

import pytest

from universalchess.state.chess_clock import ChessClockState, reset_chess_clock
from universalchess.state.time_control import TimeControl
from universalchess.players.settings import GameSettings
from universalchess.tests.fake_clock import FakeMonotonic


def _fake_game(turn="white", plies=0):
    """Stand-in for ChessGameState exposing turn_name and move_stack length."""
    return SimpleNamespace(turn_name=turn, move_stack=[None] * plies)


def _state_with_clock(turn="black"):
    """ChessClockState wired to a fake game and a controllable time source.

    Default turn is 'black' to model an engine (black) whose move is pending
    while the human (white) is about to receive the clock.
    """
    state = ChessClockState()
    state.set_game_state(_fake_game(turn))
    state.set_timed_mode(True)
    state.set_times(300, 300)
    clock = FakeMonotonic()
    state._now_fn = clock
    return state, clock


# ---------------------------------------------------------------------------
# State: forced active color override (grace window + hand-off)
# ---------------------------------------------------------------------------

def test_grace_period_reports_no_active_side():
    """During the grace delay neither side is the active clock.

    Why: the engine "pressed its clock" but the human has not started theirs yet.
    How a regression manifests: if active_color still returned the board turn
    (black/engine) the engine's clock would keep counting during the delay.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    # Board turn is still black (engine's move not yet transcribed).
    assert state.active_color is None


def test_forced_side_active_after_delay():
    """After the grace delay the forced (human) side is active, not the board turn.

    Why: the human's clock must run during transcription once the delay elapses.
    How a regression manifests: returning the board turn (black) would count the
    engine's time; returning None forever would freeze both clocks.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    clock.now += 1  # grace elapsed
    assert state.active_color == "white"


def test_tick_during_grace_decrements_neither_side():
    """A tick inside the grace window leaves both clocks untouched.

    Why: the grace delay is a true pause for both players. How a regression
    manifests: the tick would decrement the board-turn side (engine) or the
    forced side prematurely.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    state.tick()
    assert state.white_time == 300
    assert state.black_time == 300


def test_tick_after_delay_decrements_forced_human_side_only():
    """After the delay a tick decrements the human (forced) side, not the engine.

    Why: this is the whole point -- transcription runs on the human's clock. How
    a regression manifests: black_time dropping would mean the engine is still
    being charged during transcription.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    clock.now += 1
    state.tick()
    assert state.white_time == 299
    assert state.black_time == 300


def test_clear_forced_active_color_restores_board_turn():
    """Clearing the override returns active_color to the natural board turn.

    Why: once the override is cleared normal turn-derived counting must resume.
    How a regression manifests: a lingering override would keep forcing the human
    side during the engine's next turn.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    clock.now += 1
    assert state.active_color == "white"
    state.clear_forced_active_color()
    assert state.active_color == "black"


def test_reset_clears_forced_active_color():
    """reset() drops any active override.

    Why: a new game must not inherit the previous game's transcription override.
    How a regression manifests: the first tick of the new game would count the
    wrong (previously forced) side.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    state.reset()
    # After reset the game state is untouched (turn black), so active follows it.
    assert state.active_color == "black"


def test_forced_and_board_turn_agree_after_transcription():
    """Once the board turn reaches the forced color the override is redundant.

    Why: after the human transcribes the engine's move the board turn flips to
    the human (the forced color); active_color must be that color either way.
    How a regression manifests: returning None here (mistaking agreement for a
    grace window) would stall the human's clock after transcription completes.
    """
    state, clock = _state_with_clock(turn="black")
    state.begin_forced_active_color("white", delay_seconds=1)
    clock.now += 1
    # Human transcribes: board turn flips to white (the forced color).
    state.set_game_state(_fake_game(turn="white", plies=1))
    assert state.active_color == "white"


# ---------------------------------------------------------------------------
# Service: configured delay + opponent-turn hand-off + auto-clear
# ---------------------------------------------------------------------------

@pytest.fixture()
def clock_service():
    """A ChessClockService bound to a fresh singleton state and fake game."""
    state = reset_chess_clock()
    game = _fake_game(turn="black", plies=1)
    state.set_game_state(game)
    from universalchess.services.chess_clock import ChessClockService
    service = ChessClockService()
    return service, state, game


def test_service_default_engine_move_delay_is_one_second(clock_service):
    """The service defaults the engine-move clock delay to 1 second.

    Why: the agreed default grace is 1s. How a regression manifests: a 0 default
    would remove the pause; a large default would stall the human's clock.
    """
    service, _state, _game = clock_service
    assert service.engine_move_delay_seconds == 1


def test_service_begin_opponent_turn_uses_configured_delay(clock_service):
    """begin_opponent_turn applies the configured delay to the forced hand-off.

    Why: the delay is configurable in timer settings. How a regression manifests:
    ignoring the configured value would use a hardcoded delay -- the human's
    clock would start too early or too late relative to the setting.
    """
    service, state, _game = clock_service
    service.configure(TimeControl.fischer_minutes(5, 0))
    service.set_engine_move_delay_seconds(2)
    clock = FakeMonotonic()
    state._now_fn = clock
    # Engine (black) to move; human is white.
    service.begin_opponent_turn("white")
    assert state.active_color is None            # grace
    clock.now += 1
    assert state.active_color is None            # still inside 2s grace
    clock.now += 1
    assert state.active_color == "white"         # human now counts


def test_service_notify_move_completed_clears_override_at_handoff(clock_service):
    """When the board turn reaches the forced color, the override auto-clears.

    Why: after the human transcribes the engine's move the override has served
    its purpose; it must not persist into the human's own move (which would keep
    the human's clock running during the next engine turn). How a regression
    manifests: without the clear, after the human moves (turn back to black) the
    override would still force white, charging the human during the engine turn.
    """
    service, state, game = clock_service
    service.configure(TimeControl.fischer_minutes(5, 0))
    clock = FakeMonotonic()
    state._now_fn = clock
    service.begin_opponent_turn("white")

    # Human transcribes the engine's move: board turn flips to white, ply +1.
    game.turn_name = "white"
    game.move_stack = [None, None]
    service.notify_move_completed()

    # Override cleared: the human then makes their own move (turn back to black).
    game.turn_name = "black"
    game.move_stack = [None, None, None]
    assert state.active_color == "black"   # engine's clock, not a leaked "white"


# ---------------------------------------------------------------------------
# Settings: engine_move_clock_delay_seconds persistence
# ---------------------------------------------------------------------------

def test_game_settings_default_engine_move_clock_delay_is_one():
    """GameSettings defaults engine_move_clock_delay_seconds to 1.

    Why: matches the agreed default grace. How a regression manifests: a missing
    field or wrong default would silently disable/alter the hand-off delay.
    """
    settings = GameSettings(section="test")
    assert settings.engine_move_clock_delay_seconds == 1


def test_game_settings_to_dict_exposes_engine_move_clock_delay():
    """to_dict surfaces engine_move_clock_delay_seconds for the menu/web binding.

    Why: the board menu and web settings read to_dict; an omitted key means the
    control cannot show or edit the value. How a regression manifests: KeyError
    or a stale default in the UI.
    """
    settings = GameSettings(section="test", engine_move_clock_delay_seconds=3)
    assert settings.to_dict()["engine_move_clock_delay_seconds"] == 3
