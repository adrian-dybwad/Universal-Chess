"""Tests for the postinst's creation of ``/opt/universalchess/.venv``.

These guard the regression where an install that had already failed once could
never succeed again, even after the missing dependency was installed. The
postinst skipped venv creation whenever ``.venv/bin/python`` existed, but
``python3 -m venv`` links the interpreter into ``bin/`` *before* it runs
ensurepip -- so a create that aborted at ensurepip (a board with no
``python3-venv``) left a directory that satisfied that test permanently. Every
later install skipped creation and died one line further on with
``.venv/bin/pip: No such file or directory``.

The block is executed against a fake ``python3`` that reproduces that ordering
rather than only pattern-matched, so the partial directory the tests start from
is the one the real tool leaves behind.
"""

import os
import subprocess
from pathlib import Path

import pytest

import universalchess.services.update_service as us

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/postinst.
POSTINST = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

# Fake `python3 -m venv`, in the order the real module works: it links the
# interpreter into bin/ first and only then runs ensurepip to install pip. That
# ordering is the whole point -- a fake that created both at once, or neither,
# could not produce the half-built directory these tests start from.
_FAKE_PYTHON3 = """#!/bin/sh
echo "python3 $*" >> "$UC_TEST_LOG"
for target; do :; done
mkdir -p "$target/bin"
: > "$target/bin/python"
chmod +x "$target/bin/python"
if [ -n "${UC_TEST_ENSUREPIP_FAILS:-}" ]; then
  echo "ensurepip is not available" >&2
  exit 1
fi
: > "$target/bin/pip"
chmod +x "$target/bin/pip"
"""


@pytest.fixture
def postinst_text() -> str:
    """The postinst must ship in the source tree; a missing file means the
    package has no install-time configuration at all.
    """
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    return POSTINST.read_text()


def _venv_creation_block(text: str) -> str:
    """The postinst's venv-creation ``if`` statement, verbatim.

    Located from the ``python3 -m venv`` invocation outwards rather than from a
    comment, so rewording the surrounding commentary cannot silently reduce
    these tests to running an empty string.
    """
    lines = text.splitlines()
    invocations = [i for i, line in enumerate(lines) if "-m venv" in line]
    assert len(invocations) == 1, (
        f"expected exactly one `python3 -m venv` in the postinst; got {len(invocations)}"
    )
    index = invocations[0]
    start = next(i for i in range(index, -1, -1) if lines[i].startswith("if "))
    end = next(i for i in range(index, len(lines)) if lines[i].startswith("fi"))
    return "\n".join(lines[start : end + 1])


@pytest.fixture
def venv_env(tmp_path):
    """A fake ``python3`` on PATH, an empty install prefix, and a call log.

    The fake directory is prepended rather than replacing PATH, because the fake
    itself needs mkdir and chmod to lay down the directory it is imitating. It
    still shadows the real interpreter -- a PATH lookup takes the first match --
    so the block cannot build an actual venv and pass regardless of what the
    guard decides.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python3 = bindir / "python3"
    python3.write_text(_FAKE_PYTHON3)
    python3.chmod(0o755)

    venv_dir = tmp_path / "opt" / "universalchess" / ".venv"
    venv_dir.parent.mkdir(parents=True)

    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["UC_TEST_LOG"] = str(log)
    env["VENV_DIR"] = str(venv_dir)
    return env, log, venv_dir


def _run_creation_block(postinst_text, env):
    """Run the shipped block under the postinst's own shell options."""
    script = "set -euo pipefail\n" + _venv_creation_block(postinst_text)
    proc = subprocess.run(  # noqa: S603 - runs the postinst's own venv block
        ["/bin/bash", "-c", script],
        env=env, capture_output=True, text=True, timeout=60,
    )
    log = Path(env["UC_TEST_LOG"])
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _venv_creations(calls) -> list:
    return [call for call in calls if "-m venv" in call]


def test_creates_the_venv_when_it_is_absent(postinst_text, venv_env):
    """A first install builds the environment.

    Why this test exists: the other cases push toward "skip creation", and the
    cheapest way to satisfy them is a guard that never creates anything -- which
    breaks every fresh install. This pins the case that must still act.

    How a regression manifests: no `-m venv` call is recorded and bin/pip never
    appears, so the wheel install one line later has no pip to run.
    """
    env, _log, venv_dir = venv_env

    proc, calls = _run_creation_block(postinst_text, env)

    assert proc.returncode == 0, proc.stderr
    assert len(_venv_creations(calls)) == 1, calls
    assert (venv_dir / "bin" / "pip").is_file()


def test_rebuilds_a_venv_left_half_built_by_a_failed_create(postinst_text, venv_env):
    """A create that aborted at ensurepip must be finished on the next install.

    Why this test exists: this is the field failure. The board's first install
    had no python3-venv, so venv linked bin/python and then died at ensurepip.
    Adding the dependency fixed nothing, because the guard tested bin/python and
    that file was already there -- the install skipped creation and failed at
    `.venv/bin/pip: No such file or directory` instead, with no way out but
    deleting the directory by hand.

    The half-built directory is produced by running the shipped block against a
    python3 whose ensurepip fails, so it is exactly the state the real tool
    leaves -- an interpreter with no pip beside it.

    How a regression manifests: the second run records no `-m venv` call and
    bin/pip is still missing, i.e. the board stays unrecoverable.
    """
    env, _log, venv_dir = venv_env

    failing = dict(env, UC_TEST_ENSUREPIP_FAILS="1")
    first, _calls = _run_creation_block(postinst_text, failing)

    # The premise of this test: the first attempt must fail and must leave an
    # interpreter with no pip. If venv ever stopped leaving that behind, the
    # second run below would be testing nothing.
    assert first.returncode != 0
    assert (venv_dir / "bin" / "python").is_file()
    assert not (venv_dir / "bin" / "pip").exists()

    Path(env["UC_TEST_LOG"]).unlink()
    second, calls = _run_creation_block(postinst_text, env)

    assert second.returncode == 0, second.stderr
    assert len(_venv_creations(calls)) == 1, (
        "the postinst skipped venv creation over a half-built directory; the "
        f"install cannot recover from a failed first attempt. Calls: {calls}"
    )
    assert (venv_dir / "bin" / "pip").is_file()


def test_leaves_a_complete_venv_alone(postinst_text, venv_env):
    """An upgrade must not rebuild an environment that already works.

    Why this test exists: the fix could have been "always run venv", which is
    wrong in a way nothing else here would catch -- every upgrade would re-run
    ensurepip on the slowest hardware the product ships on, for no gain.

    How a regression manifests: a `-m venv` call is recorded even though both
    the interpreter and pip were already present.
    """
    env, _log, venv_dir = venv_env
    (venv_dir / "bin").mkdir(parents=True)
    for name in ("python", "pip"):
        tool = venv_dir / "bin" / name
        tool.touch()
        tool.chmod(0o755)

    proc, calls = _run_creation_block(postinst_text, env)

    assert proc.returncode == 0, proc.stderr
    assert _venv_creations(calls) == [], calls


def test_rebuilds_a_venv_whose_interpreter_is_missing(postinst_text, venv_env):
    """A pip script with no interpreter beside it is not a usable environment.

    Why this test exists: the guard has to describe the environment the rest of
    the postinst uses, which is both tools -- bin/pip installs the wheels and
    bin/python runs compileall and the shim build. Testing pip alone would trade
    the original defect for its mirror image and fail just as unrecoverably,
    several steps later and further from the cause.

    How a regression manifests: no `-m venv` call, and the install proceeds to
    run a pip whose shebang points at an interpreter that is not there.
    """
    env, _log, venv_dir = venv_env
    (venv_dir / "bin").mkdir(parents=True)
    pip = venv_dir / "bin" / "pip"
    pip.touch()
    pip.chmod(0o755)

    proc, calls = _run_creation_block(postinst_text, env)

    assert proc.returncode == 0, proc.stderr
    assert len(_venv_creations(calls)) == 1, calls
    assert (venv_dir / "bin" / "python").is_file()
