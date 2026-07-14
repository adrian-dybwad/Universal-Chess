# Derived-engine entry point
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Runs a derived engine as ``python -m universalchess.services.derived_engines
# <engine-id>``. The installed ``engines/<name>`` launcher shim invokes this
# with the service venv python; the single positional argument selects the
# engine spec (name, options, and selection policy) from the registry.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import logging
import sys
from typing import Optional, Sequence

from universalchess.board.logging import log, setup_logging

from .spec import SPECS
from .stockfish import open_stockfish
from .uci_wrapper import run


def _configure_logging_for_uci() -> None:
    """Keep ``stdout`` protocol-only by routing this process's logs to stderr.

    ``stdout`` is the UCI channel the launching GUI/``popen_uci`` parses, so a
    single log line there corrupts the handshake and move stream for the reader
    (the app's engine probe reads ``option``/``uciok`` from exactly this
    stream). The module-level ``log`` is configured for ``stdout`` at import;
    this reconfigures it before any engine work runs. INFO level also drops
    python-chess's DEBUG protocol trace of the backing Stockfish, which would
    otherwise be dumped onto this process's output.
    """
    setup_logging(log_file_path="", log_level=logging.INFO, console_stream=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the derived engine selected by the first CLI argument.

    Returns a process exit code: 2 for a missing/unknown engine argument, 0
    after the UCI loop ends normally.
    """
    _configure_logging_for_uci()

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in SPECS:
        known = ", ".join(sorted(SPECS))
        log.error("[derived_engines] Expected an engine argument (one of: %s)", known)
        return 2

    spec = SPECS[args[0]]
    # Stockfish is opened lazily (only when a move must be analysed) so the UCI
    # handshake -- and the app's option probe, which never sends ``go`` -- does
    # not block on Stockfish startup. ``run`` owns the engine's lifecycle.
    run(open_stockfish, spec, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
