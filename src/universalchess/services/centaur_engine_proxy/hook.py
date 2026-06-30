"""Install the proxy as the engine Centaur execs (``engines/stockfish_pi``).

Centaur launches its engine via a path baked into its binary: ``engines/
stockfish_pi``. To hook in without modifying Centaur, the import writes a tiny
launcher there that runs the UC proxy under the service's venv python. The
launcher overwrites whatever ``stockfish_pi`` the SD shipped; the proxy then
plays the configured UC engine (UC always ships Stockfish), with no fallback to
the SD's original engine.
"""

from __future__ import annotations

import os
from pathlib import Path

from universalchess.paths import BASE_DIR

# The service venv python and the PYTHONPATH the systemd units use. Hardcoded to
# the install layout (units set PYTHONPATH=/opt and run /opt/universalchess/.venv
# /bin/python), so the launcher resolves the universalchess package the same way.
DEFAULT_PYTHON_BIN = os.path.join(BASE_DIR, ".venv", "bin", "python")
DEFAULT_PYTHONPATH = os.path.dirname(BASE_DIR)

PROXY_MODULE = "universalchess.services.centaur_engine_proxy"


def render_launcher(python_bin: str = DEFAULT_PYTHON_BIN, pythonpath: str = DEFAULT_PYTHONPATH) -> str:
    """Render the ``stockfish_pi`` launcher shell script.

    ``exec`` so the proxy replaces the shell (Centaur's signals reach it
    directly). PYTHONPATH is set so the package resolves regardless of Centaur's
    own cwd/environment.

    Two env adjustments are essential because Centaur execs this from inside its
    own install dir (``nice -n 10 engines/stockfish_pi`` with cwd ``~/centaur``):

    - ``PYTHONSAFEPATH=1`` stops Python from prepending that cwd to ``sys.path``.
      ``~/centaur`` is full of the original Centaur app's Python 3.5 C-extension
      ``.so`` files (``_sqlite3.so`` etc.). Without this, ``import sqlite3`` loads
      Centaur's 3.5 ``_sqlite3.so`` into our 3.x venv, failing with an undefined
      symbol -- which silently disabled the proxy's DB recording (it caught the
      import error and ran without a recorder). It also shadows any other stdlib
      extension, so the fix must be the broad cwd-exclusion, not a sqlite patch.
    - ``env -u LD_PRELOAD`` drops the display shim that translate mode preloads
      into Centaur. The shim virtualizes Centaur's e-paper SPI traffic only; it
      has no business in the engine or this proxy, and its frames inherit into
      every child. Stripping it keeps the engine/proxy on plain libc.
    """
    return (
        "#!/bin/sh\n"
        "# Universal Chess Centaur engine proxy launcher. Installed over the\n"
        "# engine path Centaur execs so its UCI traffic routes through the proxy\n"
        "# (any UC engine, memory-safe options, games recorded in UC's database).\n"
        "# PYTHONSAFEPATH keeps Centaur's cwd (full of py3.5 .so files) off\n"
        "# sys.path; -u LD_PRELOAD drops the display shim meant only for Centaur.\n"
        f'exec env -u LD_PRELOAD PYTHONPATH="{pythonpath}" PYTHONSAFEPATH=1 "{python_bin}" -m {PROXY_MODULE}\n'
    )


def install_engine_hook(
    engines_dir,
    *,
    python_bin: str = DEFAULT_PYTHON_BIN,
    pythonpath: str = DEFAULT_PYTHONPATH,
) -> Path:
    """Write the proxy launcher to ``<engines_dir>/stockfish_pi`` and mark it exec.

    Overwrites whatever ``stockfish_pi`` the SD image shipped (the original bash
    wrapper). Returns the launcher path.
    """
    engines_dir = Path(engines_dir)
    launcher = engines_dir / "stockfish_pi"
    launcher.write_text(render_launcher(python_bin, pythonpath))
    launcher.chmod(0o755)  # nosec B103 - launcher must be executable; 0o755 is least-permissive
    return launcher
