"""Tests for recovery from a slow or failed shared engine load.

Root cause these guard: black stopped presenting moves after a restart that
happened to follow an OTA update. The black player was Drawfish, which runs on
the Stockfish binary, and the analysis engine had begun loading that same binary
1.1 seconds earlier -- so the player waited on that load instead of launching a
second copy. On a single-core Pi Zero still busy finishing the update, the load
took 66.8 seconds::

    21:19:29.774 [EngineRegistry] Loading engine: /usr/games/stockfish
    21:19:30.874 [EngineRegistry] Waiting for another thread to load ...
    21:19:39.416 [Player] Drawfish (Default) still initializing, queueing move
    21:20:31.024 ERROR [EngineRegistry] Other thread failed to load ...
    21:20:36.622 INFO  [EngineRegistry] Engine loaded: /usr/games/stockfish

The waiter's cap was 60 seconds and the return value of ``Event.wait`` was
discarded, so a load that had not finished yet was indistinguishable from one
that had failed. It gave up 5.6 seconds early and reported a failure that never
happened. ``EnginePlayer`` then latched ``PlayerState.ERROR``, which was
terminal: the queued move request was dropped and every later request returned
early, so black could not move for the rest of the session even though the
engine had loaded successfully and was sitting in the registry.

Two layers are covered here, because they fail independently:

* the registry must not report a load that is still running as a failed one,
  which is what lets the queued move fire on its own;
* a failed load must not be a terminal player state, so that a wait which does
  legitimately expire still recovers on the next move request.
"""

import errno
import pathlib
import threading
import time
from unittest.mock import MagicMock, patch

import chess
import pytest

from universalchess.players import engine as engine_player_module
from universalchess.players.base import PlayerState
from universalchess.players.engine import EnginePlayer, EnginePlayerConfig
from universalchess.services import engine_registry
from universalchess.services.engine_registry import EngineLoadError, EngineRegistry

ENGINE_PATH = "/usr/games/stockfish"
POPEN = "universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci"

# Long enough that the waiting thread genuinely blocks, short enough to keep the
# suite fast. The production values are seconds; the behaviour under test is the
# ordering, not the duration.
SLOW_LOAD_SECONDS = 0.3
THREAD_JOIN_SECONDS = 5.0


@pytest.fixture
def registry():
    """A fresh EngineRegistry singleton, torn down after the test."""
    EngineRegistry._instance = None
    yield EngineRegistry.get_instance()
    if EngineRegistry._instance is not None:
        EngineRegistry._instance._engines.clear()
        EngineRegistry._instance = None


def _await_load_in_flight(registry_instance) -> None:
    """Block until a load has registered itself, so the next acquire waits on it.

    Without this the second acquire can win the race and become the loader
    itself, which would exercise the wrong branch entirely and make the test
    pass for the wrong reason.
    """
    deadline = time.monotonic() + THREAD_JOIN_SECONDS
    while not registry_instance._loading and time.monotonic() < deadline:
        time.sleep(0.005)
    assert registry_instance._loading, "no load registered; the waiter branch was never reached"


def test_a_waiting_thread_receives_the_engine_when_a_slow_load_finishes(registry):
    """A second acquire during a slow load must get the loaded engine.

    Why this test exists: this is the path black's engine took. The waiting
    thread must end up with the same pooled handle the loader produced, because
    that is what transitions the player to ready and releases the move request
    it queued while initializing.

    How a regression manifests: the waiter returns None (or raises) while the
    load succeeds moments later, so the engine is loaded and healthy but the
    consumer that asked for it believes it failed -- the exact split that left
    black unable to move.
    """
    def slow_popen(*_args, **_kwargs):
        time.sleep(SLOW_LOAD_SECONDS)
        return MagicMock()

    loader_handles = []
    with patch(POPEN, side_effect=slow_popen):
        loader = threading.Thread(
            target=lambda: loader_handles.append(registry.acquire(ENGINE_PATH)),
            daemon=True,
        )
        loader.start()
        _await_load_in_flight(registry)

        waiter_handle = registry.acquire(ENGINE_PATH)
        loader.join(timeout=THREAD_JOIN_SECONDS)

    assert loader_handles, "the loading thread never returned"
    assert waiter_handle is not None, "the waiting thread was told the load failed"
    assert waiter_handle is loader_handles[0], "the waiter got a different engine instance"
    # Both consumers hold a reference to the one pooled engine; a waiter that
    # silently loaded its own copy would leave a second Stockfish running on a
    # board with 426 MB of RAM.
    assert waiter_handle.ref_count == 2


def test_a_wait_that_expires_while_the_load_runs_is_not_reported_as_a_failure(
    registry, monkeypatch
):
    """An expired wait must be reported as still loading, not as a failed load.

    Why this test exists: the two are opposite situations and the old code could
    not tell them apart, because it discarded the result of ``Event.wait`` and
    inferred the outcome from whether the engine had appeared yet. Reporting
    "failed" for a load still in progress is what made a 66.8-second load look
    like a broken engine.

    How a regression manifests: collapsing the two back together returns the
    caller a permanent-sounding failure for a transient condition, and the
    consumer gives up on an engine that is about to become available.
    """
    monkeypatch.setattr(engine_registry, "ENGINE_LOAD_WAIT_SECONDS", 0.05)

    release_loader = threading.Event()

    def blocking_popen(*_args, **_kwargs):
        release_loader.wait(timeout=THREAD_JOIN_SECONDS)
        return MagicMock()

    with patch(POPEN, side_effect=blocking_popen):
        loader = threading.Thread(target=lambda: registry.acquire(ENGINE_PATH), daemon=True)
        loader.start()
        try:
            _await_load_in_flight(registry)
            with pytest.raises(EngineLoadError) as raised:
                registry.acquire_or_raise(ENGINE_PATH)
        finally:
            release_loader.set()
            loader.join(timeout=THREAD_JOIN_SECONDS)

    assert "still loading" in str(raised.value), (
        "a wait that expired while the load was still running must say so, not "
        f"claim the load failed: {raised.value}"
    )


def test_a_load_that_actually_fails_is_reported_as_a_failure(registry, monkeypatch):
    """When the loader really fails, the waiter must report a failure.

    Why this test exists: the counterpart risk to the previous test is
    over-correction -- treating every unsuccessful wait as "still loading" would
    make a genuinely broken engine look like a slow one, so a consumer would
    retry forever instead of surfacing the fault. The loader signals its event
    in a ``finally`` on the failure path too, so the waiter can tell.

    How a regression manifests: a missing or non-executable binary reports as
    still loading, and the user gets a player that never becomes ready and never
    explains why.
    """
    monkeypatch.setattr(engine_registry, "ENGINE_LOAD_WAIT_SECONDS", THREAD_JOIN_SECONDS)

    release_loader = threading.Event()

    def failing_popen(*_args, **_kwargs):
        release_loader.wait(timeout=THREAD_JOIN_SECONDS)
        raise OSError(errno.ENOENT, "No such file or directory")

    waiter_errors = []

    def _wait_for_engine():
        try:
            registry.acquire_or_raise(ENGINE_PATH)
        except EngineLoadError as exc:
            waiter_errors.append(exc)

    with patch(POPEN, side_effect=failing_popen):
        loader = threading.Thread(target=lambda: registry.acquire(ENGINE_PATH), daemon=True)
        loader.start()
        _await_load_in_flight(registry)

        waiter = threading.Thread(target=_wait_for_engine, daemon=True)
        waiter.start()
        # Let the waiter block on the event before the loader resolves it, so the
        # branch under test is the one that wakes on a signalled event.
        time.sleep(0.05)
        release_loader.set()
        waiter.join(timeout=THREAD_JOIN_SECONDS)
        loader.join(timeout=THREAD_JOIN_SECONDS)

    assert waiter_errors, "the waiter did not report the loader's failure"
    assert "still loading" not in str(waiter_errors[0]), (
        f"a genuine load failure must not be reported as a slow load: {waiter_errors[0]}"
    )


class RecordingRegistry:
    """Registry stand-in whose loads succeed or fail on demand.

    Invokes the callbacks synchronously. The real registry runs them on a
    background thread, but the player's contract is the same either way and the
    threading is not what these tests are about.
    """

    def __init__(self):
        self.attempts = 0
        self.should_fail = True

    def acquire_async(self, engine_path, on_ready, on_error=None):
        self.attempts += 1
        if self.should_fail:
            on_error(Exception(f"Failed to load engine: {engine_path}"))
        else:
            on_ready(MagicMock())


@pytest.fixture
def failing_player(monkeypatch):
    """An EnginePlayer left in ERROR by a failed load, plus its fake registry."""
    fake_registry = RecordingRegistry()
    monkeypatch.setattr(engine_player_module, "get_engine_registry", lambda: fake_registry)

    player = EnginePlayer(
        EnginePlayerConfig(name="Drawfish", color=chess.BLACK, engine_name="drawfish")
    )
    monkeypatch.setattr(player, "_resolve_engine_path", lambda: pathlib.Path(ENGINE_PATH))
    monkeypatch.setattr(player, "_resolve_uci_file_path", lambda: None)
    monkeypatch.setattr(player, "_configure_handle", lambda handle: None)

    assert player.start() is True
    assert player.state == PlayerState.ERROR, "the fake registry did not fail the load"
    return player, fake_registry


def test_a_player_whose_engine_load_failed_plays_once_a_retry_succeeds(
    failing_player, monkeypatch
):
    """A move request after a failed load must retry and then serve the move.

    Why this test exists: this is the user-visible symptom -- black never
    presented a move. ``PlayerState.ERROR`` was terminal, so the engine becoming
    available afterwards changed nothing and the only cure was restarting the
    service. Recovery has to reach the *move*, not merely the state, because the
    request is queued while initializing and is only released by the transition
    to ready.

    How a regression manifests: the player's state recovers but the queued
    request is dropped, so the board sits waiting for an engine that is loaded
    and idle -- indistinguishable, to the user, from the original fault.
    """
    player, fake_registry = failing_player
    requested = []
    monkeypatch.setattr(player, "_do_request_move", lambda board: requested.append(board.fen()))

    fake_registry.should_fail = False
    board = chess.Board()
    player.request_move(board)

    assert player.state == PlayerState.READY, "the player did not retry the failed load"
    assert fake_registry.attempts == 2, (
        f"expected exactly one retry after the initial failure; got {fake_registry.attempts} loads"
    )
    assert requested == [board.fen()], (
        "the queued move request was not served once the engine became available"
    )


def test_engine_load_retries_are_bounded(failing_player):
    """A permanently failing engine must not be retried on every move request.

    Why this test exists: an engine that cannot load -- a deleted binary, one
    built for the wrong architecture -- would otherwise spawn a fresh load
    attempt for every request, on the board where that costs the most. The retry
    exists for a transient failure, so it needs a budget.

    How a regression manifests: no error, just a board that launches processes in
    a loop and gets slower, with the original load failure buried in the repeats.
    """
    player, fake_registry = failing_player
    board = chess.Board()

    for _ in range(5):
        player.request_move(board)

    assert player.state == PlayerState.ERROR
    assert fake_registry.attempts <= 1 + engine_player_module.MAX_ENGINE_LOAD_RETRIES, (
        f"{fake_registry.attempts} load attempts exceeds the retry budget of "
        f"{engine_player_module.MAX_ENGINE_LOAD_RETRIES}"
    )


def test_a_successful_load_restores_the_retry_budget(failing_player, monkeypatch):
    """Recovering must reset the retry count, not consume it for the session.

    Why this test exists: the budget bounds one episode of failure, not the
    lifetime of the player. A board that recovers from a busy moment at startup
    and then loses its engine hours later -- an engine reinstall, an OOM kill --
    must still be able to come back, otherwise the first transient failure
    quietly spends the allowance for the whole session.

    How a regression manifests: recovery works exactly once per player and the
    second occurrence is permanent, which looks like an unrelated intermittent
    fault because the first one healed.
    """
    player, fake_registry = failing_player
    monkeypatch.setattr(player, "_do_request_move", lambda board: None)
    board = chess.Board()

    fake_registry.should_fail = False
    player.request_move(board)
    assert player.state == PlayerState.READY

    # Lose the engine again, exactly as a later failure would.
    fake_registry.should_fail = True
    player._set_state(PlayerState.ERROR, "engine lost")
    attempts_before_second_recovery = fake_registry.attempts

    fake_registry.should_fail = False
    player.request_move(board)

    assert player.state == PlayerState.READY, (
        "the player could not recover a second time; the retry budget was not reset "
        "by the successful load"
    )
    assert fake_registry.attempts == attempts_before_second_recovery + 1
