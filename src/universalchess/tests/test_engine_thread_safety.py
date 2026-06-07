"""Thread-safety tests for EnginePlayer pending-move / thinking state.

Background / why these tests exist
----------------------------------
The engine computes its move on a background thread (handle.play blocks for the
move's time limit). Meanwhile the game thread can clear the pending move
(on_move_made / on_takeback / on_new_game / clear_pending_move) and can issue new
move requests. _pending_move and _thinking are shared between these threads.

Previously the lock guarded only _engine_handle, so:
- an in-flight computation that finished AFTER a takeback / external clear would
  resurrect a now-invalid pending move (and fire the LED callback for it);
- the "already thinking / already have pending" guard in _do_request_move was a
  non-atomic check-then-act.

These tests pin the cancellation contract: a computation invalidated mid-flight
must discard its result, while the normal path still produces the pending move,
and a duplicate request while thinking must not start a second computation. The
blocking fake handle makes the interleaving deterministic (no sleeps/races).
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import chess

from universalchess.players.engine import EnginePlayer, PlayerState

E2E4 = chess.Move.from_uci("e2e4")


class _BlockingHandle:
    """Fake engine handle whose play() blocks until released, deterministically."""

    def __init__(self, move):
        self._move = move
        self.play_started = threading.Event()
        self.release = threading.Event()
        self._calls_lock = threading.Lock()
        self.play_calls = 0

    def play(self, board, limit, options=None):
        with self._calls_lock:
            self.play_calls += 1
        self.play_started.set()
        self.release.wait(timeout=5)
        return SimpleNamespace(move=self._move)

    def configure(self, options):
        pass


def _engine_with_handle(handle):
    player = EnginePlayer()
    player._color = chess.WHITE
    player._engine_handle = handle
    return player


def test_normal_result_sets_pending_and_fires_callback():
    """The success path stores the move and notifies once.

    Why: baseline so the cancellation tests below cannot pass by simply never
    delivering moves.

    How the regression manifests: _pending_move stays None or the callback does
    not fire exactly once.
    """
    handle = _BlockingHandle(E2E4)
    handle.release.set()  # do not block
    player = _engine_with_handle(handle)
    notified = []
    player._pending_move_callback = lambda m: notified.append(m)

    player._do_request_move(chess.Board())
    player._think_thread.join(timeout=3)

    assert player._pending_move == E2E4
    assert notified == [E2E4]


def test_stale_result_discarded_after_takeback():
    """A computation finishing after a takeback must not resurrect a pending move.

    Why: the takeback invalidated the position; the engine's move for the old
    position is no longer valid and must be dropped.

    How the regression manifests: _pending_move becomes E2E4 again and the LED
    callback fires for it, so the player is prompted to play a move for a position
    that no longer exists.
    """
    handle = _BlockingHandle(E2E4)
    player = _engine_with_handle(handle)
    notified = []
    player._pending_move_callback = lambda m: notified.append(m)
    board = chess.Board()

    player._do_request_move(board)
    assert handle.play_started.wait(timeout=3)
    player.on_takeback(board)  # invalidate while engine is mid-think
    handle.release.set()
    player._think_thread.join(timeout=3)

    assert player._pending_move is None
    assert notified == []


def test_stale_result_discarded_after_clear_pending_move():
    """A computation finishing after an external clear must be discarded too.

    Why: clear_pending_move runs when an external app takes over control; an
    in-flight engine result must not reinstate a pending move behind its back.

    How the regression manifests: _pending_move becomes E2E4 and the callback
    fires after the takeover cleared it.
    """
    handle = _BlockingHandle(E2E4)
    player = _engine_with_handle(handle)
    notified = []
    player._pending_move_callback = lambda m: notified.append(m)
    board = chess.Board()

    player._do_request_move(board)
    assert handle.play_started.wait(timeout=3)
    player.clear_pending_move()
    handle.release.set()
    player._think_thread.join(timeout=3)

    assert player._pending_move is None
    assert notified == []


def test_second_request_while_thinking_is_ignored():
    """A duplicate request while thinking must not start a second computation.

    Why: the thinking guard must be atomic; two overlapping requests must not both
    spawn engine computations on the shared handle.

    How the regression manifests: play_calls > 1, i.e. a second computation was
    started while the first was still in flight.
    """
    handle = _BlockingHandle(E2E4)
    player = _engine_with_handle(handle)
    board = chess.Board()

    player._do_request_move(board)
    assert handle.play_started.wait(timeout=3)
    player._do_request_move(board)  # must be ignored: already thinking
    handle.release.set()
    player._think_thread.join(timeout=3)

    assert handle.play_calls == 1
    assert player._pending_move == E2E4


def test_on_move_made_does_not_cancel_in_flight_think():
    """on_move_made must not cancel the engine's freshly-started think.

    Why: when the opponent's move switches the turn to the engine, the controller
    starts the engine thinking (request_move) and also notifies on_move_made for
    that same move; these race. on_move_made is a normal completion, not a
    position invalidation, so it must only clear the consumed pending move - it
    must NOT abort the in-flight computation for the engine's own turn.

    How the regression manifests: if on_move_made invalidates (bumps the think
    generation), the engine computes its move but discards it as "superseded",
    so the engine never plays its turn - _pending_move stays None and the LED
    callback never fires.
    """
    handle = _BlockingHandle(E2E4)
    player = _engine_with_handle(handle)
    notified = []
    player._pending_move_callback = lambda m: notified.append(m)
    board = chess.Board()

    player._do_request_move(board)  # engine starts thinking for its turn
    assert handle.play_started.wait(timeout=3)
    # The move that triggered the engine's turn is announced while it thinks.
    player.on_move_made(chess.Move.from_uci("d2d4"), board)
    handle.release.set()
    player._think_thread.join(timeout=3)

    assert player._pending_move == E2E4
    assert notified == [E2E4]


def test_stop_clears_handle_and_stops_even_if_release_raises():
    """stop() must not leak the handle or skip STOPPED if release() raises.

    Why: the engine handle is a shared resource; an exception during release must
    not leave the player holding a dangling reference or stuck in a non-stopped
    state (the original code had no try/finally around the release).

    How the regression manifests: without the guarded release, the raised error
    propagates, _engine_handle stays set (leak) and state never becomes STOPPED.
    """
    player = EnginePlayer()
    player._engine_handle = object()

    class _BoomRegistry:
        def release(self, handle):
            raise RuntimeError("registry release failed")

    with patch("universalchess.players.engine.get_engine_registry", return_value=_BoomRegistry()):
        player.stop()  # must not raise

    assert player._engine_handle is None
    assert player._state == PlayerState.STOPPED
