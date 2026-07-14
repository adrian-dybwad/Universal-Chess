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

import sys
from typing import Optional, Sequence

from universalchess.board.logging import log

from .spec import SPECS
from .stockfish import open_stockfish
from .uci_wrapper import run


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the derived engine selected by the first CLI argument.

    Returns a process exit code: 2 for a missing/unknown engine argument, 0
    after the UCI loop ends normally.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in SPECS:
        known = ", ".join(sorted(SPECS))
        log.error("[derived_engines] Expected an engine argument (one of: %s)", known)
        return 2

    spec = SPECS[args[0]]
    engine = open_stockfish()
    try:
        run(engine, spec, sys.stdin, sys.stdout)
    finally:
        engine.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
