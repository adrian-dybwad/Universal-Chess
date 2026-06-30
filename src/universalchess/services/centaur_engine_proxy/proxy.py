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
    build_config_setoptions,
    rewrite_setoption_line,
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
    inject_options: Iterable[str] = (),
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """Forward UCI between Centaur and the engine, recording the game.

    - engine output is pumped to Centaur on a background thread (so a long search
      does not block reading Centaur's input);
    - the configured options are injected once, immediately before the first
      ``go`` (valid UCI: after Centaur's own setoptions, so the clamped/config
      values win and the memory floor holds);
    - each ``position`` is parsed and folded into the tracker/recorder; a
      recording failure is logged and swallowed so it never breaks play;
    - Centaur's ``setoption`` lines are rewritten to the memory floor before
      forwarding; everything else passes through verbatim.

    Returns when Centaur closes its input; the engine's stdin is then closed so
    it exits, and the pump thread is drained.
    """
    inject_options = list(inject_options)

    def pump_engine() -> None:
        for line in engine_out:
            _write_line(centaur_out, line.rstrip("\n"))

    pump = threading.Thread(target=pump_engine, name="centaur-proxy-engine-pump", daemon=True)
    pump.start()

    injected = False
    for raw in centaur_in:
        line = raw.rstrip("\n")
        stripped = line.strip()
        lowered = stripped.lower()

        if not injected and lowered.startswith("go"):
            for option_line in inject_options:
                _write_line(engine_in, option_line)
            injected = True

        if lowered.startswith("position") and tracker is not None:
            parsed = parse_position_command(stripped)
            if parsed is not None:
                try:
                    update = tracker.update(*parsed)
                    if recorder is not None:
                        recorder.apply(update)
                except Exception as exc:  # noqa: BLE001 - recording must never break play
                    if log_fn:
                        log_fn(f"centaur-proxy: recording error: {exc}")

        out_line = rewrite_setoption_line(line) if lowered.startswith("setoption") else line
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
    import subprocess  # nosec B404 - launches the resolved UC engine path only

    from universalchess.board.settings import Settings
    from universalchess.services.centaur_engine_proxy.config import load_proxy_config

    def log_fn(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    config = load_proxy_config(Settings.read)
    engine_cmd = _resolve_engine_command(config)
    if engine_cmd is None:
        log_fn(f"centaur-proxy: no engine available (configured '{config.engine_name}')")
        return 1

    proc = subprocess.Popen(  # noqa: S603 - engine_cmd is a resolved trusted path  # nosec B603
        engine_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    tracker = PositionTracker()
    recorder = _build_recorder(log_fn)
    inject_options = build_config_setoptions(config.options)

    try:
        run_proxy(
            sys.stdin,
            sys.stdout,
            proc.stdin,
            proc.stdout,
            tracker=tracker,
            recorder=recorder,
            inject_options=inject_options,
            log_fn=log_fn,
        )
    finally:
        proc.wait()
    return proc.returncode or 0


def _build_recorder(log_fn):
    """Build a DB-backed recorder, or None if the DB cannot be opened.

    Recording is best-effort: a DB problem must not stop Centaur from playing, so
    a failure here yields None and the proxy forwards UCI without recording.
    """
    try:
        from sqlalchemy.orm import Session

        from universalchess.db import models
        from universalchess.services.centaur_engine_proxy.recorder import GameRecorder

        session = Session(bind=models.engine)
        return GameRecorder(session, source="centaur")
    except Exception as exc:  # noqa: BLE001 - recording is optional
        log_fn(f"centaur-proxy: recording disabled ({exc})")
        return None
