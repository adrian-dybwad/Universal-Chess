# Derived-engine launcher shim
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# A derived engine is installed as a tiny executable shell script at
# ``engines/<name>`` that the board execs via ``popen_uci``. The script runs the
# shared Python UCI wrapper for a given policy under the service venv, mirroring
# the launcher pattern used by the Centaur engine proxy (see
# ``services/centaur_engine_proxy/hook.py``). This keeps derived engines inside
# the existing "every engine is a UCI executable" runtime with no build step.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from universalchess.paths import BASE_DIR

# The service venv python and the PYTHONPATH the systemd units use, hardcoded to
# the install layout (units set PYTHONPATH=/opt and run
# /opt/universalchess/.venv/bin/python). Shared with the Centaur proxy launcher
# so the derived engine resolves the universalchess package the same way.
DEFAULT_PYTHON_BIN = os.path.join(BASE_DIR, ".venv", "bin", "python")
DEFAULT_PYTHONPATH = os.path.dirname(BASE_DIR)

# The runnable package; launched as ``python -m PACKAGE_MODULE <policy>``.
PACKAGE_MODULE = "universalchess.services.derived_engines"


def render_launcher(
    policy: str,
    python_bin: str = DEFAULT_PYTHON_BIN,
    pythonpath: str = DEFAULT_PYTHONPATH,
) -> str:
    """Render the ``engines/<name>`` launcher shell script for ``policy``.

    ``exec`` so the wrapper replaces the shell and the board's UCI signals reach
    it directly. ``PYTHONPATH`` is set so the package resolves regardless of the
    launching process's cwd, and ``PYTHONSAFEPATH=1`` keeps that cwd off
    ``sys.path``. The single positional argument selects which policy (and thus
    which engine) the shared wrapper runs.
    """
    return (
        "#!/bin/sh\n"
        "# Universal Chess derived-engine launcher. Runs the shared UCI wrapper\n"
        "# for one move-selection policy on top of the installed Stockfish.\n"
        f'exec env PYTHONPATH="{pythonpath}" PYTHONSAFEPATH=1 '
        f'"{python_bin}" -m {PACKAGE_MODULE} {policy}\n'
    )


def install_shim(
    engines_dir: Union[str, Path],
    name: str,
    policy: str,
    *,
    python_bin: str = DEFAULT_PYTHON_BIN,
    pythonpath: str = DEFAULT_PYTHONPATH,
) -> Path:
    """Write the launcher to ``<engines_dir>/<name>`` and mark it executable.

    Returns the launcher path. ``name`` is the installed engine id (the file the
    board execs); ``policy`` is the wrapper argument selecting the behaviour.
    """
    launcher = Path(engines_dir) / name
    launcher.write_text(render_launcher(policy, python_bin, pythonpath))
    launcher.chmod(0o755)  # nosec B103 - launcher must be executable; 0o755 is least-permissive
    return launcher
