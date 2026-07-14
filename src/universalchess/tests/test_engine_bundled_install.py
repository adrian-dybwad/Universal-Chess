"""Tests for bundled-engine install/uninstall (Worstfish, Drawfish).

Background / why these tests exist
----------------------------------
Worstfish and Drawfish are "bundled" engines: they ship with the application as a
Python UCI wrapper and are not compiled or fetched. ``install_engine`` must not
route them through the source-build path (they have no repo_url, so a build
would fail); instead ``_install_bundled`` writes an executable launcher shim at
``engines/<name>``. Once written, the normal single-file resolution must report
them installed/available, and the normal single-file uninstall must remove them.

These tests drive the real ``EngineManager`` against a temp engines dir so no
compilation, download, or network access occurs.
"""

import os

import pytest

from universalchess.managers.engine_manager import ENGINES, EngineManager
from universalchess.services.engine_install_record import EngineInstallRecordStore

# The two bundled engines. Parameterised so both are held to the same contract.
BUNDLED_ENGINES = ["worstfish", "drawfish"]


def _manager(tmp_path):
    """An EngineManager on a temp engines dir with a temp record store."""
    return EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=EngineInstallRecordStore(path=tmp_path / "record.json"),
    )


@pytest.mark.parametrize("name", BUNDLED_ENGINES)
def test_bundled_engine_is_marked_bundled_without_repo_or_build(name):
    """Each bundled engine declares is_bundled and carries nothing to build.

    Why: the install router keys off ``is_bundled``; a repo_url or build_commands
    would additionally (mis)route it to the git/build path. How it manifests: a
    stray repo_url flips ``source_installable`` True in the web layer and a
    fresh install would attempt a doomed clone/compile.
    """
    engine = ENGINES[name]
    assert engine.is_bundled is True
    assert engine.is_system_package is False
    assert engine.repo_url is None
    assert engine.build_commands == []
    # Empty binary_path keeps it a top-level single-file engine (not a Maia-style
    # subdirectory engine), so is_installed/get_engine_path resolve engines/<name>.
    assert engine.binary_path == ""


@pytest.mark.parametrize("name", BUNDLED_ENGINES)
def test_install_bundled_writes_executable_shim_and_marks_available(tmp_path, name):
    """Installing a bundled engine writes an exec shim and makes it available.

    Why: this is the whole install path for bundled engines. How it manifests:
    if install routed to the source build (repo_url is None) it would return
    False and no file would appear; is_installed/is_available would stay False
    and the engine would never be selectable.
    """
    manager = _manager(tmp_path)
    shim_path = tmp_path / "engines" / name

    assert manager.is_installed(name) is False
    assert manager.is_available(name) is False

    assert manager.install_engine(name) is True

    assert shim_path.exists()
    assert os.access(shim_path, os.X_OK)
    # The shim runs the shared wrapper with this engine's name as the policy arg.
    content = shim_path.read_text()
    assert f"-m universalchess.services.derived_engines {name}" in content

    assert manager.is_installed(name) is True
    assert manager.is_available(name) is True


@pytest.mark.parametrize("name", BUNDLED_ENGINES)
def test_uninstall_bundled_removes_shim(tmp_path, name):
    """Uninstalling a bundled engine removes its launcher shim.

    Why: the single-file uninstall path must handle bundled engines (no special
    directory tree). How it manifests: a leftover shim would keep is_installed
    True after uninstall, so the UI would wrongly show it installed.
    """
    manager = _manager(tmp_path)
    shim_path = tmp_path / "engines" / name
    assert manager.install_engine(name) is True
    assert shim_path.exists()

    assert manager.uninstall_engine(name) is True

    assert not shim_path.exists()
    assert manager.is_installed(name) is False
    assert manager.is_available(name) is False
