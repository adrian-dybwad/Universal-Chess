"""Tests for the derived-engine launcher shim.

Background / why these tests exist
----------------------------------
A derived engine is installed as a tiny executable shell script at
``engines/<name>`` that the board's ``popen_uci`` launches; the shim in turn
runs the Python UCI wrapper for the right policy under the service venv. These
tests pin the shim's contents (so the wrapper is invoked with the correct
policy and import path) and that it is written executable (so ``popen_uci`` can
exec it).
"""

import os
import stat

from universalchess.services.derived_engines.shim import (
    PACKAGE_MODULE,
    install_shim,
    render_launcher,
)


def test_render_launcher_invokes_wrapper_module_with_policy():
    """The shim execs the venv python running the wrapper module + policy arg.

    Why: the policy name is how one shared wrapper serves both Worstfish and
    Drawfish. How it manifests: a missing/incorrect policy arg or module path would
    launch the wrong engine (or fail to import), which this pins exactly.
    """
    script = render_launcher(
        "worstfish", python_bin="/opt/uc/.venv/bin/python", pythonpath="/opt"
    )

    assert script.startswith("#!/bin/sh\n")
    assert "exec " in script
    assert '"/opt/uc/.venv/bin/python"' in script
    assert 'PYTHONPATH="/opt"' in script
    assert f"-m {PACKAGE_MODULE} worstfish" in script


def test_install_shim_writes_executable_file(tmp_path):
    """install_shim writes engines/<name> with the rendered script, executable.

    Why: `popen_uci` execs this path directly, so the file must exist, contain
    the launcher, and carry the executable bit. How it manifests: without the
    exec bit the engine fails to start with a permission error; wrong contents
    launch the wrong policy.
    """
    engines_dir = tmp_path / "engines"
    engines_dir.mkdir()

    path = install_shim(
        engines_dir,
        "drawfish",
        "drawfish",
        python_bin="/opt/uc/.venv/bin/python",
        pythonpath="/opt",
    )

    assert path == engines_dir / "drawfish"
    assert path.exists()
    assert os.access(path, os.X_OK)
    content = path.read_text()
    assert content == render_launcher(
        "drawfish", python_bin="/opt/uc/.venv/bin/python", pythonpath="/opt"
    )
    # World-executable but not world-writable (least privilege for a launcher).
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o755
