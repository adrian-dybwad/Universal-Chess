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

    The manager gets its own clock service on a fresh clock state, stopped on
    teardown. With the process-wide service, a disconnect resumed whatever
    timed clock an earlier test had configured, and the countdown thread it
    started outlived the test.
    """
    reset_chess_game()
    reset_chess_clock()
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
    service = ChessClockService()
    manager._clock_service = service
    manager.set_led_callbacks(_recording_led([]))
    yield manager
    manager.unsubscribe_game()
    service.stop()


@pytest.fixture
def clock(gm):
    """The real clock service ``gm`` owns, counting the plies ``gm`` pushes."""
    return gm._clock_service


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

        def set_kept_by_app(self, kept):
            pass

    gm._clock_service = _FakeClock()

    gm.set_remote_app_session(True)
    assert calls == ["pause"]

    gm.set_remote_app_session(False)
    assert calls == ["pause", "baseline", "resume"]


def test_remote_session_marks_the_clock_as_kept_by_the_app(gm, clock):
    """The clock must say the app keeps it for exactly the session's duration.

    No supported protocol carries the app's clock, so the board cannot show the
    game's times while an app is connected. The flag is what makes the e-paper
    and the web show "clock on app" instead of the paused board clock, and it
    must clear on disconnect so local play shows times again.

    Regression: observed is not [False, True, False] -- the panel and the web
    keep showing frozen times during the session, or never show them again.
    """
    observed = [clock.kept_by_app]

    gm.set_remote_app_session(True)
    observed.append(clock.kept_by_app)
    gm.set_remote_app_session(False)
    observed.append(clock.kept_by_app)

    assert observed == [False, True, False]


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


def test_session_exit_resets_when_physical_board_is_already_at_start(gm, monkeypatch):
    """Disconnect with pieces at home ranks must open a local start, not midgame correction.

    Why: HIARCS (or another app) starts a new game and the pieces are set to
    start while the Centaur shadow is still midgame. Disconnect hand-back
    entered correction toward that midgame, so the board never started the
    opening the pieces already showed.

    How the regression manifests: correction_mode.is_active is True and fen is
    still the midgame position after disconnect.
    """
    gm.set_remote_app_session(True)
    assert gm._on_player_move(chess.Move.from_uci("e2e4")) is True
    assert gm.chess_board.peek().uci() == "e2e4"
    # Pieces already rebuilt for the app's new game (start), shadow still e2e4.
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: bytearray(START_OCCUPANCY),
    )

    gm.set_remote_app_session(False)

    assert gm.correction_mode.is_active is False
    assert gm.chess_board.fen() == chess.STARTING_FEN


def test_resync_after_web_play_resets_when_physical_board_is_at_start(gm, monkeypatch):
    """First physical touch after web play must follow an app new-game at start.

    Why: with the app connected, a web move arms resync. HIARCS then starts a
    new game and the pieces are at start while the shadow is still midgame.
    Resync entered correction toward that midgame, so the Centaur never
    aligned with the app's opening.

    How the regression manifests: correction_mode.is_active is True and
    ``_resyncing_physical_board`` stays True after ``_begin_physical_resync``.
    """
    gm.set_remote_app_session(True)
    assert gm._on_player_move(chess.Move.from_uci("e2e4")) is True
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: bytearray(START_OCCUPANCY),
    )

    gm._begin_physical_resync()

    assert gm.correction_mode.is_active is False
    assert gm._resyncing_physical_board is False
    assert gm.chess_board.fen() == chess.STARTING_FEN
    assert gm.is_remote_app_session is True


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


def test_moves_made_during_the_session_earn_no_increment_after_disconnect(gm, clock, monkeypatch):
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
    # Occupancy must match the shadow midgame: the fixture defaults to start,
    # which would reset on disconnect instead of continuing this game.
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: gm._chess_board_to_state(gm.chess_board),
    )
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


# Recorded on the board: Stockfish had computed d7d5 for Black when HIARCS
# connected over Millennium; the app then played g8f6 and, later, d7d5 itself.
MOVE_BEFORE_SESSION = "d2d4"
ENGINE_REPLY_BEFORE_SESSION = "d7d5"
SESSION_MOVES_AFTER_ENGINE_REPLY = ("g8f6", "c2c3", "d7d5", "a2a4")


def _occupancy(chess_board, lifted_square=None):
    """Piece presence the board reports for ``chess_board``, minus a lifted piece."""
    return bytearray(
        1 if chess_board.piece_at(square) and square != lifted_square else 0
        for square in chess.SQUARES
    )


class _PhysicalBoard:
    """Occupancy that follows the game, except for a piece the test holds up."""

    def __init__(self, gm):
        self._gm = gm
        self.lifted_square = None

    def get_chess_state(self):
        return _occupancy(self._gm.chess_board, self.lifted_square)


@pytest.fixture
def physical_board(gm, monkeypatch):
    """Board occupancy driven by ``_PhysicalBoard``; piece events are processed at once.

    ``_is_ready`` stands in for the running game thread, without which
    receive_field only queues the events.
    """
    gm._is_ready = True
    physical = _PhysicalBoard(gm)
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        physical.get_chess_state,
    )
    return physical


@pytest.fixture
def engine_with_reply_pending_at_connect(gm, physical_board):
    """Human White v. Stockfish Black, Stockfish's d7d5 pending when the app connects.

    Engine start is stubbed so no Stockfish process is launched; the computed
    reply is set directly, as the engine's think thread would. Yields
    ``(protocol, engine)`` after the app has connected, played the session
    moves, and disconnected.
    """
    from universalchess.managers.protocol import ProtocolManager
    from universalchess.players.engine import EnginePlayer
    from universalchess.players.human import HumanPlayer
    from universalchess.players.manager import PlayerManager

    engine = EnginePlayer()
    engine.start = lambda: True
    protocol = ProtocolManager(game_manager=gm)
    protocol.set_player_manager(PlayerManager(HumanPlayer(), engine))
    _play(gm, [MOVE_BEFORE_SESSION])
    engine._pending_move = chess.Move.from_uci(ENGINE_REPLY_BEFORE_SESSION)

    protocol.on_protocol_detected(ProtocolManager.CLIENT_MILLENNIUM)
    gm.set_remote_app_session(True)
    _play(gm, SESSION_MOVES_AFTER_ENGINE_REPLY)
    protocol.on_app_disconnected()
    gm.set_remote_app_session(False)
    yield protocol, engine


def test_restored_engine_holds_no_reply_computed_before_the_session(
    engine_with_reply_pending_at_connect,
):
    """The engine put back on disconnect must not keep a reply for an old position.

    The engine sat out the session holding the d7d5 it had computed before the
    app connected, and it is not told about the moves the app played. Restored
    with that move still pending, it ignored the take-over request ("Already
    have pending move") so the board never played, and the board's move logic
    treated d7d5 as the move Black still had to make.

    Regression: the restored engine's pending_move is d7d5 instead of None.
    """
    protocol, engine = engine_with_reply_pending_at_connect

    restored = protocol.player_manager.get_player(chess.BLACK)
    assert restored is engine
    assert restored.pending_move is None


def test_lifting_and_replacing_a_piece_after_disconnect_leaves_correction(
    gm, physical_board, engine_with_reply_pending_at_connect
):
    """Putting a lifted piece back after an app session must end correction.

    With d7d5 still pending, lifting a4 entered correction as a "wrong piece
    lifted" for d7d5, and replacing it ran the pending-move transcription,
    which pushed d7d5 onto a board where it is illegal. The error aborted the
    place handler before correction could exit, so every later touch re-entered
    guidance and the game never left correction.

    Regression: correction_mode.is_active is True after the place, or the
    place raises "push() expects move to be pseudo-legal ... d7d5".
    """
    from universalchess.board.piece_events import PIECE_LIFT, PIECE_PLACE

    physical_board.lifted_square = chess.A4
    gm.receive_field(PIECE_LIFT, chess.A4, None)
    physical_board.lifted_square = None
    gm.receive_field(PIECE_PLACE, chess.A4, None)

    assert gm.correction_mode.is_active is False


class _RecordingPlayers:
    """The LocalController's view of PlayerManager, recording move requests."""

    is_ready = True

    def __init__(self):
        self.requested_fens = []

    def start(self):
        return True

    def clear_pending_moves(self):
        return None

    def get_player(self, color):
        return None

    def request_move(self, chess_board):
        self.requested_fens.append(chess_board.fen())


def test_reactivating_local_play_leaves_started_players_running(caplog):
    """Local play coming back after an app session must not try to start players again.

    LocalController.start() starts the PlayerManager each time local play is
    activated, and its stop() deliberately leaves players running. On
    disconnect the restored engine was already started by set_player, so the
    second start was refused and logged "Black player failed to start" for a
    player that was working. A player that was never started must still be.

    Regression: start() returns False, logs that ERROR, or leaves the unstarted
    player UNINITIALIZED.
    """
    import logging

    from universalchess.players.base import PlayerState
    from universalchess.players.engine import EnginePlayer
    from universalchess.players.human import HumanPlayer
    from universalchess.players.manager import PlayerManager

    unstarted_human = HumanPlayer()
    running_engine = EnginePlayer()
    running_engine._set_state(PlayerState.READY)
    players = PlayerManager(unstarted_human, running_engine)

    with caplog.at_level(logging.WARNING):
        assert players.start() is True

    assert unstarted_human.state == PlayerState.READY
    assert running_engine.state == PlayerState.READY
    assert [r.getMessage() for r in caplog.records if "start" in r.getMessage()] == []


@pytest.fixture
def resumable_board_app(gm, physical_board, monkeypatch):
    """board_app wired to ``gm`` through a real ControllerManager, players recorded.

    _start_game_mode is replaced by the one effect resume depends on: it
    suppresses the LocalController's move requests during the replay. Yields
    ``(board_app, controllers, players)``.
    """
    from universalchess.app import board_app
    from universalchess.controllers.manager import ControllerManager

    controllers = ControllerManager(gm)
    local = controllers.create_local_controller()
    controllers.create_remote_controller()
    players = _RecordingPlayers()
    local._player_manager = players
    controllers.activate_local()

    monkeypatch.setattr(
        board_app,
        "_start_game_mode",
        lambda suppress_initial_move_request=False: local.set_suppress_move_requests(
            suppress_initial_move_request
        ),
    )
    monkeypatch.setattr(board_app, "_record_session_view", lambda *a, **k: None)
    monkeypatch.setattr(board_app._game, "protocol", SimpleNamespace(game_manager=gm))
    monkeypatch.setattr(board_app._game, "controller", controllers)
    monkeypatch.setattr(board_app._game, "display", None)
    yield board_app, controllers, players


def _connect_and_disconnect_app(controllers):
    with patch.object(controllers.remote_controller, "_create_emulators"):
        controllers.activate_remote()
        controllers.on_bluetooth_disconnected()


def test_local_play_keeps_one_game_thread_across_an_app_session(gm, resumable_board_app):
    """Re-activating local play after an app session must not start another game thread.

    LocalController.start() subscribed to GameManager on every activation, and
    each subscription starts a game thread that opens its own database engine
    and session and runs until the manager is torn down. Stopping local play
    never unsubscribes, so every app disconnect left one more thread and one
    more open session behind, and replaced the session the game writes through.

    Regression: gm.game_thread is a different thread after the disconnect, or
    more than one game thread for this manager is alive.
    """
    import threading

    _board_app, controllers, _players = resumable_board_app
    first_thread = gm.game_thread

    _connect_and_disconnect_app(controllers)

    assert gm.game_thread is first_thread
    game_threads = [
        thread for thread in threading.enumerate()
        if thread.is_alive() and getattr(thread, "_target", None) == gm._game_thread
    ]
    assert game_threads == [first_thread]


def _saved_board():
    saved = chess.Board()
    saved.push_uci(MOVE_BEFORE_SESSION)
    return saved


def test_disconnect_after_resuming_a_resigned_game_asks_for_no_move(
    gm, resumable_board_app
):
    """A finished game must not be played on when an app leaves.

    A resignation ends the game without a terminal position, so
    chess_board.is_game_over() is False. The disconnect hand-back checked only
    that, and asked the engine to reply in a game that was already over.

    Regression: requested_fens contains the resigned position.
    """
    board_app, controllers, players = resumable_board_app
    saved = _saved_board()
    assert board_app._resume_game({
        "id": 21, "moves": [MOVE_BEFORE_SESSION], "fen": saved.fen(),
        "result": "1-0", "termination": "resignation",
    }) is True

    players.requested_fens.clear()
    _connect_and_disconnect_app(controllers)

    assert players.requested_fens == []


def test_disconnect_after_a_resume_that_needed_correction_asks_for_the_next_move(
    gm, physical_board, resumable_board_app
):
    """A resumed game that started in correction must still be taken over on disconnect.

    Resume suppresses the LocalController's move requests while it replays the
    saved moves. Only the path where the pieces already matched turned that
    off; a resume that had to enter correction left it on for the rest of the
    game. Turn changes still reached the engine through GameManager, but the
    disconnect hand-back asks only the LocalController, so the engine restored
    for the side to move was never asked to play.

    Regression: requested_fens is empty after the disconnect.
    """
    from universalchess.board.piece_events import PIECE_PLACE

    board_app, controllers, players = resumable_board_app
    saved = _saved_board()
    physical_board.lifted_square = chess.D4
    assert board_app._resume_game(
        {"id": 21, "moves": [MOVE_BEFORE_SESSION], "fen": saved.fen(), "result": None}
    ) is True
    assert gm.correction_mode.is_active is True

    physical_board.lifted_square = None
    gm.receive_field(PIECE_PLACE, chess.D4, None)
    assert gm.correction_mode.is_active is False

    players.requested_fens.clear()
    _connect_and_disconnect_app(controllers)

    assert players.requested_fens == [saved.fen()]
