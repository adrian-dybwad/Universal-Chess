"""Tests for layout-aware engine uninstall.

Background / why these tests exist
----------------------------------
Engines install with two different on-disk shapes:

* Single-file engines: one executable at ``engines/<name>`` (plus any declared
  ``extra_files`` alongside it, e.g. Arasan's ``*.nnue``).
* Custom-script engines (``repo_url is None`` with a ``binary_path``): the whole
  install lives in a subdirectory ``engines/<name>/`` -- for Maia that is
  ``engines/maia/lc0`` plus ``engines/maia/maia_weights`` and
  ``engines/maia/leela_weights``.

``uninstall_engine`` previously assumed the single-file shape for everyone: it
called ``Path.unlink()`` on ``engines/maia`` (a directory, raising
IsADirectoryError which was swallowed) and cleaned ``engines/maia_weights`` (the
wrong path). The net effect left the entire ``engines/maia/`` tree behind. These
tests pin that a directory-layout engine is removed wholesale while the
single-file path is unchanged.
"""

import stat

from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_record import EngineInstallRecordStore


def _make_executable(path):
    """Create parent dirs, an empty file at path, and mark it executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _manager(tmp_path):
    """An EngineManager on a temp engines dir with a temp record store."""
    return EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=EngineInstallRecordStore(path=tmp_path / "record.json"),
    )


def test_uninstall_removes_custom_script_directory_tree(tmp_path):
    """Uninstalling Maia removes the whole engines/maia/ tree, weights included.

    Why this test exists: Maia installs as a directory (binary + weight files),
    so uninstall must remove the directory, not attempt to unlink it as a file.

    How the regression manifests: the old code unlink()'d the directory (raising
    IsADirectoryError, swallowed) and cleaned the wrong weights path, leaving
    engines/maia/ on disk -- so is_installed stayed True after uninstall. The
    assertions below fail (the directory and lc0 still exist) when that
    regression is present.
    """
    manager = _manager(tmp_path)
    maia_dir = tmp_path / "engines" / "maia"
    _make_executable(maia_dir / "lc0")
    (maia_dir / "maia_weights").mkdir()
    (maia_dir / "maia_weights" / "maia-1500.pb.gz").write_text("net")

    assert manager.is_installed("maia") is True
    assert manager.uninstall_engine("maia") is True

    # The whole tree is gone, so nothing can be relaunched or falsely reported.
    assert not maia_dir.exists()
    assert manager.is_installed("maia") is False


def test_uninstall_removes_single_file_engine_binary(tmp_path):
    """A single-file engine's binary is still removed (common path unchanged).

    Why this test exists: the directory-aware branch must not regress the
    ordinary case. Berserk is a single executable at engines/berserk with no
    subdirectory.

    How the regression manifests: if the fix routed every engine through the
    directory branch, the single-file unlink would be skipped and the binary
    would survive uninstall, so is_installed would stay True.
    """
    manager = _manager(tmp_path)
    _make_executable(tmp_path / "engines" / "berserk")

    assert manager.is_installed("berserk") is True
    assert manager.uninstall_engine("berserk") is True
    assert not (tmp_path / "engines" / "berserk").exists()
    assert manager.is_installed("berserk") is False
