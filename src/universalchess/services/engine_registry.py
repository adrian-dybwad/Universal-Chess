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
    """Handle to an engine instance.

    Provides serialized access to the underlying UCI engine.
    All operations acquire the lock before interacting with the engine.

    ``shared`` distinguishes a pooled instance (cached and reused across
    consumers via the registry) from a dedicated one owned by a single consumer.
    A dedicated engine is required for UCI pondering: python-chess keeps the
    ``go ponder`` search running in the background between the player's moves, and
    any command from another consumer (analysis, the opponent) on a shared
    process would interrupt it. Releasing a dedicated handle quits its engine
    process, since nothing else references it.
    """
    path: str
    engine: chess.engine.SimpleEngine
    lock: threading.Lock = field(default_factory=threading.Lock)
    ref_count: int = 0
    shared: bool = True
    
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
        root_moves: Optional[List[chess.Move]] = None,
        ponder: bool = False,
        game: object = None,
    ) -> chess.engine.PlayResult:
        """Compute best move (serialized).
        
        Args:
            board: Current position
            limit: Time/depth limit
            options: Optional UCI options to apply before this search
            root_moves: Optional list of moves to restrict search to
            ponder: When True, python-chess lets the engine keep searching on the
                opponent's move (``go ponder``) after returning the best move, and
                sends ``ponderhit`` on the next call when the position matches. Use
                only with a dedicated handle (``shared=False``): the background
                search would otherwise be clobbered by another consumer.
            game: Opaque token identifying the current game. python-chess only
                issues ``ponderhit`` when this equals the token from the previous
                ``play`` call, so a caller passes a stable per-game object and
                refreshes it on a new game.
            
        Returns:
            PlayResult with best move
        """
        with self.lock:
            if options:
                self.engine.configure(options)
            return self.engine.play(
                board, limit, root_moves=root_moves, ponder=ponder, game=game
            )
    
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
        # Dedicated (non-shared) handles are not pooled by path, so they are
        # tracked separately purely so shutdown() can quit any that a consumer
        # failed to release (e.g. an abrupt exit mid-game).
        self._dedicated: List[EngineHandle] = []
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

    def acquire_dedicated(self, engine_path: str) -> Optional[EngineHandle]:
        """Acquire a private engine instance not shared with other consumers.

        Unlike :meth:`acquire`, this always spawns a fresh engine process and does
        not cache it by path, so the returned handle is owned solely by the
        caller. This is required for UCI pondering: the engine keeps a background
        ``go ponder`` search running between the player's moves, which any command
        from another consumer on a shared instance would interrupt.

        The caller must :meth:`release` the handle when done; releasing a
        dedicated handle quits its engine process.

        Args:
            engine_path: Path to engine executable.

        Returns:
            A dedicated EngineHandle, or None on failure.
        """
        resolved = self._canonicalize_path(engine_path)
        log.info(f"[EngineRegistry] Loading dedicated engine: {resolved}")
        try:
            engine = chess.engine.SimpleEngine.popen_uci(resolved, timeout=None)
        except Exception as e:
            log.error(f"[EngineRegistry] Failed to load dedicated engine {resolved}: {e}")
            return None

        handle = EngineHandle(path=resolved, engine=engine, ref_count=1, shared=False)
        with self._lock:
            self._dedicated.append(handle)
        log.info(f"[EngineRegistry] Dedicated engine loaded: {resolved}")
        return handle

    def release(self, handle: EngineHandle) -> None:
        """Release a handle to an engine.
        
        A shared engine is kept loaded for potential reuse by other consumers;
        call shutdown() to actually close pooled engines. A dedicated handle
        (``shared=False``) is owned by this one consumer, so releasing it quits
        the engine process immediately and stops tracking it.
        
        Args:
            handle: The handle to release
        """
        if not handle.shared:
            with self._lock:
                if handle in self._dedicated:
                    self._dedicated.remove(handle)
            handle.ref_count = 0
            try:
                handle.engine.quit()
                log.info(f"[EngineRegistry] Dedicated engine quit: {handle.path}")
            except Exception as e:
                log.debug(f"[EngineRegistry] Error quitting dedicated engine {handle.path}: {e}")
            return

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
            # Quit any dedicated engines a consumer failed to release (e.g. an
            # abrupt shutdown mid-game left a pondering engine running).
            for handle in self._dedicated:
                try:
                    log.info(f"[EngineRegistry] Closing dedicated engine: {handle.path}")
                    handle.engine.quit()
                except Exception as e:
                    log.debug(f"[EngineRegistry] Error closing dedicated {handle.path}: {e}")
            self._dedicated.clear()
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

