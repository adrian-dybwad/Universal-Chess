"""Tests for EngineManager.is_available (engine-selection availability).

Background / why these tests exist
----------------------------------
"Available to select" and "binary physically installed" are two different
concepts that were previously duplicated with subtly different logic: the web
dropdowns used ``is_system_package or is_installed`` while the on-device picker
used only ``is_installed``. Because ``is_installed`` resolves a system package
via ``shutil.which`` (PATH-based) and the service PATH omits ``/usr/games``,
Stockfish (a system package) was dropped from the board picker even though the
web offered it.

``is_available`` is the single shared definition. These tests pin its two
branches - system packages are always available (regardless of PATH), and other
engines are available only when their binary is present - so the board and web
can never drift apart again.
"""

import os
import stat

from universalchess.managers.engine_manager import EngineManager


def _make_executable(path):
    """Create an empty file at path and mark it executable."""
    path.write_bytes(b"")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_system_package_available_even_when_not_on_path(tmp_path, monkeypatch):
    """Stockfish stays selectable even when shutil.which cannot find it.

    Why: this is the reported bug - a service PATH without /usr/games makes the
    PATH-based is_installed check fail, so the board dropped Stockfish. is_available
    must not depend on PATH for a system package.

    How the regression manifests: is_available returns False (mirroring the old
    is_installed-only board logic), so Stockfish disappears from the picker.
    """
    # Force the PATH lookup that is_installed uses for system packages to fail,
    # reproducing the service environment where /usr/games is not on PATH.
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.shutil.which",
        lambda _name: None,
    )
    manager = EngineManager(engines_dir=str(tmp_path))

    # Pin the difference the fix relies on: is_installed is PATH-based and fails,
    # but is_available treats the system package as selectable regardless.
    assert manager.is_installed("stockfish") is False
    assert manager.is_available("stockfish") is True


def test_non_system_engine_unavailable_without_binary(tmp_path):
    """A source-built engine with no binary present is not selectable.

    Why: non-system engines must actually be installed before they can be
    offered; availability must not be granted just because a catalog entry
    exists.

    How the regression manifests: is_available returns True for an engine whose
    binary was never installed, so selecting it would fail at game start.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    assert manager.is_available("berserk") is False


def test_non_system_engine_available_when_binary_present(tmp_path):
    """A source-built engine becomes selectable once its binary is installed.

    Why: complements the previous test - availability must follow the presence
    of the executable for non-system engines.

    How the regression manifests: is_available stays False after the binary is
    installed (e.g. wrong path checked), so an installed engine never appears.
    """
    _make_executable(tmp_path / "berserk")
    manager = EngineManager(engines_dir=str(tmp_path))
    assert manager.is_installed("berserk") is True
    assert manager.is_available("berserk") is True


def test_unknown_engine_not_available(tmp_path):
    """An engine name not in the catalog is never available.

    Why: guards against offering (and later failing to launch) an engine the app
    has no definition for.

    How the regression manifests: is_available returns True (or raises) for an
    unknown name instead of a clean False.
    """
    manager = EngineManager(engines_dir=str(tmp_path))
    assert manager.is_available("does-not-exist") is False
