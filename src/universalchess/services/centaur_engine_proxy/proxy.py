"""The Centaur UCI proxy: sit between Centaur and any UC engine.

Centaur execs ``engines/stockfish_pi`` and speaks UCI to it. This proxy takes
that path: it forwards UCI both ways to a configured UC engine, enforces the
memory-safety floor on Hash/MultiPV, injects the user's configured options, and
reconstructs+records the game from Centaur's ``position`` stream -- so Centaur
can use any engine with no modified binary and its games land in UC's database.

``run_proxy`` is stream-oriented (Centaur in/out and engine in/out are injected)
so it can be unit-tested with in-memory streams and a fake engine; ``main`` wires
it to real stdio and a real engine subprocess on the board.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Iterable, Optional

from universalchess.services.centaur_engine_proxy.options import (
    CENTAUR_FACE_OPTION_LINES,
    allows_setoption,
    build_config_setoptions,
    ensure_info_multipv,
    info_line_has_pv,
    is_uci_engine_output_line,
    parse_advertised_option_name,
    parse_bestmove,
    rewrite_setoption_line,
    synthetic_info_for_move,
)
from universalchess.services.centaur_engine_proxy.tracker import (
    PositionTracker,
    parse_position_command,
)


def _write_line(stream, line: str) -> None:
    stream.write(line if line.endswith("\n") else line + "\n")
    stream.flush()


def run_proxy(
    centaur_in: Iterable[str],
    centaur_out,
    engine_in,
    engine_out: Iterable[str],
    *,
    tracker: Optional[PositionTracker] = None,
    recorder=None,
    publisher=None,
    inject_options: Iterable[str] = (),
    log_fn: Optional[Callable[[str], None]] = None,
    debug: bool = False,
) -> None:
    """Forward UCI between Centaur and the engine, recording the game.

    - engine output is pumped to Centaur on a background thread (so a long search
      does not block reading Centaur's input);
    - the configured options are injected once, immediately before the first
      ``go`` (valid UCI: after Centaur's own setoptions, so the clamped/config
      values win and the memory floor holds);
    - each ``position`` is parsed and folded into the tracker/recorder, then the
      reconstructed board is mirrored to the web via ``publisher`` (fen.log +
      broadcast); a recording or publish failure is logged and swallowed so it
      never breaks play;
    - Centaur's ``setoption`` lines are rewritten to the memory floor before
      forwarding; names the engine did not advertise in its ``uci`` handshake
      are dropped (Centaur always sends Stockfish's options; other engines exit
      on them). Engine stdout that is not UCI (banners, "No such option") is
      not forwarded to Centaur. The real engine's ``option`` advertisements are
      replaced with a Stockfish-shaped Hash/MultiPV/Skill Level list so
      Centaur's python-chess 0.x handshake succeeds; ``info`` lines without
      ``multipv`` get it inserted, and a bare ``bestmove`` is preceded by a
      dummy MultiPV ``info`` so ``magic_choose`` has a candidate. Everything
      else passes through verbatim.

    The engine's ``bestmove`` is also watched: when it ends the game (the player
    is about to make the mating/stalemating move and Centaur will send no further
    ``position``), that final move is committed to the tracker/recorder/publisher
    so it is recorded and shown -- otherwise the last move of a Centaur-won game
    is lost. Mid-game bestmoves are ignored (they re-arrive via the next
    ``position``). Engine output is read on the pump thread while ``position`` is
    read on the main thread, so a lock serializes all tracker/recorder/publisher
    access between them.

    When ``debug`` is set, every ``position`` command and the resulting tracker
    classification (new-game / appended / removed moves / total / FEN) is logged
    via ``log_fn``. It is off by default (it is per-move noise) and exists to
    validate Centaur's position-stream forms -- e.g. takebacks -- against the
    tracker without redeploying; enable it with ``UC_CENTAUR_PROXY_DEBUG`` (see
    ``main``).

    Returns when Centaur closes its input; the engine's stdin is then closed so
    it exits, and the pump thread is drained.
    """
    inject_options = list(inject_options)
    state_lock = threading.Lock()
    advertised_names: set = set()
    advertised_lock = threading.Lock()
    uciok_seen = threading.Event()
    # Search-info tracking: Centaur's magic_choose needs at least one MultiPV
    # candidate. Set when a PV info line is forwarded; if still false at
    # bestmove, a dummy info line is synthesized. Cleared after each bestmove
    # so the next search starts clean (not on ``go``, which races the pump).
    search_lock = threading.Lock()
    saw_pv_info = False

    def commit_update(update, source_desc: str) -> None:
        """Fan one tracker update out to the recorder, publisher, and debug log.

        Shared by the ``position`` path and the terminal-``bestmove`` path so both
        record, mirror to the web, and trace identically. Callers hold
        ``state_lock``; a recording/publish failure is logged and swallowed so it
        never breaks play.
        """
        try:
            if recorder is not None:
                recorder.apply(update)
            if publisher is not None:
                publisher.publish(tracker.board)
            if debug and log_fn and tracker.board is not None:
                log_fn(
                    f"centaur-proxy[debug] {source_desc} -> "
                    f"newgame={update.is_new_game} "
                    f"added={[m for m, _ in update.moves_added]} "
                    f"removed={update.moves_removed} "
                    f"total={update.total_moves} fen={tracker.board.fen()}"
                )
        except Exception as exc:  # noqa: BLE001 - recording must never break play
            if log_fn:
                log_fn(f"centaur-proxy: recording error: {exc}")

    def pump_engine() -> None:
        nonlocal saw_pv_info
        for line in engine_out:
            text = line.rstrip("\n")
            option_name = parse_advertised_option_name(text)
            if option_name is not None:
                with advertised_lock:
                    advertised_names.add(option_name.lower())
                # Do not forward foreign option lists: Centaur's python-chess
                # 0.x handshake was written against Stockfish and aborts or
                # never starts search on combo/string/button names.
                continue
            if text.strip().lower() == "uciok":
                uciok_seen.set()
                for face_line in CENTAUR_FACE_OPTION_LINES:
                    _write_line(centaur_out, face_line)
                _write_line(centaur_out, text)
                continue
            if not is_uci_engine_output_line(text):
                continue
            lowered_out = text.strip().lower()
            if lowered_out.startswith("info"):
                text = ensure_info_multipv(text)
                if info_line_has_pv(text):
                    with search_lock:
                        saw_pv_info = True
            elif lowered_out.startswith("bestmove"):
                with search_lock:
                    need_synthetic = not saw_pv_info
                    saw_pv_info = False
                if need_synthetic:
                    move = parse_bestmove(text)
                    if move is not None:
                        _write_line(centaur_out, synthetic_info_for_move(move))
            _write_line(centaur_out, text)
            if tracker is None:
                continue
            if lowered_out.startswith("bestmove"):
                move = parse_bestmove(text)
                # "bestmove <uci> [ponder <uci>]"; "(none)"/"0000" mean no move.
                if move is not None:
                    with state_lock:
                        update = tracker.apply_terminal_engine_move(move)
                        if update is not None:
                            commit_update(update, f"bestmove {move} (terminal)")

    pump = threading.Thread(target=pump_engine, name="centaur-proxy-engine-pump", daemon=True)
    pump.start()

    injected = False
    for raw in centaur_in:
        line = raw.rstrip("\n")
        stripped = line.strip()
        lowered = stripped.lower()

        if not injected and lowered.startswith("go"):
            uciok_seen.wait(timeout=5.0)
            with advertised_lock:
                names = set(advertised_names)
            for option_line in inject_options:
                if allows_setoption(names, option_line):
                    _write_line(engine_in, option_line)
            injected = True

        if lowered.startswith("ucinewgame") and tracker is not None:
            # Explicit new-game delimiter: the next position starts a fresh game
            # rather than being read as a takeback to the opening of this one.
            with state_lock:
                tracker.mark_new_game()

        if lowered.startswith("position") and tracker is not None:
            parsed = parse_position_command(stripped)
            if parsed is not None:
                with state_lock:
                    try:
                        update = tracker.update(*parsed)
                    except Exception as exc:  # noqa: BLE001 - must never break play
                        update = None
                        if log_fn:
                            log_fn(f"centaur-proxy: recording error: {exc}")
                    if update is not None:
                        commit_update(update, stripped)

        if lowered.startswith("setoption"):
            uciok_seen.wait(timeout=5.0)
            with advertised_lock:
                names = set(advertised_names)
            if not allows_setoption(names, line):
                continue
            out_line = rewrite_setoption_line(line)
        else:
            out_line = line
        _write_line(engine_in, out_line)

    try:
        engine_in.close()
    except Exception:  # noqa: BLE001,S110 - best-effort close on shutdown  # nosec B110
        pass
    pump.join(timeout=5)


def _resolve_engine_command(config) -> Optional[list]:
    """Resolve the configured UC engine executable to run.

    The whole point of the hook is that Centaur plays a UC engine, and UC always
    ships Stockfish, so the configured engine is expected to resolve. There is
    deliberately no fallback to the SD's original ``stockfish_pi``: keeping a
    second engine just for a case that should not happen means carrying a stale
    binary and a code path that is exercised only when something is already
    misconfigured. If the configured engine cannot be resolved, return None so
    the caller fails loudly with the engine name -- a clear, actionable error is
    better than silently playing a different (old) engine.
    """
    from universalchess.paths import get_engine_path

    engine_path = get_engine_path(config.engine_name)
    if engine_path:
        return [engine_path]
    return None


def main(argv: Optional[list] = None) -> int:
    """Entry point invoked at the ``engines/stockfish_pi`` path Centaur execs.

    Loads the proxy config, starts the engine subprocess, and runs the proxy
    against real stdio. Recording uses a fresh DB session bound to UC's database;
    if it cannot be set up, the proxy still forwards UCI (play is never blocked by
    a recording problem).
    """
    import os
    import subprocess  # nosec B404 - launches the resolved UC engine path only

    from universalchess.board.settings import Settings
    from universalchess.services.centaur_engine_proxy.config import load_proxy_config

    def log_fn(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    # Opt-in per-move position-stream trace (off by default; see run_proxy). Set
    # UC_CENTAUR_PROXY_DEBUG=1 in Centaur's launch env to capture the stream for
    # validating tracker behavior (e.g. takebacks) without code changes.
    debug = os.environ.get("UC_CENTAUR_PROXY_DEBUG", "").strip().lower() in (
        "1", "true", "on", "yes",
    )

    config = load_proxy_config(Settings.read)
    engine_cmd = _resolve_engine_command(config)
    if engine_cmd is None:
        log_fn(f"centaur-proxy: no engine available (configured '{config.engine_name}')")
        return 1

    proc = subprocess.Popen(  # noqa: S603 - engine_cmd is a resolved trusted path  # nosec B603
        engine_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _drain_engine_stderr() -> None:
        if proc.stderr is None:
            return
        for err_line in proc.stderr:
            log_fn(f"centaur-proxy[engine]: {err_line.rstrip()}")

    threading.Thread(
        target=_drain_engine_stderr, name="centaur-proxy-engine-stderr", daemon=True
    ).start()

    tracker = PositionTracker()
    recorder = _build_recorder(log_fn)
    publisher = _build_publisher(log_fn)
    inject_options = build_config_setoptions(config.options)

    try:
        run_proxy(
            sys.stdin,
            sys.stdout,
            proc.stdin,
            proc.stdout,
            tracker=tracker,
            recorder=recorder,
            publisher=publisher,
            inject_options=inject_options,
            log_fn=log_fn,
            debug=debug,
        )
    finally:
        proc.wait()
    return proc.returncode or 0


def _build_recorder(log_fn):
    """Build a DB-backed recorder, or None if the DB cannot be opened.

    Recording is best-effort: a DB problem must not stop Centaur from playing, so
    a failure here yields None and the proxy forwards UCI without recording.

    The session uses a dedicated engine rather than the shared ``models.engine``
    because the proxy records from two threads -- Centaur's position stream (main
    thread) and the engine's terminal bestmove (pump thread), serialized by
    run_proxy's state_lock. A SQLite connection is thread-affine
    (``check_same_thread``), so a single connection reused across those threads
    (StaticPool + check_same_thread=False) is what lets the game-ending move be
    recorded from the pump thread. Access is lock-serialized, never concurrent.
    The flags apply only to SQLite; any other configured backend is left as-is.
    """
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        from universalchess.db.uri import get_database_uri
        from universalchess.services.centaur_engine_proxy.recorder import GameRecorder

        uri = get_database_uri()
        engine_kwargs = {}
        if uri.startswith("sqlite"):
            engine_kwargs = {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            }
        engine = create_engine(uri, **engine_kwargs)
        session = Session(bind=engine)
        return GameRecorder(session, source="centaur")
    except Exception as exc:  # noqa: BLE001 - recording is optional
        log_fn(f"centaur-proxy: recording disabled ({exc})")
        return None


def _build_publisher(log_fn):
    """Build the web-state publisher, or None if its sinks cannot be wired.

    Mirrors recording: pushing live state to the web is best-effort, so a setup
    problem yields None and the proxy forwards UCI without web updates. The two
    sinks (fen.log writer and the broadcast socket function) are UC's own, the
    same ones ChessGameService uses for normal play.
    """
    try:
        from universalchess.paths import write_fen_log
        from universalchess.services.centaur_engine_proxy.web_publisher import (
            CentaurStatePublisher,
        )
        from universalchess.services.game_broadcast import broadcast_game_state

        return CentaurStatePublisher(write_fen_log, broadcast_game_state, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001 - web mirroring is optional
        log_fn(f"centaur-proxy: web state disabled ({exc})")
        return None
