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

import errno
import os
import pathlib
import re
import shutil
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

import chess
import chess.engine

from universalchess.board.logging import log

# Stable reason codes for a failed engine launch. Each names a different repair,
# which is the point: an engine that installed cleanly and then refuses to start
# leaves the user with nothing to act on unless the failure mode is named.
# Values are fixed tokens, never derived from exception text -- they are returned
# by an endpoint that is not auth-gated, and an exception's message carries the
# absolute path of the engine binary.
LOAD_FAILURE_BINARY_MISSING = "binary_missing"
LOAD_FAILURE_NOT_EXECUTABLE = "not_executable"
LOAD_FAILURE_INCOMPATIBLE_BINARY = "incompatible_binary"
LOAD_FAILURE_CRASHED_AT_STARTUP = "crashed_at_startup"
LOAD_FAILURE_HANDSHAKE_TIMEOUT = "handshake_timeout"
LOAD_FAILURE_UNKNOWN = "launch_failed"

# The complete published vocabulary. Anything outside it is not a reason code,
# and :func:`sanitize_reason_code` refuses to emit it.
LOAD_FAILURE_REASONS = frozenset({
    LOAD_FAILURE_BINARY_MISSING,
    LOAD_FAILURE_NOT_EXECUTABLE,
    LOAD_FAILURE_INCOMPATIBLE_BINARY,
    LOAD_FAILURE_CRASHED_AT_STARTUP,
    LOAD_FAILURE_HANDSHAKE_TIMEOUT,
    LOAD_FAILURE_UNKNOWN,
})

# A publishable detail token: a leading letter, then letters, digits, spaces and
# underscores only, bounded in length. Deliberately excludes '/', '.', ':' and
# quotes, so no filesystem path, URL or exception message can satisfy it.
_SAFE_DETAIL = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,63}$")

# errno -> reason code. ENOEXEC is the signature of a binary built for another
# architecture, the one failure whose repair (rebuild) is invisible otherwise.
_ERRNO_REASONS = {
    errno.ENOENT: LOAD_FAILURE_BINARY_MISSING,
    errno.EACCES: LOAD_FAILURE_NOT_EXECUTABLE,
    errno.EPERM: LOAD_FAILURE_NOT_EXECUTABLE,
    errno.ENOEXEC: LOAD_FAILURE_INCOMPATIBLE_BINARY,
}


class EngineLoadError(Exception):
    """An engine binary could not be launched or did not complete the handshake.

    Carries a classified :attr:`reason_code` and a short, path-free
    :attr:`detail` token so the failure can be reported to the user rather than
    existing only in the journal of the board it happened on.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = LOAD_FAILURE_UNKNOWN,
        detail: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


def classify_load_failure(exc: BaseException) -> str:
    """Reduce a launch exception to one of the fixed load-failure reason codes.

    Pure. Matches on exception type and ``errno`` only; nothing from the
    exception's message reaches the result, because the caller publishes it.
    """
    if isinstance(exc, chess.engine.EngineTerminatedError):
        return LOAD_FAILURE_CRASHED_AT_STARTUP
    if isinstance(exc, TimeoutError):
        return LOAD_FAILURE_HANDSHAKE_TIMEOUT
    if isinstance(exc, OSError):
        return _ERRNO_REASONS.get(exc.errno, LOAD_FAILURE_UNKNOWN)
    return LOAD_FAILURE_UNKNOWN


def describe_load_failure(exc: BaseException) -> str:
    """Return a short technical token naming the exception and its errno.

    Pure. ``"OSError ENOEXEC"`` tells a maintainer exactly what the operating
    system reported while remaining safe to publish: the exception's message,
    which contains the engine's absolute path, is deliberately excluded. The
    fuller text goes to the auth-gated event log instead.
    """
    name = type(exc).__name__
    code = getattr(exc, "errno", None)
    symbol = errno.errorcode.get(code) if code is not None else None
    return f"{name} {symbol}" if symbol else name


def sanitize_reason_code(reason_code: Optional[str]) -> str:
    """Reduce ``reason_code`` to a member of :data:`LOAD_FAILURE_REASONS`.

    Pure. The reason travels from an exception attribute, through a persisted
    record, to a response served without authentication, so the value is checked
    against the vocabulary at the point it is published rather than trusted
    because every producer is currently careful. An unrecognized code is
    reported as :data:`LOAD_FAILURE_UNKNOWN`: the UI has no localized sentence
    for it, and echoing it would publish whatever a future caller assigned --
    the exception's message, and with it the engine's absolute path.
    """
    if reason_code in LOAD_FAILURE_REASONS:
        return reason_code
    return LOAD_FAILURE_UNKNOWN


def sanitize_detail(detail: Optional[str]) -> Optional[str]:
    """Return ``detail`` if it is a publishable token, otherwise None.

    Pure. Companion to :func:`sanitize_reason_code` for the free-form half of a
    failure record. :func:`describe_load_failure` already produces only
    ``"OSError ENOEXEC"``-shaped text, but the detail reaches the same
    unauthenticated response from a record on disk, so it is re-checked against
    a character allowlist here. Dropping it is the correct failure mode: the
    reason code still explains the failure, while a path or exception message
    admitted "just this once" cannot be taken back.
    """
    if detail and _SAFE_DETAIL.match(detail):
        return detail
    return None


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
    
    def _supported_options(self, options: Dict[str, str]) -> Dict[str, str]:
        """Keep only options this engine advertised in its UCI handshake.

        python-chess raises ``EngineError`` for any option a UCI engine did not
        advertise (``self.engine.options`` is the case-insensitive map of the
        engine's own ``option`` lines). The app applies one shared profile to
        every engine -- the Default section carries ``Threads=1`` -- but limited
        engines advertise far fewer options: the derived policy engines
        (Worstfish/Drawfish) expose only ``Randomness``/``AvoidCaptures``.
        Forwarding ``Threads`` to them aborted initialization before they could
        move. Filtering here, at the single boundary to python-chess, keeps a
        generic profile compatible with every engine and covers every caller
        (players, hand/brain, analysis) without each re-implementing the check.

        Unknown options are dropped (with a log line) rather than raising: an
        option a given engine does not understand is not an error for the app,
        it simply does not apply to that engine.
        """
        advertised = self.engine.options
        supported = {name: value for name, value in options.items() if name in advertised}
        dropped = [name for name in options if name not in advertised]
        if dropped:
            log.info(
                f"[EngineHandle] {self.path}: ignoring options not advertised "
                f"by engine: {dropped}"
            )
        return supported

    def full_strength_options(self) -> Dict[str, object]:
        """Return the options that clear any playing-strength limit on this engine.

        A pooled engine is shared by every consumer of the same binary, and a
        player engine configures it down to its ELO profile
        (``UCI_LimitStrength``/``UCI_Elo``/``Skill Level``). UCI options persist
        for the life of the process, so an objective consumer -- position
        analysis, the ``?`` hint -- must clear them before its own search or it
        silently reports the opponent's weakened evaluation.

        Values are read from the engine's own advertised metadata rather than
        hardcoded: ``Skill Level`` tops out at 20 on Stockfish but not
        everywhere, and python-chess rejects an out-of-range spin value, which
        would turn a merely-weak search into a failed one. A spin option that
        declares no maximum is skipped rather than given an invented ceiling.

        Returns an empty dict for engines that advertise no strength limits
        (the derived policy engines expose only Randomness/AvoidCaptures), which
        is also what keeps this safe to call unconditionally.
        """
        advertised = self.engine.options
        options: Dict[str, object] = {}

        limit_strength = advertised.get("UCI_LimitStrength")
        if limit_strength is not None:
            options["UCI_LimitStrength"] = False

        for name in ("UCI_Elo", "Skill Level"):
            option = advertised.get(name)
            if option is not None and getattr(option, "max", None) is not None:
                options[name] = option.max

        return options

    def configure(self, options: Dict[str, str]) -> None:
        """Configure UCI options (serialized).
        
        Options the engine did not advertise are ignored (see
        ``_supported_options``) so a shared profile never fails a limited engine.
        
        Args:
            options: Dict of UCI option name -> value
        """
        if not options:
            return
        with self.lock:
            supported = self._supported_options(options)
            if supported:
                self.engine.configure(supported)
    
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
                supported = self._supported_options(options)
                if supported:
                    self.engine.configure(supported)
            return self.engine.play(
                board, limit, root_moves=root_moves, ponder=ponder, game=game
            )
    
    def analyse(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        multipv: Optional[int] = None,
        options: Optional[Dict[str, object]] = None,
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
            options: Optional UCI options to apply before this search, mirroring
                ``play``. Analysis callers pass ``full_strength_options()`` so a
                pooled engine left weakened by a reduced-ELO player does not
                produce the objective evaluation. Applied under the same lock as
                the search, so a concurrent consumer cannot interleave its own
                options between the configure and the analyse.

        Returns:
            A single InfoDict when multipv is None, else a List[InfoDict].
        """
        with self.lock:
            if options:
                supported = self._supported_options(options)
                if supported:
                    self.engine.configure(supported)
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
    
    @staticmethod
    def _is_engine_alive(engine: chess.engine.SimpleEngine) -> bool:
        """Return whether a pooled engine's UCI event loop is still running.

        python-chess flips ``SimpleEngine._shutdown`` to True the moment its
        background event loop (and thus the engine subprocess) terminates -- for
        example when the OOM-killer reaps Stockfish under memory pressure. From
        that point every call raises ``EngineTerminatedError("engine event loop
        dead")``, so a cached handle in that state is a corpse that must never be
        handed out again.

        The check is an identity test against ``True`` (not truthiness) and
        defaults to alive when the attribute is absent: a real engine sets a
        plain bool, so this correctly detects death, while a partially built or
        test-double engine without the flag is treated as alive rather than
        being falsely evicted.
        """
        return getattr(engine, "_shutdown", False) is not True

    @staticmethod
    def _quit_engine_quietly(engine: chess.engine.SimpleEngine) -> None:
        """Quit an engine subprocess, swallowing errors.

        Used when reaping a handle the registry is discarding (a dead cached
        engine on reload, or an unused pooled engine on eviction). Quitting an
        already-dead engine raises, and a live quit can still fail on a wedged
        process; either way the registry has already dropped its reference, so a
        failure here must not propagate.
        """
        try:
            engine.quit()
        except Exception as e:
            log.debug(f"[EngineRegistry] Error quitting engine: {e}")

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
        # engine_path may be request-derived (a configured/custom engine path), so
        # it is normalized with os.path.realpath before any filesystem access. The
        # startswith(os.sep) check is the CodeQL-recognized SafeAccessCheck that
        # clears the path-injection taint state after realpath normalization
        # (py/path-injection): realpath always yields an absolute path, so the
        # branch never triggers in practice, but it makes the guard explicit and
        # rejects any non-absolute result before it reaches the filesystem.
        normalized = os.path.realpath(engine_path)
        if not normalized.startswith(os.sep):
            return normalized

        # If the normalized path exists, it is the real binary (symlinks already
        # followed by realpath), which is the dedup key we want.
        if os.path.exists(normalized):
            return normalized

        # Path doesn't exist as given - try to find it via PATH lookup. This
        # handles a bare name like "stockfish" without a full path.
        which_path = shutil.which(os.path.basename(normalized))
        if which_path:
            return os.path.realpath(which_path)

        # Last resort: the normalized (non-existent) path is still a stable key.
        return normalized
    
    def acquire(
        self,
        engine_path: str,
        on_ready: Optional[Callable[[EngineHandle], None]] = None
    ) -> Optional[EngineHandle]:
        """Acquire a handle to an engine, loading it if necessary.

        Thin wrapper over :meth:`acquire_or_raise` preserving the None-on-failure
        contract every consumer branches on (players, analysis, the display).
        Callers that need to report *why* a load failed -- the UCI option probe,
        whose failure the user has to see -- use :meth:`acquire_or_raise`.

        Args:
            engine_path: Path to engine executable
            on_ready: Optional callback when engine is ready (for async pattern)

        Returns:
            EngineHandle for the engine, or None on failure
        """
        try:
            return self.acquire_or_raise(engine_path, on_ready=on_ready)
        except EngineLoadError:
            return None

    def acquire_or_raise(
        self,
        engine_path: str,
        on_ready: Optional[Callable[[EngineHandle], None]] = None
    ) -> EngineHandle:
        """Acquire a handle to an engine, raising with the reason on failure.

        This is a blocking call that may take time on first load.
        For async loading, use acquire_async().

        If another thread is already loading the same engine, this thread
        will wait for that load to complete rather than starting a duplicate.

        Args:
            engine_path: Path to engine executable
            on_ready: Optional callback when engine is ready (for async pattern)

        Returns:
            EngineHandle for the engine.

        Raises:
            EngineLoadError: The engine could not be launched, carrying a
                classified reason code and a path-free detail token.
        """
        resolved = self._canonicalize_path(engine_path)
        wait_event: Optional[threading.Event] = None
        should_load = False
        dead_engine: Optional[chess.engine.SimpleEngine] = None
        
        with self._lock:
            # Check if already loaded. A cached handle is only reused when its
            # engine is still alive; a dead one (subprocess killed, e.g. OOM) is
            # evicted here and reloaded below. The ref_count is incremented only
            # after the liveness check so a dead handle never leaks references
            # (the pre-fix code bumped the count before discovering the engine was
            # dead, so repeated failed reuse climbed the count without bound).
            handle = self._engines.get(resolved)
            if handle is not None:
                if self._is_engine_alive(handle.engine):
                    handle.ref_count += 1
                    log.debug(f"[EngineRegistry] Reusing engine {resolved} (refs={handle.ref_count})")
                    if on_ready:
                        on_ready(handle)
                    return handle
                # Dead cached engine: drop it from the pool and reap it outside
                # the lock, then fall through to load a fresh instance.
                log.warning(
                    f"[EngineRegistry] Cached engine {resolved} is dead "
                    f"(event loop terminated) - evicting and reloading"
                )
                del self._engines[resolved]
                dead_engine = handle.engine
                handle.ref_count = 0
            
            # Check if another thread is loading this engine
            if resolved in self._loading:
                log.debug(f"[EngineRegistry] Waiting for another thread to load {resolved}")
                wait_event = self._loading[resolved]
            else:
                # We're the first - mark as loading
                self._loading[resolved] = threading.Event()
                should_load = True
        
        # Reap the evicted dead engine's process outside the lock (best-effort).
        if dead_engine is not None:
            self._quit_engine_quietly(dead_engine)
        
        # If another thread is loading, wait for it
        if wait_event is not None:
            wait_event.wait(timeout=60.0)  # Wait up to 60 seconds
            # Now check if it succeeded
            with self._lock:
                handle = self._engines.get(resolved)
                if handle is not None and self._is_engine_alive(handle.engine):
                    handle.ref_count += 1
                    log.debug(f"[EngineRegistry] Got engine from other thread {resolved} (refs={handle.ref_count})")
                    if on_ready:
                        on_ready(handle)
                    return handle
                else:
                    log.error(f"[EngineRegistry] Other thread failed to load {resolved}")
                    raise EngineLoadError(
                        f"engine load failed in another thread: {resolved}"
                    )
        
        # We're responsible for loading
        log.info(f"[EngineRegistry] Loading engine: {resolved}")
        handle: Optional[EngineHandle] = None
        load_error: Optional[EngineLoadError] = None
        try:
            engine = chess.engine.SimpleEngine.popen_uci(resolved, timeout=None)
            handle = EngineHandle(path=resolved, engine=engine, ref_count=1)
            
            with self._lock:
                self._engines[resolved] = handle
                log.info(f"[EngineRegistry] Engine loaded: {resolved}")
        except Exception as e:
            log.error(f"[EngineRegistry] Failed to load engine {resolved}: {e}")
            load_error = EngineLoadError(
                f"could not launch engine at {resolved}",
                reason_code=classify_load_failure(e),
                detail=describe_load_failure(e),
            )
        finally:
            # Signal waiting threads
            with self._lock:
                if resolved in self._loading:
                    self._loading[resolved].set()
                    del self._loading[resolved]

        if load_error is not None:
            raise load_error
        if on_ready:
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
            handle: Optional[EngineHandle] = None
            try:
                handle = self.acquire(engine_path)
                if handle:
                    on_ready(handle)
                elif on_error:
                    on_error(Exception(f"Failed to load engine: {engine_path}"))
            except Exception as e:
                log.error(f"[EngineRegistry] Async load error: {e}")
                # acquire() already took a reference and pooled the engine; if
                # on_ready raised (e.g. the consumer's configure rejected an
                # option) that reference is otherwise orphaned -- the consumer
                # errored without storing the handle, so it never releases it and
                # the process lingers pooled at ref>=1, immune to evict_unused.
                # Release it here so the load failure leaves no leaked engine
                # process (the derived-engine startup leak on dgt-64).
                if handle is not None:
                    self.release(handle)
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
    
    def evict_unused(self) -> int:
        """Quit and remove pooled engines that no consumer still references.

        Called at a game-teardown / engine-switch boundary so an engine used only
        by the game that just ended -- e.g. Ethereal after the players switch
        back to Stockfish -- is unloaded instead of lingering as an idle process
        consuming memory. Only shared, pooled engines with ``ref_count <= 0`` are
        reaped; an engine still held by a player or the analysis service is left
        running. Dedicated (ponder) handles are owned by their consumer and
        released explicitly, so they are never in the pool and are untouched here.

        Release intentionally does not quit at ref zero -- transient consumers
        (per-move coach MultiPV, UCI option probing) acquire and release a shared
        engine many times during a single game, and quitting on each release
        would thrash the process. Reaping is deferred to this explicit boundary
        instead.

        Returns:
            The number of engines evicted (for logging and tests).
        """
        with self._lock:
            unused_paths = [
                path for path, handle in self._engines.items()
                if handle.ref_count <= 0
            ]
            reaped = [(path, self._engines.pop(path).engine) for path in unused_paths]
        
        for path, engine in reaped:
            self._quit_engine_quietly(engine)
            log.info(f"[EngineRegistry] Evicted unused engine: {path}")
        return len(reaped)

    def evict_if_unused(self, engine_path: str) -> bool:
        """Quit one named pooled engine if no consumer still references it.

        The targeted counterpart to :meth:`evict_unused`, for consumers that must
        clean up after themselves rather than wait for a teardown boundary. The
        UCI option probe is the case that requires it: the web process never
        reaches a game teardown and so never calls ``evict_unused``, which left
        one idle engine process resident per distinct engine the profile editor
        had ever opened, for the lifetime of the service.

        Deliberately narrower than a full sweep -- reaping every ref-zero engine
        here would unload an engine another consumer left pooled for reuse, which
        is not the probe's business. A still-referenced engine (a game is playing
        with it) and a dedicated pondering handle are both left alone, and an
        unpooled path is a quiet no-op, since this is called from a ``finally``
        that also runs when the load failed.

        Args:
            engine_path: Path to the engine executable, in any form the registry
                canonicalizes.

        Returns:
            True if an engine was quit and dropped from the pool.
        """
        resolved = self._canonicalize_path(engine_path)
        with self._lock:
            handle = self._engines.get(resolved)
            if handle is None or handle.ref_count > 0:
                return False
            del self._engines[resolved]

        self._quit_engine_quietly(handle.engine)
        log.info(f"[EngineRegistry] Evicted unused engine: {resolved}")
        return True
    
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
    "LOAD_FAILURE_REASONS",
    "EngineHandle",
    "EngineLoadError",
    "EngineRegistry",
    "classify_load_failure",
    "describe_load_failure",
    "get_engine_registry",
    "sanitize_detail",
    "sanitize_reason_code",
]

