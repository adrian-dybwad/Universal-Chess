# Engine Registry Tests
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Tests for the EngineRegistry singleton that manages shared UCI engine
# instances with serialized access.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import errno
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


class TestEngineRegistry:
    """Tests for EngineRegistry singleton and EngineHandle."""
    
    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Reset the registry singleton between tests."""
        from universalchess.services.engine_registry import EngineRegistry
        # Clear singleton and engines
        EngineRegistry._instance = None
        yield
        # Cleanup after test
        if EngineRegistry._instance is not None:
            EngineRegistry._instance._engines.clear()
            EngineRegistry._instance = None
    
    def test_singleton_returns_same_instance(self):
        """Test that get_instance() always returns the same registry.
        
        Expected: Multiple calls return identical object.
        Reason: Singleton pattern ensures single point of engine management.
        """
        from universalchess.services.engine_registry import EngineRegistry
        
        r1 = EngineRegistry.get_instance()
        r2 = EngineRegistry.get_instance()
        
        assert r1 is r2
    
    def test_get_engine_registry_returns_singleton(self):
        """Test that get_engine_registry() helper returns the singleton.
        
        Expected: Helper function returns same instance as get_instance().
        Reason: Convenience function should behave identically.
        """
        from universalchess.services.engine_registry import (
            get_engine_registry,
            EngineRegistry
        )
        
        r1 = get_engine_registry()
        r2 = EngineRegistry.get_instance()
        
        assert r1 is r2
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_loads_engine_on_first_request(self, mock_popen):
        """Test that acquire() loads engine on first request.
        
        Expected: popen_uci called once, handle returned.
        Reason: First acquire should trigger engine initialization.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")
        
        assert handle is not None
        assert handle.engine is mock_engine
        assert handle.ref_count == 1
        mock_popen.assert_called_once()
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_reuses_engine_on_second_request(self, mock_popen):
        """Test that acquire() reuses engine for same path.
        
        Expected: popen_uci called once, same handle returned twice.
        Reason: Engine sharing avoids duplicate processes.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        handle1 = registry.acquire("/usr/games/stockfish")
        handle2 = registry.acquire("/usr/games/stockfish")
        
        assert handle1 is handle2
        assert handle1.ref_count == 2
        mock_popen.assert_called_once()
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_different_paths_different_engines(self, mock_popen):
        """Test that different paths get different engine instances.
        
        Expected: Each unique path gets its own engine.
        Reason: Different engine binaries need separate processes.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        engines = []
        def make_engine(path, **kwargs):
            e = MagicMock()
            e.path = path
            engines.append(e)
            return e
        mock_popen.side_effect = make_engine
        
        registry = get_engine_registry()
        handle1 = registry.acquire("/usr/games/stockfish")
        handle2 = registry.acquire("/usr/games/ct800")
        
        assert handle1 is not handle2
        assert handle1.engine is not handle2.engine
        assert mock_popen.call_count == 2
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_release_decrements_ref_count(self, mock_popen):
        """Test that release() decrements reference count.
        
        Expected: Ref count decremented, engine kept alive.
        Reason: Other consumers may still need the engine.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")
        assert handle.ref_count == 1
        
        registry.release(handle)
        assert handle.ref_count == 0
        
        # Engine should still exist in registry
        assert "/usr/games/stockfish" in str(registry.get_loaded_engines())
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_shutdown_closes_all_engines(self, mock_popen):
        """Test that shutdown() closes all loaded engines.
        
        Expected: All engine.quit() called, registry cleared.
        Reason: Clean shutdown requires terminating all engine processes.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine1 = MagicMock()
        mock_engine2 = MagicMock()
        mock_popen.side_effect = [mock_engine1, mock_engine2]
        
        registry = get_engine_registry()
        registry.acquire("/usr/games/stockfish")
        registry.acquire("/usr/games/ct800")
        
        registry.shutdown()
        
        mock_engine1.quit.assert_called_once()
        mock_engine2.quit.assert_called_once()
        assert len(registry.get_loaded_engines()) == 0
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_handle_play_acquires_lock(self, mock_popen):
        """Test that EngineHandle.play() acquires lock for serialized access.
        
        Expected: Lock is held during play() call.
        Reason: UCI engines are stateful, concurrent access would corrupt state.
        """
        import chess
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_result = MagicMock()
        mock_engine.play.return_value = mock_result
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")
        
        board = chess.Board()
        limit = chess.engine.Limit(time=1.0)
        
        # Track if lock was held during call
        lock_held_during_call = []
        original_play = mock_engine.play
        def track_lock(*args, **kwargs):
            lock_held_during_call.append(handle.lock.locked())
            return mock_result
        mock_engine.play.side_effect = track_lock
        
        handle.play(board, limit)
        
        assert lock_held_during_call == [True]
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_handle_analyse_acquires_lock(self, mock_popen):
        """Test that EngineHandle.analyse() acquires lock for serialized access.
        
        Expected: Lock is held during analyse() call.
        Reason: Analysis must not run concurrently with other operations.
        """
        import chess
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_info = {"score": MagicMock()}
        mock_engine.analyse.return_value = mock_info
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")
        
        board = chess.Board()
        limit = chess.engine.Limit(time=0.1)
        
        # Track if lock was held during call
        lock_held_during_call = []
        def track_lock(*args, **kwargs):
            lock_held_during_call.append(handle.lock.locked())
            return mock_info
        mock_engine.analyse.side_effect = track_lock
        
        handle.analyse(board, limit)
        
        assert lock_held_during_call == [True]
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_handle_analyse_defaults_to_single_info_not_list(self, mock_popen):
        """EngineHandle.analyse() must forward multipv=None by default.

        Why this test exists: python-chess returns a single InfoDict only when
        multipv is None; passing any int (even 1) makes it return a List[InfoDict].
        The previous default of multipv=1 silently broke every caller that indexed
        the result as a dict (analysis score parsing did `"score" not in info` on a
        list -> always True -> no score ever set, so the analysis widget stayed at
        +0.0 with an empty graph).

        How the regression manifests: if the default is an int again, the captured
        multipv kwarg below is 1 (or the call passes a non-None int), and real
        python-chess would hand callers a list instead of a dict.
        """
        import chess
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        captured = {}

        def fake_analyse(board, limit, **kwargs):
            captured.update(kwargs)
            # Mirror python-chess: None -> single InfoDict, int -> list.
            if kwargs.get("multipv") is None:
                return {"score": MagicMock()}
            return [{"score": MagicMock()}]

        mock_engine.analyse.side_effect = fake_analyse
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")

        result = handle.analyse(chess.Board(), chess.engine.Limit(time=0.1))

        assert captured.get("multipv") is None, "default analyse must forward multipv=None"
        assert isinstance(result, dict), "default analyse must return a single InfoDict, not a list"
        assert "score" in result

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_returns_none_on_failure(self, mock_popen):
        """Test that acquire() returns None when engine fails to load.
        
        Expected: None returned, no crash.
        Reason: Graceful degradation when engine unavailable.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_popen.side_effect = Exception("Engine not found")
        
        registry = get_engine_registry()
        handle = registry.acquire("/nonexistent/engine")
        
        assert handle is None
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_async_calls_on_ready(self, mock_popen):
        """Test that acquire_async() calls on_ready callback.
        
        Expected: Callback invoked with handle.
        Reason: Async pattern needs callback notification.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine
        
        registry = get_engine_registry()
        
        received_handle = []
        event = threading.Event()
        
        def on_ready(handle):
            received_handle.append(handle)
            event.set()
        
        registry.acquire_async("/usr/games/stockfish", on_ready=on_ready)
        
        # Wait for async completion
        event.wait(timeout=2.0)
        
        assert len(received_handle) == 1
        assert received_handle[0].engine is mock_engine
    
    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_async_calls_on_error(self, mock_popen):
        """Test that acquire_async() calls on_error callback on failure.
        
        Expected: Error callback invoked with exception.
        Reason: Caller needs to know when async load fails.
        """
        from universalchess.services.engine_registry import get_engine_registry
        
        mock_popen.side_effect = Exception("Engine not found")
        
        registry = get_engine_registry()
        
        received_error = []
        event = threading.Event()
        
        def on_ready(handle):
            pass  # Should not be called
        
        def on_error(e):
            received_error.append(e)
            event.set()
        
        registry.acquire_async(
            "/nonexistent/engine",
            on_ready=on_ready,
            on_error=on_error
        )
        
        # Wait for async completion
        event.wait(timeout=2.0)
        
        assert len(received_error) == 1

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_dedicated_is_private_not_pooled(self, mock_popen):
        """acquire_dedicated must return a private instance, never the shared pool.

        Why this test exists: pondering needs an engine whose background search is
        never touched by another consumer. If a dedicated acquire were pooled by
        path (like acquire), a later acquire() for the same binary would hand the
        analysis service the pondering engine and interrupt it.

        How the regression manifests: two dedicated acquires for the same path
        would return the same handle, or the handle would appear in _engines and a
        subsequent shared acquire() would reuse it (popen called only once).
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_popen.side_effect = lambda *a, **k: MagicMock()

        registry = get_engine_registry()
        d1 = registry.acquire_dedicated("/usr/games/stockfish")
        d2 = registry.acquire_dedicated("/usr/games/stockfish")

        assert d1 is not None and d2 is not None
        assert d1 is not d2, "each dedicated acquire must be its own instance"
        assert d1.engine is not d2.engine
        assert d1.shared is False and d2.shared is False
        # Not pooled: a shared acquire spawns yet another (3rd) process.
        shared = registry.acquire("/usr/games/stockfish")
        assert shared is not d1 and shared is not d2
        assert mock_popen.call_count == 3

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_release_dedicated_quits_engine(self, mock_popen):
        """Releasing a dedicated handle must quit its engine process.

        Why this test exists: a dedicated engine has a single owner, so releasing
        it must free the process immediately (a shared engine, by contrast, stays
        loaded for reuse). Leaking it would leave a pondering engine burning CPU.

        How the regression manifests: if release treated a dedicated handle like a
        shared one, engine.quit() is never called and the process lingers.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        handle = registry.acquire_dedicated("/usr/games/stockfish")
        assert handle is not None

        registry.release(handle)

        mock_engine.quit.assert_called_once()

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_release_shared_does_not_quit_engine(self, mock_popen):
        """Releasing a shared handle must NOT quit the engine (kept for reuse).

        Why this test exists: guards the boundary that the dedicated-release change
        must not alter shared behavior. A shared engine is reused across consumers,
        so a single consumer's release only decrements the ref count.

        How the regression manifests: if release quit shared engines, the analysis
        service would lose its engine the moment any other consumer released.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")

        registry.release(handle)

        mock_engine.quit.assert_not_called()
        assert handle.ref_count == 0

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_shutdown_quits_unreleased_dedicated_engine(self, mock_popen):
        """shutdown() must quit dedicated engines a consumer failed to release.

        Why this test exists: an abrupt shutdown mid-game can leave a pondering
        engine running; shutdown is the last chance to reap it.

        How the regression manifests: if shutdown only iterated the shared pool, an
        unreleased dedicated engine's quit() is never called and the process leaks.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        registry.acquire_dedicated("/usr/games/stockfish")  # intentionally not released

        registry.shutdown()

        mock_engine.quit.assert_called_once()

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_handle_play_forwards_ponder_and_game(self, mock_popen):
        """EngineHandle.play must forward ponder and the game token to the engine.

        Why this test exists: python-chess only keeps pondering / issues ponderhit
        when it receives ponder=True and a stable game token across calls. If the
        wrapper dropped these kwargs, pondering would silently never engage.

        How the regression manifests: the captured kwargs below lack 'ponder'/'game'
        (or ponder is False), so the engine never runs a background search.
        """
        import chess
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        captured = {}

        def fake_play(board, limit, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_engine.play.side_effect = fake_play
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        handle = registry.acquire_dedicated("/usr/games/stockfish")

        token = object()
        handle.play(chess.Board(), chess.engine.Limit(time=0.1), ponder=True, game=token)

        assert captured.get("ponder") is True
        assert captured.get("game") is token

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_handle_play_defaults_to_no_ponder(self, mock_popen):
        """EngineHandle.play must default ponder off so normal play is unaffected.

        Why this test exists: pondering is opt-in; the default play path (analysis
        off / battery boards) must not start a background search.

        How the regression manifests: a default of ponder=True would leave every
        engine pondering after each move even when the toggle is off.
        """
        import chess
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock()
        captured = {}

        def fake_play(board, limit, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_engine.play.side_effect = fake_play
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        handle = registry.acquire("/usr/games/stockfish")

        handle.play(chess.Board(), chess.engine.Limit(time=0.1))

        assert captured.get("ponder") is False
        assert captured.get("game") is None

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_reloads_dead_cached_engine(self, mock_popen):
        """A cached engine whose event loop has died is evicted and reloaded.

        Why this test exists: when a shared engine's subprocess is killed (the
        OOM-killer reaping Stockfish under memory pressure was the production
        trigger), python-chess sets SimpleEngine._shutdown and every later call
        raises EngineTerminatedError("engine event loop dead"). The registry
        previously kept handing out that dead cached handle forever, so both
        engine players stayed in ERROR and never moved (no engine-vs-engine
        auto-play). acquire must detect the dead handle, evict it, and load a
        fresh live one.

        How the regression manifests: without the liveness check the second
        acquire reuses the dead handle (popen called once) and returns an engine
        still flagged _shutdown, so the caller gets a corpse again.
        """
        from universalchess.services.engine_registry import get_engine_registry

        created = []

        def make(*a, **k):
            e = MagicMock()
            e._shutdown = False
            created.append(e)
            return e
        mock_popen.side_effect = make

        registry = get_engine_registry()
        first = registry.acquire("/usr/games/stockfish")
        assert first is not None and first.ref_count == 1

        # Simulate the subprocess dying (e.g. OOM kill): python-chess sets this.
        first.engine._shutdown = True
        registry.release(first)  # consumer drops the now-dead handle -> ref 0

        second = registry.acquire("/usr/games/stockfish")

        assert second is not None
        assert second is not first, "must not reuse the dead handle"
        assert second.engine is not first.engine
        assert second.engine._shutdown is False, "reloaded engine must be live"
        assert mock_popen.call_count == 2, "dead engine must trigger a reload"

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_dead_cached_engine_does_not_leak_refs(self, mock_popen):
        """Reacquiring after a death yields a fresh handle at ref 1, not a leak.

        Why this test exists: the dead-handle bug also leaked references -- the
        old acquire incremented ref_count before discovering the engine was dead,
        so each failed reuse pushed the count up (observed climbing 3->11 in
        production) and it never returned to zero. Both players hitting the dead
        handle without releasing must still leave the reloaded engine at exactly
        one reference per live acquire.

        How the regression manifests: if the ref increment stays on the dead
        path, the reloaded handle's ref_count is greater than 1.
        """
        from universalchess.services.engine_registry import get_engine_registry

        def make(*a, **k):
            e = MagicMock()
            e._shutdown = False
            return e
        mock_popen.side_effect = make

        registry = get_engine_registry()
        first = registry.acquire("/usr/games/stockfish")
        # Mark dead WITHOUT releasing: mimics a consumer still holding the corpse
        # when the next consumer acquires (the two-engine game: White holds the
        # dead handle while Black acquires).
        first.engine._shutdown = True

        second = registry.acquire("/usr/games/stockfish")

        assert second is not first
        assert second.ref_count == 1, "fresh handle must start at one reference"

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_quits_evicted_dead_engine(self, mock_popen):
        """Evicting a dead cached engine quits its (defunct) process handle.

        Why this test exists: reaping the dead handle keeps python-chess's
        transport/threads from lingering; the registry must call quit() on the
        corpse it drops, not just forget the reference.

        How the regression manifests: if eviction only deletes the dict entry,
        quit() is never called on the dead engine.
        """
        from universalchess.services.engine_registry import get_engine_registry

        def make(*a, **k):
            e = MagicMock()
            e._shutdown = False
            return e
        mock_popen.side_effect = make

        registry = get_engine_registry()
        first = registry.acquire("/usr/games/stockfish")
        dead = first.engine
        dead._shutdown = True

        registry.acquire("/usr/games/stockfish")

        dead.quit.assert_called_once()

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_unused_quits_and_removes_ref_zero_engine(self, mock_popen):
        """evict_unused() unloads a pooled engine no consumer references.

        Why this test exists: switching engines (e.g. Ethereal -> Stockfish) must
        free the previous engine instead of leaving it as an idle process eating
        memory. After the ended game releases it (ref 0), evict_unused must quit
        the process and drop it from the pool, while leaving a still-referenced
        engine untouched.

        How the regression manifests: if evict_unused skipped ref-0 engines the
        Ethereal process lingers (quit never called, still in the pool); if it
        reaped referenced engines it would kill Stockfish out from under the
        active player.
        """
        from universalchess.services.engine_registry import get_engine_registry

        e_ethereal = MagicMock(); e_ethereal._shutdown = False
        e_stock = MagicMock(); e_stock._shutdown = False
        mock_popen.side_effect = [e_ethereal, e_stock]

        registry = get_engine_registry()
        h_ethereal = registry.acquire("/opt/universalchess/engines/ethereal")
        registry.acquire("/usr/games/stockfish")  # stays referenced (ref 1)
        registry.release(h_ethereal)  # ethereal now unused (ref 0)

        evicted = registry.evict_unused()

        assert evicted == 1
        e_ethereal.quit.assert_called_once()
        e_stock.quit.assert_not_called()
        loaded = registry.get_loaded_engines()
        assert not any("ethereal" in path for path in loaded), "ethereal must be dropped"
        assert any("stockfish" in path for path in loaded), "stockfish must remain pooled"

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_unused_keeps_referenced_engine(self, mock_popen):
        """evict_unused() must not touch an engine a consumer still holds.

        Why this test exists: an engine in active use (a player mid-game, the
        analysis service) has ref_count > 0; reaping it would break the game.

        How the regression manifests: if evict_unused used the wrong ref
        comparison it would quit the held engine and remove it from the pool.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock(); mock_engine._shutdown = False
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        registry.acquire("/usr/games/stockfish")  # ref 1, not released

        evicted = registry.evict_unused()

        assert evicted == 0
        mock_engine.quit.assert_not_called()
        assert any("stockfish" in path for path in registry.get_loaded_engines())

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_unused_ignores_dedicated_engines(self, mock_popen):
        """evict_unused() must ignore dedicated (ponder) engines.

        Why this test exists: dedicated engines are owned by a single consumer
        and released explicitly (releasing quits them); they are never pooled, so
        the pool-eviction sweep must not reach them.

        How the regression manifests: if dedicated handles were tracked in the
        pool, evict_unused would quit a live pondering engine mid-game.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock(); mock_engine._shutdown = False
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        registry.acquire_dedicated("/usr/games/stockfish")

        evicted = registry.evict_unused()

        assert evicted == 0
        mock_engine.quit.assert_not_called()

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_if_unused_reaps_only_the_named_ref_zero_engine(self, mock_popen):
        """evict_if_unused() quits one named pooled engine, leaving others pooled.

        Why this test exists: a UCI option probe in the web process spawns an
        engine that nothing else wants afterwards, and the web process has no
        game-teardown boundary that calls evict_unused(). The probe therefore
        reaps its own engine by name. It must not sweep the whole pool the way
        evict_unused() does, or a probe of one engine would unload an idle
        engine another consumer intends to reuse.

        How the regression manifests: if the method fell back to a full sweep,
        the arasan assertion below fails because arasan was quit too; if it
        reaped nothing, ct800's process lingers and the web process leaks one
        engine per distinct engine ever probed.
        """
        from universalchess.services.engine_registry import get_engine_registry

        e_ct800 = MagicMock(); e_ct800._shutdown = False
        e_arasan = MagicMock(); e_arasan._shutdown = False
        mock_popen.side_effect = [e_ct800, e_arasan]

        registry = get_engine_registry()
        h_ct800 = registry.acquire("/opt/universalchess/engines/ct800")
        h_arasan = registry.acquire("/opt/universalchess/engines/arasan")
        registry.release(h_ct800)
        registry.release(h_arasan)  # both pooled at ref 0

        reaped = registry.evict_if_unused("/opt/universalchess/engines/ct800")

        assert reaped is True
        e_ct800.quit.assert_called_once()
        e_arasan.quit.assert_not_called()
        loaded = registry.get_loaded_engines()
        assert not any("ct800" in path for path in loaded), "probed engine must be dropped"
        assert any("arasan" in path for path in loaded), "unnamed engine must stay pooled"

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_if_unused_keeps_engine_another_consumer_holds(self, mock_popen):
        """evict_if_unused() must not quit an engine still referenced.

        Why this test exists: probing reads only the handshake options dict, so a
        probe can legitimately run against the very engine a game is playing
        with. Reaping on ref > 0 would kill the engine mid-game -- the probe must
        clean up only when it is the last consumer.

        How the regression manifests: with a ref check that ignores outstanding
        references, quit() is called on a live game engine and the pool no longer
        contains it, so the player's next move request fails.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock(); mock_engine._shutdown = False
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        registry.acquire("/usr/games/stockfish")            # the game's reference
        probe_handle = registry.acquire("/usr/games/stockfish")  # the probe's
        registry.release(probe_handle)                      # back to ref 1

        reaped = registry.evict_if_unused("/usr/games/stockfish")

        assert reaped is False
        mock_engine.quit.assert_not_called()
        assert any("stockfish" in path for path in registry.get_loaded_engines())

    def test_evict_if_unused_is_a_noop_for_an_unpooled_path(self):
        """evict_if_unused() on a path that was never pooled reports False.

        Why this test exists: the probe calls this unconditionally in a finally
        block, including on the failure path where no engine was ever pooled. It
        must be a quiet no-op there, not a KeyError that masks the real launch
        error the caller is about to raise.

        How the regression manifests: an unguarded pool lookup raises KeyError
        out of the finally block, replacing EngineProbeError with a 500.
        """
        from universalchess.services.engine_registry import get_engine_registry

        registry = get_engine_registry()

        assert registry.evict_if_unused("/opt/universalchess/engines/never-loaded") is False

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_evict_if_unused_ignores_dedicated_engines(self, mock_popen):
        """evict_if_unused() must not reach a dedicated (ponder) engine.

        Why this test exists: dedicated handles are owned by one consumer and are
        never pooled by path. A probe of the same binary must not quit the
        pondering engine that shares that path.

        How the regression manifests: if dedicated handles were matched by path,
        quit() is called on a live pondering engine and the game loses its
        opponent mid-search.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock(); mock_engine._shutdown = False
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        registry.acquire_dedicated("/usr/games/stockfish")

        assert registry.evict_if_unused("/usr/games/stockfish") is False
        mock_engine.quit.assert_not_called()

    def test_configure_forwards_only_engine_advertised_options(self):
        """EngineHandle.configure drops options the engine does not advertise.

        Why this test exists: the app applies a shared profile (the Default
        section carries Threads=1) to every engine, but the derived policy
        engines (Worstfish/Drawfish) advertise only Randomness/AvoidCaptures.
        python-chess's configure() raises EngineError for any option the engine
        never advertised, which aborted the derived player's initialization
        before it could move (the "not suggesting moves" symptom on dgt-64).
        Filtering to advertised options at this boundary keeps one generic
        profile compatible with every engine.

        How the regression manifests: without the filter, the unsupported
        Threads is forwarded to the engine and configure() raises, so the mock's
        configure would be called with Threads present (or the real engine would
        reject it).
        """
        from unittest.mock import MagicMock

        import chess.engine

        from universalchess.services.engine_registry import EngineHandle

        engine = MagicMock()
        # Case-insensitive map, exactly as python-chess exposes advertised options.
        engine.options = chess.engine.UciOptionMap(
            [("Randomness", object()), ("AvoidCaptures", object())]
        )
        handle = EngineHandle(path="/opt/universalchess/engines/worstfish", engine=engine)

        handle.configure({"Threads": "1", "Randomness": "50"})

        engine.configure.assert_called_once_with({"Randomness": "50"})

    def test_configure_forwards_all_when_every_option_is_advertised(self):
        """EngineHandle.configure passes options through unchanged when supported.

        Why this test exists: the filter must not strip legitimate options a
        capable engine (Stockfish advertises Threads, Hash, ...) does accept;
        over-filtering would silently drop a user's engine settings.

        How the regression manifests: if membership were tested case-sensitively
        (or the filter were too aggressive), a differently-cased but advertised
        option like "threads" would be dropped and the forwarded dict would be
        missing it.
        """
        from unittest.mock import MagicMock

        import chess.engine

        from universalchess.services.engine_registry import EngineHandle

        engine = MagicMock()
        engine.options = chess.engine.UciOptionMap(
            [("Threads", object()), ("Hash", object())]
        )
        handle = EngineHandle(path="/usr/games/stockfish", engine=engine)

        # "threads" differs in case from the advertised "Threads"; UCI option
        # names are case-insensitive, so it must survive the filter.
        handle.configure({"threads": "2", "Hash": "16"})

        engine.configure.assert_called_once_with({"threads": "2", "Hash": "16"})

    def test_play_filters_unsupported_options_before_search(self):
        """EngineHandle.play strips unsupported options before applying them.

        Why this test exists: the per-move path also applies the profile options
        (EnginePlayer passes its UCI options to play()), so play() must filter
        just like configure() -- otherwise a derived engine that initialized
        would still crash on the first move when Threads is re-applied.

        How the regression manifests: without filtering, play() forwards Threads
        to engine.configure and python-chess raises mid-move, so no bestmove is
        produced.
        """
        from unittest.mock import MagicMock

        import chess
        import chess.engine

        from universalchess.services.engine_registry import EngineHandle

        engine = MagicMock()
        engine.options = chess.engine.UciOptionMap([("Randomness", object())])
        engine.play.return_value = MagicMock()
        handle = EngineHandle(path="/opt/universalchess/engines/drawfish", engine=engine)

        handle.play(
            chess.Board(),
            chess.engine.Limit(time=0.1),
            options={"Threads": "1", "Randomness": "50"},
        )

        engine.configure.assert_called_once_with({"Randomness": "50"})

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_async_acquire_releases_handle_when_on_ready_raises(self, mock_popen):
        """A failed on_ready must not leak the just-loaded pooled engine.

        Why this test exists: acquire_async loads the engine (caching it at
        ref 1) and then calls the consumer's on_ready. When on_ready raised --
        the derived players' configure() rejecting Threads was the production
        trigger -- the old code went straight to on_error, leaving the process
        pooled at ref 1 forever. Nothing referenced it (the player errored out
        without storing the handle) yet evict_unused could never reap it, so
        derived-engine processes accumulated (observed as lingering worstfish/
        drawfish processes parented to the app). The registry must release the
        reference it took when on_ready fails.

        How the regression manifests: if the reference is not released, the
        handle stays at ref 1, so evict_unused() reaps nothing (returns 0) and
        the engine process is never quit.
        """
        from universalchess.services.engine_registry import get_engine_registry

        engine = MagicMock()
        engine._shutdown = False
        mock_popen.return_value = engine

        registry = get_engine_registry()

        done = threading.Event()
        errors = []

        def on_ready(_handle):
            raise RuntimeError("engine does not support option Threads")

        def on_error(exc):
            errors.append(exc)
            done.set()

        registry.acquire_async(
            "/opt/universalchess/engines/worstfish",
            on_ready=on_ready,
            on_error=on_error,
        )
        assert done.wait(timeout=5.0), "async load did not finish"
        assert len(errors) == 1

        # The reference taken by acquire must have been released back to zero, so
        # the boundary sweep can now reap the otherwise-orphaned process.
        reaped = registry.evict_unused()
        assert reaped == 1, "failed-init engine leaked (still referenced)"
        engine.quit.assert_called_once()


class TestCanonicalizePath:
    """Tests for EngineRegistry._canonicalize_path.

    Canonicalization normalizes an engine path (following symlinks) so identical
    binaries reached via different paths share one instance, and resolves bare
    names via PATH. It also passes the untrusted input through the realpath +
    startswith barrier so it is not a path-injection sink.
    """

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Reset the registry singleton between tests."""
        from universalchess.services.engine_registry import EngineRegistry
        EngineRegistry._instance = None
        yield
        if EngineRegistry._instance is not None:
            EngineRegistry._instance._engines.clear()
            EngineRegistry._instance = None

    def test_resolves_symlink_to_target(self, tmp_path):
        """A symlinked engine path canonicalizes to its real target.

        Why this test exists: dedup requires that <engines>/stockfish and its
        target /usr/games/stockfish map to the same key.
        How the regression manifests: if the symlink were not followed, the two
        paths would create two separate engine processes.
        """
        from universalchess.services.engine_registry import get_engine_registry

        target = tmp_path / "real_stockfish"
        target.write_text("binary")
        link = tmp_path / "stockfish"
        link.symlink_to(target)

        registry = get_engine_registry()
        assert registry._canonicalize_path(str(link)) == os.path.realpath(str(target))

    def test_falls_back_to_which_for_bare_name(self, tmp_path, monkeypatch):
        """A bare name that is not an existing path is resolved via PATH.

        Why this test exists: consumers may pass just "stockfish"; it must map to
        the installed binary so dedup still works.
        How the regression manifests: without the which() fallback, a bare name
        would canonicalize to a nonexistent cwd-relative path.
        """
        from universalchess.services import engine_registry
        from universalchess.services.engine_registry import get_engine_registry

        found = tmp_path / "stockfish"
        found.write_text("binary")
        monkeypatch.setattr(
            engine_registry.shutil, "which",
            lambda name: str(found) if name == "stockfish" else None,
        )

        registry = get_engine_registry()
        assert registry._canonicalize_path("stockfish") == os.path.realpath(str(found))

    def test_nonexistent_path_returns_normalized(self, tmp_path, monkeypatch):
        """A path that neither exists nor is on PATH returns its normalized form.

        Why this test exists: the caller still needs a stable dedup key even when
        the engine is missing (the launch fails later with a clear error).
        How the regression manifests: returning the raw input would skip
        normalization and could split one missing engine across keys.
        """
        from universalchess.services import engine_registry
        from universalchess.services.engine_registry import get_engine_registry

        monkeypatch.setattr(engine_registry.shutil, "which", lambda name: None)
        missing = tmp_path / "nope" / "engine"

        registry = get_engine_registry()
        assert registry._canonicalize_path(str(missing)) == os.path.realpath(str(missing))


# ---------------------------------------------------------------------------
# Load-failure classification
#
# Why this section exists
# -----------------------
# When an installed engine will not start, the only person who can see the
# exception is whoever reads the journal on that board. Remote reports arrive as
# a screenshot, so the reason must survive into the UI. Raw exception text
# cannot: it carries filesystem paths and would reintroduce the stack-trace
# exposure finding. classify_load_failure() reduces the exception to a stable,
# path-free code the UI can localize, and each code names a genuinely different
# repair (rebuild for the wrong architecture, chmod for a permission problem,
# reinstall for a crash at startup).
# ---------------------------------------------------------------------------


class TestClassifyLoadFailure:
    """Pure mapping from a launch exception to a stable, path-free reason code."""

    @pytest.mark.parametrize("exc,expected", [
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), "binary_missing"),
        (PermissionError(errno.EACCES, "Permission denied"), "not_executable"),
        (OSError(errno.ENOEXEC, "Exec format error"), "incompatible_binary"),
        (OSError(errno.EIO, "Input/output error"), "launch_failed"),
        (ValueError("something else entirely"), "launch_failed"),
    ], ids=["enoent", "eacces", "enoexec", "other-oserror", "non-oserror"])
    def test_maps_launch_exception_to_reason_code(self, exc, expected):
        """Each launch failure mode maps to the code describing its repair.

        Why this test exists: ENOEXEC is the signature of a binary built for the
        wrong architecture -- the single most useful thing to tell a user whose
        engine installed cleanly but will not run. Collapsing it into a generic
        code would leave them with no next step.

        How the regression manifests: an over-broad branch (e.g. catching OSError
        before checking errno) returns "binary_missing" or "launch_failed" for
        every case, so the enoexec/eacces parameters fail while the generic ones
        still pass.
        """
        from universalchess.services.engine_registry import classify_load_failure

        assert classify_load_failure(exc) == expected

    def test_maps_engine_terminated_to_crashed_at_startup(self):
        """An engine that dies during the handshake is reported as a crash.

        Why this test exists: python-chess raises EngineTerminatedError when the
        process exits before answering `uci`. That is distinct from never having
        started (binary_missing) and points at a broken build rather than a
        broken install path.

        How the regression manifests: without an explicit branch this falls
        through to "launch_failed", and the UI tells the user nothing more than
        that something went wrong.
        """
        import chess.engine

        from universalchess.services.engine_registry import classify_load_failure

        assert classify_load_failure(
            chess.engine.EngineTerminatedError("engine process died")
        ) == "crashed_at_startup"

    def test_maps_timeout_to_handshake_timeout(self):
        """A handshake that never completes is distinguished from a crash.

        Why this test exists: a hung engine (started, never answered `uciok`) and
        a crashed engine need different advice, and both are invisible without
        separate codes.

        How the regression manifests: a missing branch collapses this into
        "launch_failed".
        """
        from universalchess.services.engine_registry import classify_load_failure

        assert classify_load_failure(TimeoutError()) == "handshake_timeout"

    def test_reason_code_carries_no_filesystem_path(self):
        """Codes are fixed tokens, never derived from the exception's text.

        Why this test exists: these codes are returned over the API, so anything
        interpolated from the exception would leak absolute paths to the client
        -- the stack-trace exposure class of finding this design exists to avoid.

        How the regression manifests: if an implementation appended str(exc) for
        "extra detail", the path below appears in the returned code.
        """
        from universalchess.services.engine_registry import classify_load_failure

        secret_path = "/opt/universalchess/engines/ct800"
        code = classify_load_failure(OSError(errno.ENOEXEC, "Exec format error", secret_path))

        assert secret_path not in code
        assert code == "incompatible_binary"


class TestDescribeLoadFailure:
    """The short technical token shown beside the reason in the UI and event log."""

    def test_names_the_exception_and_its_errno(self):
        """An OSError is described by its class and symbolic errno.

        Why this test exists: the reason code says what to do; this token says
        what the operating system actually reported, which is what a maintainer
        needs from a screenshot. "OSError ENOEXEC" identifies an architecture
        mismatch unambiguously, while the numeric errno alone would not be
        recognisable and the class alone would not distinguish it from any other
        OS-level failure.

        How a regression manifests: the token degrades to a bare class name and
        every OSError looks alike in the report.
        """
        from universalchess.services.engine_registry import describe_load_failure

        assert describe_load_failure(
            OSError(errno.ENOEXEC, "Exec format error")
        ) == "OSError ENOEXEC"

    def test_names_the_exception_alone_when_there_is_no_errno(self):
        """A non-OS exception is described by its class name only.

        Why this test exists: python-chess raises EngineTerminatedError with no
        errno. Appending an empty or None errno would produce a ragged token like
        "EngineTerminatedError None" in the UI.

        How a regression manifests: the rendered detail carries a trailing None.
        """
        import chess.engine

        from universalchess.services.engine_registry import describe_load_failure

        assert describe_load_failure(
            chess.engine.EngineTerminatedError("engine process died")
        ) == "EngineTerminatedError"

    def test_omits_the_exception_message_and_any_path(self):
        """The token is built from types and errno, never from the message.

        Why this test exists: this string is returned by an endpoint that is not
        auth-gated, and the exception's message is where the absolute engine path
        lives. The fuller text belongs in the event log, which is auth-gated;
        this token must stay safe to publish.

        How a regression manifests: the engine path and the OS message text
        appear in the browser payload.
        """
        from universalchess.services.engine_registry import describe_load_failure

        secret_path = "/opt/universalchess/engines/ct800"
        detail = describe_load_failure(
            OSError(errno.ENOEXEC, "Exec format error", secret_path)
        )

        assert secret_path not in detail
        assert "Exec format error" not in detail


class TestSanitizeReasonCode:
    """The allowlist applied where a reason code is published to a client."""

    @pytest.mark.parametrize("code", [
        "binary_missing",
        "not_executable",
        "incompatible_binary",
        "crashed_at_startup",
        "handshake_timeout",
        "launch_failed",
    ])
    def test_passes_through_every_published_reason(self, code):
        """Each code the UI can localize survives the boundary unchanged.

        Why this test exists: the allowlist is only correct if it admits the
        whole vocabulary. One omission silently downgrades a specific,
        actionable reason to the generic one, which reads as a bug in the
        diagnosis rather than in the filter.

        How the regression manifests: dropping a constant from
        LOAD_FAILURE_REASONS fails exactly that parameter while the rest pass.
        """
        from universalchess.services.engine_registry import sanitize_reason_code

        assert sanitize_reason_code(code) == code

    @pytest.mark.parametrize("code", [
        None,
        "",
        "made_up_code",
        "/opt/universalchess/engines/ct800: Exec format error",
    ], ids=["none", "empty", "unknown-token", "exception-text"])
    def test_replaces_anything_outside_the_vocabulary(self, code):
        """Unrecognized values are reported as the generic reason, not echoed.

        Why this test exists: the reason reaches an endpoint that is not
        auth-gated, having passed through an exception attribute and a file on
        disk. If a future caller assigns str(exc) to reason_code, echoing it
        publishes the engine's absolute path. The filter is what makes that
        impossible rather than merely unlikely.

        How the regression manifests: returning the input unchanged leaks the
        path in the exception-text parameter; the others then also fail because
        the UI has no sentence for them.
        """
        from universalchess.services.engine_registry import sanitize_reason_code

        assert sanitize_reason_code(code) == "launch_failed"


class TestSanitizeDetail:
    """The character allowlist for the free-form half of a failure record."""

    @pytest.mark.parametrize("detail", [
        "OSError ENOEXEC",
        "EngineTerminatedError",
        "TimeoutError",
    ])
    def test_passes_through_tokens_describe_load_failure_produces(self, detail):
        """Everything the producer emits is publishable.

        Why this test exists: the filter and the producer must agree. A filter
        stricter than describe_load_failure silently deletes the detail from
        every report, removing the one line a maintainer reads off a screenshot.

        How the regression manifests: a pattern that forbids the space (or
        anchors wrongly) drops "OSError ENOEXEC" to None.
        """
        from universalchess.services.engine_registry import sanitize_detail

        assert sanitize_detail(detail) == detail

    @pytest.mark.parametrize("detail", [
        "/opt/universalchess/engines/ct800",
        "failed to open '/opt/universalchess/engines/ct800'",
        "https://example.test/engines.tar.gz",
        "OSError: Exec format error",
        "a" * 64 + "b",
        "",
        None,
    ], ids=["path", "message-with-path", "url", "message", "over-length", "empty", "none"])
    def test_drops_anything_that_could_carry_a_path_or_message(self, detail):
        """Values outside the token shape are dropped rather than truncated.

        Why this test exists: this is the half of the record that is not drawn
        from a fixed vocabulary, so it is the half that can leak. Truncating
        instead of dropping would still publish the leading characters of an
        absolute path, and the separators excluded here ('/', '.', ':', quotes)
        are exactly what a path, URL or exception message needs.

        How the regression manifests: a permissive pattern returns the input, so
        the path and url parameters fail; an unbounded one fails over-length.
        """
        from universalchess.services.engine_registry import sanitize_detail

        assert sanitize_detail(detail) is None


class TestAcquireOrRaise:
    """acquire_or_raise() surfaces the launch failure that acquire() swallows."""

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        from universalchess.services.engine_registry import EngineRegistry
        EngineRegistry._instance = None
        yield
        if EngineRegistry._instance is not None:
            EngineRegistry._instance._engines.clear()
            EngineRegistry._instance = None

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_raises_typed_error_carrying_the_reason_code(self, mock_popen):
        """A failed load raises EngineLoadError with the classified reason.

        Why this test exists: acquire() returns None on failure, which discards
        the one fact the UI needs. The probe path needs the reason, so it goes
        through acquire_or_raise instead.

        How the regression manifests: if the reason were not attached, the
        endpoint has nothing to report and falls back to the old, untrue
        "not installed" message.
        """
        from universalchess.services.engine_registry import (
            EngineLoadError,
            get_engine_registry,
        )

        mock_popen.side_effect = OSError(errno.ENOEXEC, "Exec format error")

        registry = get_engine_registry()
        with pytest.raises(EngineLoadError) as excinfo:
            registry.acquire_or_raise("/opt/universalchess/engines/ct800")

        assert excinfo.value.reason_code == "incompatible_binary"

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_acquire_still_returns_none_on_failure(self, mock_popen):
        """acquire() keeps its None-on-failure contract for existing callers.

        Why this test exists: players, the analysis service and the display all
        branch on a None handle. Refactoring the load path to raise must not
        change what they see.

        How the regression manifests: if acquire stopped catching, an unhandled
        EngineLoadError propagates into the board's game loop instead of the
        graceful "engine unavailable" path.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_popen.side_effect = OSError(errno.ENOEXEC, "Exec format error")

        registry = get_engine_registry()

        assert registry.acquire("/opt/universalchess/engines/ct800") is None

    @patch('universalchess.services.engine_registry.chess.engine.SimpleEngine.popen_uci')
    def test_pools_and_reuses_like_acquire_on_success(self, mock_popen):
        """A successful acquire_or_raise pools the engine exactly as acquire does.

        Why this test exists: the two entry points must share one load path. If
        acquire_or_raise spawned outside the pool, the probe would create a
        duplicate process alongside the engine a game is already using.

        How the regression manifests: popen_uci is called twice and the handles
        differ, so the board runs two copies of the same engine.
        """
        from universalchess.services.engine_registry import get_engine_registry

        mock_engine = MagicMock(); mock_engine._shutdown = False
        mock_popen.return_value = mock_engine

        registry = get_engine_registry()
        first = registry.acquire_or_raise("/usr/games/stockfish")
        second = registry.acquire("/usr/games/stockfish")

        assert first is second
        assert first.ref_count == 2
        mock_popen.assert_called_once()

