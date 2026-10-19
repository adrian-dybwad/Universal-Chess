"""Remote-app session: shadow the game, do not referee.

Why these tests exist
---------------------
While a Pegasus/Millennium/Chessnut app is connected, GameManager still
records legal physical moves so play can continue when the app leaves.
It must not run a second clock, enter correction mode, or drive LEDs --
those belong to the app. These tests pin that split at GameManager.

How a regression manifests
--------------------------
Post-move validation re-enters correction after every accepted move;
_enter_correction_mode lights guidance squares; LedCallbacks blank the
app's lights; a legal shadow move is skipped so disconnect resumes from
the wrong position.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("chess")

import chess

from universalchess.managers.game.game_manager import GameManager
from universalchess.services.chess_clock import ChessClockService
from universalchess.state.chess_clock import reset_chess_clock
from universalchess.state.chess_game import ChessGameState, get_chess_game, reset_chess_game
from universalchess.state.time_control import TimeControl
from universalchess.utils.led import LedCallbacks

START_OCCUPANCY = bytearray([1] * 16 + [0] * 32 + [1] * 16)

BASE_MINUTES = 5
INCREMENT_SECONDS = 2
BASE_SECONDS = BASE_MINUTES * 60
TIMED_CONTROL = TimeControl.fischer_minutes(BASE_MINUTES, INCREMENT_SECONDS)
UNTIMED_CONTROL = TimeControl.sudden_death_minutes(0)
SESSION_MOVES = ("e2e4", "e7e5")


def _recording_led(calls):
    return LedCallbacks(
        from_to=lambda *a, **k: calls.append("from_to"),
        array=lambda *a, **k: calls.append("array"),
        single=lambda *a, **k: calls.append("single"),
        off=lambda *a, **k: calls.append("off"),
        from_to_hint=lambda *a, **k: calls.append("from_to_hint"),
        array_hint=lambda *a, **k: calls.append("array_hint"),
        array_fast=lambda *a, **k: calls.append("array_fast"),
        from_to_fast=lambda *a, **k: calls.append("from_to_fast"),
        single_fast=lambda *a, **k: calls.append("single_fast"),
    )


@pytest.fixture
def gm(monkeypatch):
    """GameManager on a fresh standard game, board hardware stubbed.

    reset_chess_game() clears the shared singleton. ledsOff / beep / occupancy
    are stubbed because set_remote_app_session talks to the board module, and
    the task worker is stopped on teardown so it does not leak across tests.
    """
    reset_chess_game()
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.ledsOff", lambda: None
    )
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.beep", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: bytearray(START_OCCUPANCY),
    )
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_recording_led([]))
    yield manager
    manager._stop_event.set()


@pytest.fixture
def clock(gm):
    """A real clock service on the game ``gm`` plays, owned by ``gm``.

    reset_chess_clock() links the fresh clock state to the current chess
    game, so the service counts the plies ``gm`` pushes. Stopped on teardown
    so a countdown thread started by a resume does not outlive the test.
    """
    reset_chess_clock()
    service = ChessClockService()
    gm._clock_service = service
    yield service
    service.stop()


class _RecordingPanel:
    """The e-paper Manager's refresh-mode switch, recorded."""

    def __init__(self):
        self.defer_to_clock = []

    def set_defer_to_clock(self, enabled):
        self.defer_to_clock.append(enabled)


@pytest.fixture
def board_app_display(clock, monkeypatch):
    """board_app with a real DisplayManager on ``clock``, wired to ``gm``.

    The DisplayManager shares the GameManager's clock service, as on the
    board, so a test observes what the two layers do to one clock together.
    Widgets and the analysis engine are stubbed; the panel is a recorder.
    Yields ``(board_app, display, panel)``; the caller configures the clock
    through ``display.set_time_control_spec``.
    """
    import universalchess.managers.display as display_module
    from universalchess.app import board_app
    from universalchess.managers.display import DisplayManager

    fake_game = SimpleNamespace(
        on_alert_clear=lambda *_a, **_k: None,
        set_position=lambda *_a, **_k: None,
    )
    with patch.object(DisplayManager, "_init_widgets"), \
         patch.object(display_module, "_load_widgets"), \
         patch.object(display_module, "get_chess_game", return_value=fake_game), \
         patch.object(display_module, "get_chess_clock_service", return_value=clock):
        display = DisplayManager(time_control_spec=TIMED_CONTROL)
    panel = _RecordingPanel()
    monkeypatch.setattr(display_module.board, "display_manager", panel)
    monkeypatch.setattr(board_app._game, "display", display)
    yield board_app, display, panel


def _play(gm, ucis):
    for uci in ucis:
        assert gm._on_player_move(chess.Move.from_uci(uci)) is True


def test_post_move_validation_skipped_while_remote_session_on(gm, monkeypatch):
    """Physical-board validation must not run while a chess app owns the board.

    A low-priority scan that lags the place just accepted is what made every
    Pegasus move enter correction then immediately exit. The task is run
    inline so the branch is deterministic; the recorder proves validation is
    suppressed while the session is on and restored once it is off.

    Regression: validations == ["e2e4"] while the session is on.
    """
    validations = []
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.validate_physical_board_after_move",
        lambda **kwargs: validations.append(kwargs["move_uci"]),
    )
    monkeypatch.setattr(gm._task_worker, "submit", lambda fn: fn())

    task_kwargs = dict(
        target_square=chess.E4,
        move_uci="e2e4",
        fen_before_move=chess.STARTING_FEN,
        fen_after_move=chess.STARTING_FEN,
        is_first_move=True,
        game_ended=False,
        result_string=None,
        termination=None,
    )

    gm.set_remote_app_session(True)
    gm._enqueue_post_move_tasks(**task_kwargs)
    assert validations == []

    gm.set_remote_app_session(False)
    gm._enqueue_post_move_tasks(**task_kwargs)
    assert validations == ["e2e4"]


def test_legal_move_still_updates_board_while_remote_session_on(gm):
    """A legal move must still be recorded so disconnect can continue.

    If the session flag also suppressed move application, the in-memory
    chess.Board would lag the physical pieces and take-over on disconnect
    would resume from the wrong position.

    Regression: chess_board still at the start FEN after e2e4.
    """
    gm.set_remote_app_session(True)

    accepted = gm._on_player_move(chess.Move.from_uci("e2e4"))

    assert accepted is True
    assert gm.chess_board.peek().uci() == "e2e4"
    assert gm.correction_mode.is_active is False


def test_enter_correction_mode_is_noop_while_remote_session_on(gm):
    """Correction mode must not activate while the app is the referee.

    _enter_correction_mode is the backstop every error path funnels through.
    If it still entered, the board would light "put it back" squares against
    the app's LEDs after every rejected or lagged scan.

    Regression: correction_mode.is_active is True after the call.
    """
    gm.set_remote_app_session(True)
    gm._enter_correction_mode()

    assert gm.correction_mode.is_active is False

    gm.set_remote_app_session(False)
    gm._enter_correction_mode()
    assert gm.correction_mode.is_active is True


def test_led_callbacks_are_noop_while_remote_session_on(gm):
    """GameManager must not write LEDs while the app owns the lights.

    LedCallbacks.off would blank the squares the DGT/Millennium/Chessnut app
    just lit. The session flag swaps in a no-op set; turning the session off
    restores the real callbacks.

    Regression: calls is non-empty while the session is on.
    """
    calls = []
    gm.set_led_callbacks(_recording_led(calls))

    gm.set_remote_app_session(True)
    gm.led.off()
    gm.led.single_fast(chess.E4, repeat=1)
    gm.led.from_to(chess.E2, chess.E4, repeat=0)
    assert calls == []

    gm.set_remote_app_session(False)
    gm.led.off()
    assert calls == ["off"]


def test_remote_session_pauses_clock_on_enter_and_resumes_on_exit(gm):
    """The UC clock must freeze for the app session and tick again after it.

    There is no clock in the Pegasus protocol, so remaining time on
    disconnect is whatever was frozen at connect -- not the app's unknown
    remainder. Pause on enter; resume a paused timed clock on exit.

    The move counters are re-baselined before the resume, so the clock never
    runs with a cursor that still points at the connect position.

    Regression: calls is not ["pause"] after enter, or not
    ["pause", "baseline", "resume"] after exit.
    """
    calls = []

    class _FakeClock:
        timed_mode = True

        def pause(self):
            calls.append("pause")

        def resume(self):
            calls.append("resume")

        def sync_move_counters_to_position(self):
            calls.append("baseline")

    gm._clock_service = _FakeClock()

    gm.set_remote_app_session(True)
    assert calls == ["pause"]

    gm.set_remote_app_session(False)
    assert calls == ["pause", "baseline", "resume"]


def test_session_exit_enters_correction_when_physical_board_mismatches(gm, monkeypatch):
    """On disconnect, UC is referee again: mismatch must enter correction.

    The physical pieces are whatever the app left. If they do not match the
    shadow chess.Board, the player has to be guided back before local play
    continues. Matching occupancy must not enter correction.

    Regression: empty occupancy leaves correction_mode inactive, so local
    play starts on a board that does not match the pieces.
    """
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: bytearray(64),
    )
    gm.set_remote_app_session(True)
    gm.set_remote_app_session(False)

    assert gm.correction_mode.is_active is True


def test_session_exit_skips_correction_when_physical_board_matches(gm):
    """Matching occupancy on disconnect must not enter correction.

    The shadow game already agrees with the pieces, so take-over should
    play on immediately. Entering correction here would flash guidance
    for a board that is already right.

    Regression: correction_mode.is_active is True at start occupancy.
    """
    gm.set_remote_app_session(True)
    gm.set_remote_app_session(False)

    assert gm.correction_mode.is_active is False
    assert ChessGameState.states_match(
        START_OCCUPANCY, gm._chess_board_to_state(gm.chess_board)
    )


def test_moves_made_during_the_session_earn_no_increment_after_disconnect(gm, clock):
    """Session moves were the app's: the board must not credit their increment.

    While the app is connected the turn handler skips apply_clock_move, so the
    clock's applied-ply cursor stops at the connect position. The first local
    turn event after disconnect runs notify_move_completed, which credits an
    increment for every ply past that cursor -- one per session move.

    Regression: white or black reads BASE_SECONDS + INCREMENT_SECONDS after
    disconnect (one session move each), and the first local move then still
    earns exactly one increment, proving crediting resumed rather than stopped.
    """
    clock.configure(TIMED_CONTROL)
    clock.start()

    gm.set_remote_app_session(True)
    _play(gm, SESSION_MOVES)
    gm.set_remote_app_session(False)
    assert clock.is_running and not clock.is_paused
    # Freeze the countdown so the arithmetic below is exact.
    clock.pause()

    # What DisplayManager.apply_clock_move runs on the first local turn event.
    clock.notify_move_completed()
    assert clock.get_times() == (BASE_SECONDS, BASE_SECONDS)

    _play(gm, ("g1f3",))
    clock.notify_move_completed()
    assert clock.get_times() == (BASE_SECONDS + INCREMENT_SECONDS, BASE_SECONDS)


def _end_game(_gm):
    get_chess_game().set_result("1-0", "resignation")


def _keep_playing(_gm):
    return None


@pytest.mark.parametrize(
    ("control", "during_session"),
    [
        pytest.param(TIMED_CONTROL, _end_game, id="timed-game-ended-during-session"),
        pytest.param(UNTIMED_CONTROL, _keep_playing, id="untimed-game"),
    ],
)
def test_disconnect_does_not_start_a_clock_that_must_stay_stopped(
    gm, clock, board_app_display, control, during_session
):
    """Only GameManager decides whether the clock runs again after a session.

    GameManager resumes only a timed clock in an unfinished game. The board-app
    session handler used to call DisplayManager.resume_clock as well, with no
    such check, and resume() on a stopped clock calls start(). A game that
    ended while the app was connected, or an untimed game, then had a running
    countdown after disconnect.

    The clock is stopped before the session in both cases (never started, as
    when the app connects before the first turn event), which is the state
    where resume() turns into start().

    Regression: clock.is_running is True after disconnect, and the panel is
    switched to clock-driven refresh (defer_to_clock ends True) for a clock
    that has no reason to tick.
    """
    board_app, display, panel = board_app_display
    display.set_time_control_spec(control)
    gm.on_remote_app_session = board_app._on_remote_app_session

    gm.set_remote_app_session(True)
    during_session(gm)
    gm.set_remote_app_session(False)

    assert clock.is_running is False
    assert panel.defer_to_clock[-1] is False


def test_disconnect_returns_the_panel_to_clock_driven_refresh_for_a_resumed_clock(
    gm, clock, board_app_display
):
    """A timed clock resumed by GameManager must drive the panel refresh again.

    The panel defers routine redraws to the clock tick only while the clock is
    counting. GameManager pauses and resumes the clock; the board-app handler
    is what brings the panel's refresh mode in line afterwards. If it stopped
    syncing, the panel would stay in immediate refresh during a running clock,
    or (on connect) keep deferring to a clock that no longer ticks.

    Regression: defer_to_clock is not [False, True] -- the handler did not run
    after each clock transition, or ran before the clock changed.
    """
    board_app, _display, panel = board_app_display
    gm.on_remote_app_session = board_app._on_remote_app_session
    clock.start()

    gm.set_remote_app_session(True)
    gm.set_remote_app_session(False)

    assert clock.is_running and not clock.is_paused
    assert panel.defer_to_clock == [False, True]
