# Engine Registry
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Centralized registry for UCI chess engines. Each engine binary is loaded
# once and shared across all consumers (player engines, analysis, hand-brain).
# Access is serialized per engine to handle UCI's stateful nature.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import os
import pathlib
import shutil
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

import chess
import chess.engine

from universalchess.board.logging import log


@dataclass
class EngineHandle:
    """Handle to a shared engine instance.
    
    Provides serialized access to the underlying UCI engine.
    All operations acquire the lock before interacting with the engine.
    """
    path: str
    engine: chess.engine.SimpleEngine
    lock: threading.Lock = field(default_factory=threading.Lock)
    ref_count: int = 0
    
    def configure(self, options: Dict[str, str]) -> None:
        """Configure UCI options (serialized).
        
        Args:
            options: Dict of UCI option name -> value
        """
        if not options:
            return
        with self.lock:
            self.engine.configure(options)
    
    def play(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        options: Optional[Dict[str, str]] = None,
        root_moves: Optional[List[chess.Move]] = None
    ) -> chess.engine.PlayResult:
        """Compute best move (serialized).
        
        Args:
            board: Current position
            limit: Time/depth limit
            options: Optional UCI options to apply before this search
            root_moves: Optional list of moves to restrict search to
            
        Returns:
            PlayResult with best move
        """
        with self.lock:
            if options:
                self.engine.configure(options)
            return self.engine.play(board, limit, root_moves=root_moves)
    
    def analyse(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        multipv: Optional[int] = None
    ):
        """Analyze position (serialized).

        Args:
            board: Position to analyze
            limit: Time/depth limit
            multipv: Number of principal variations. Leave None for the common
                single-line case. CRITICAL: python-chess returns a single
                InfoDict only when multipv is None; passing any int (even 1)
                makes it return a List[InfoDict]. Defaulting to 1 here silently
                broke callers that index the result as a dict (e.g. analysis
                score parsing got an empty result and never updated).

        Returns:
            A single InfoDict when multipv is None, else a List[InfoDict].
        """
        with self.lock:
            return self.engine.analyse(board, limit, multipv=multipv)


class EngineRegistry:
    """Singleton registry for shared UCI engine instances.
    
    Engines are loaded lazily on first request and cached by resolved path.
    Multiple consumers can share the same engine; access is serialized.
    
    Usage:
        registry = get_engine_registry()
        handle = await registry.acquire("/path/to/stockfish")
        result = handle.play(board, chess.engine.Limit(time=1.0))
        registry.release(handle)
    """
    
    _instance: Optional[EngineRegistry] = None
    _instance_lock = threading.Lock()
    
    def __init__(self):
        self._engines: Dict[str, EngineHandle] = {}
        self._loading: Dict[str, threading.Event] = {}  # Tracks engines currently being loaded
        self._lock = threading.Lock()
    
    @classmethod
    def get_instance(cls) -> EngineRegistry:
        """Get the singleton registry instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = EngineRegistry()
        return cls._instance
    
    def _canonicalize_path(self, engine_path: str) -> str:
        """Canonicalize an engine path to ensure identical binaries share instances.
        
        This resolves symlinks and normalizes paths so that different paths
        pointing to the same binary (e.g., /usr/games/stockfish and
        /opt/universalchess/engines/stockfish if symlinked) use the same
        engine instance.
        
        Args:
            engine_path: Path to engine executable
            
        Returns:
            Canonical absolute path to the engine binary
        """
        path = pathlib.Path(engine_path)
        
        # If path exists, resolve all symlinks to get the real binary path
        if path.exists():
            return os.path.realpath(str(path))
        
        # Path doesn't exist - try to find it via PATH lookup
        # This handles cases like "stockfish" without full path
        basename = path.name
        which_path = shutil.which(basename)
        if which_path:
            return os.path.realpath(which_path)
        
        # Last resort: just resolve what we can
        return str(path.resolve())
    
    def acquire(
        self,
        engine_path: str,
        on_ready: Optional[Callable[[EngineHandle], None]] = None
    ) -> Optional[EngineHandle]:
        """Acquire a handle to an engine, loading it if necessary.
        
        This is a blocking call that may take time on first load.
        For async loading, use acquire_async().
        
        If another thread is already loading the same engine, this thread
        will wait for that load to complete rather than starting a duplicate.
        
        Args:
            engine_path: Path to engine executable
            on_ready: Optional callback when engine is ready (for async pattern)
            
        Returns:
            EngineHandle for the engine, or None on failure
        """
        resolved = self._canonicalize_path(engine_path)
        wait_event: Optional[threading.Event] = None
        should_load = False
        
        with self._lock:
            # Check if already loaded
            if resolved in self._engines:
                handle = self._engines[resolved]
                handle.ref_count += 1
                log.debug(f"[EngineRegistry] Reusing engine {resolved} (refs={handle.ref_count})")
                if on_ready:
                    on_ready(handle)
                return handle
            
            # Check if another thread is loading this engine
            if resolved in self._loading:
                log.debug(f"[EngineRegistry] Waiting for another thread to load {resolved}")
                wait_event = self._loading[resolved]
            else:
                # We're the first - mark as loading
                self._loading[resolved] = threading.Event()
                should_load = True
        
        # If another thread is loading, wait for it
        if wait_event is not None:
            wait_event.wait(timeout=60.0)  # Wait up to 60 seconds
            # Now check if it succeeded
            with self._lock:
                if resolved in self._engines:
                    handle = self._engines[resolved]
                    handle.ref_count += 1
                    log.debug(f"[EngineRegistry] Got engine from other thread {resolved} (refs={handle.ref_count})")
                    if on_ready:
                        on_ready(handle)
                    return handle
                else:
                    log.error(f"[EngineRegistry] Other thread failed to load {resolved}")
                    return None
        
        # We're responsible for loading
        log.info(f"[EngineRegistry] Loading engine: {resolved}")
        handle: Optional[EngineHandle] = None
        try:
            engine = chess.engine.SimpleEngine.popen_uci(resolved, timeout=None)
            handle = EngineHandle(path=resolved, engine=engine, ref_count=1)
            
            with self._lock:
                self._engines[resolved] = handle
                log.info(f"[EngineRegistry] Engine loaded: {resolved}")
        except Exception as e:
            log.error(f"[EngineRegistry] Failed to load engine {resolved}: {e}")
        finally:
            # Signal waiting threads
            with self._lock:
                if resolved in self._loading:
                    self._loading[resolved].set()
                    del self._loading[resolved]
        
        if handle and on_ready:
            on_ready(handle)
        return handle
    
    def acquire_async(
        self,
        engine_path: str,
        on_ready: Callable[[EngineHandle], None],
        on_error: Optional[Callable[[Exception], None]] = None
    ) -> None:
        """Acquire engine handle asynchronously in a background thread.
        
        Args:
            engine_path: Path to engine executable
            on_ready: Callback with EngineHandle when ready
            on_error: Optional callback on failure
        """
        def _load():
            try:
                handle = self.acquire(engine_path)
                if handle:
                    on_ready(handle)
                elif on_error:
                    on_error(Exception(f"Failed to load engine: {engine_path}"))
            except Exception as e:
                log.error(f"[EngineRegistry] Async load error: {e}")
                if on_error:
                    on_error(e)
        
        thread = threading.Thread(
            target=_load,
            name=f"engine-load-{pathlib.Path(engine_path).name}",
            daemon=True
        )
        thread.start()
    
    def release(self, handle: EngineHandle) -> None:
        """Release a handle to an engine.
        
        The engine is kept loaded for potential reuse by other consumers.
        Call shutdown() to actually close engines.
        
        Args:
            handle: The handle to release
        """
        with self._lock:
            if handle.path in self._engines:
                handle.ref_count = max(0, handle.ref_count - 1)
                log.debug(f"[EngineRegistry] Released {handle.path} (refs={handle.ref_count})")
    
    def shutdown(self) -> None:
        """Shutdown all engines and clear the registry.
        
        Called during application shutdown.
        """
        with self._lock:
            for path, handle in self._engines.items():
                try:
                    log.info(f"[EngineRegistry] Closing engine: {path}")
                    handle.engine.quit()
                except Exception as e:
                    log.debug(f"[EngineRegistry] Error closing {path}: {e}")
            self._engines.clear()
        log.info("[EngineRegistry] All engines shut down")
    
    def get_loaded_engines(self) -> Dict[str, int]:
        """Get dict of loaded engine paths -> ref counts (for debugging)."""
        with self._lock:
            return {path: handle.ref_count for path, handle in self._engines.items()}


def get_engine_registry() -> EngineRegistry:
    """Get the global engine registry singleton."""
    return EngineRegistry.get_instance()


__all__ = [
    "EngineHandle",
    "EngineRegistry",
    "get_engine_registry",
]

