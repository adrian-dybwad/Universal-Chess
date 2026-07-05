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

