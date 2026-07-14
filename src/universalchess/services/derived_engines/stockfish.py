# Stockfish resolution for derived engines
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# The derived engines have no evaluation of their own: they drive the installed
# Stockfish. This module resolves the Stockfish executable and opens a single
# long-lived engine process for the wrapper to reuse across the game.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import shutil

import chess.engine

from universalchess.board.logging import log
from universalchess.paths import get_engine_path


def resolve_stockfish_path() -> str:
    """Return the Stockfish executable path, or "" if it cannot be found.

    Prefers the app's own resolver (which finds the ``engines/stockfish``
    symlink the system-package install creates, independent of PATH) and falls
    back to a PATH lookup so a dev machine with Stockfish on PATH also works.
    """
    resolved = get_engine_path("stockfish")
    if resolved:
        return resolved
    which = shutil.which("stockfish")
    return which or ""


def open_stockfish() -> chess.engine.SimpleEngine:
    """Open a Stockfish UCI process for the wrapper to analyse with.

    Raises RuntimeError when Stockfish is not installed: the derived engines
    cannot function without it, so failing loudly here is preferable to a
    later, more confusing UCI error. The caller owns the returned engine and
    must ``quit()`` it.
    """
    path = resolve_stockfish_path()
    if not path:
        log.error("[derived_engines] Stockfish not found; cannot start derived engine")
        raise RuntimeError("Stockfish is required by the derived engines but was not found")
    log.info("[derived_engines] Backing derived engine with Stockfish at %s", path)
    return chess.engine.SimpleEngine.popen_uci(path)
