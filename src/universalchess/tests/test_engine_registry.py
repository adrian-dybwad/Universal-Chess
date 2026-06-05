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

