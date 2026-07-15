"""Tests for path handling in ``EngineManager.is_installed``.

``is_installed`` builds a filesystem path from ``engine_name`` (a value that
originates from HTTP requests in the management API) and probes it with
``exists``/``os.access``. These tests guard CodeQL alerts #196/#197 ("uncontrolled
data used in a path expression", CWE-22): the resolved path must stay inside the
engines directory, and a name that is not a known engine must never resolve to a
path outside it. They also pin the normal top-level and Maia-style nested layouts
so the containment fix does not regress correct resolution.
"""

import os
from pathlib import Path

from universalchess.managers.engine_manager import EngineManager


def _make_executable(path: Path) -> None:
    """Create ``path`` (and parents) as an executable regular file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)


def test_is_installed_true_for_present_top_level_binary(tmp_path):
    """A non-system engine whose top-level binary is present reads as installed.

    Guards the common layout (engines_dir/<name>). A regression in the
    containment refactor would resolve the wrong path and report a present
    engine as not installed.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    _make_executable(tmp_path / "berserk")
    assert manager.is_installed("berserk") is True


def test_is_installed_false_when_binary_absent(tmp_path):
    """A known engine with no binary on disk reads as not installed.

    Guards the absent case so the management UI offers Install rather than
    falsely showing the engine present.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    assert manager.is_installed("berserk") is False


def test_is_installed_true_for_present_nested_maia_binary(tmp_path):
    """Maia's nested binary (engines_dir/maia/lc0) is resolved and detected.

    Guards the one subdirectory layout (engine_binary_subpath). If containment
    dropped the subpath segment the executable would be probed at the wrong
    location and Maia would read as not installed.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    _make_executable(tmp_path / "maia" / "lc0")
    assert manager.is_installed("maia") is True


def test_is_installed_false_for_non_executable_binary(tmp_path):
    """A present-but-non-executable binary reads as not installed.

    Guards the exists-and-executable contract: a file lacking the exec bit must
    not count, otherwise a partial/corrupt install would masquerade as usable.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    plain = tmp_path / "berserk"
    plain.write_bytes(b"not executable")
    plain.chmod(0o644)
    assert manager.is_installed("berserk") is False


def test_is_installed_false_for_unknown_or_traversal_name(tmp_path):
    """A name that is not a known engine never resolves outside engines_dir.

    This is the CWE-22 boundary (#196/#197): a request-derived name containing
    traversal segments must not cause a path probe outside the engines
    directory. A regression manifests as True (a matching path was found outside
    the base) or as an escape from engines_dir. The planted file lives one level
    above the base and would only be reached if the name escaped containment.
    """
    manager = EngineManager(engines_dir=str(tmp_path / "engines"))
    (tmp_path / "engines").mkdir()
    _make_executable(tmp_path / "outside")
    assert manager.is_installed("../outside") is False
    assert manager.is_installed("../../etc/passwd") is False
