"""Tests for MenuContext path capture/restore used by game suspend/resume.

Why these tests exist
---------------------
When the game is suspended (PLAY) the full menu must reopen at the exact submenu
the user was in. main.py captures the position via ``get_restore_path()`` before
the menu stack unwinds and re-applies it with ``restore_from_path()`` on return.
These tests pin that round-trip and that the navigation depth is rewound so a
subsequent level-by-level re-entry (enter_menu) restores correctly. A regression
here makes "pause and return" drop the user at the menu root instead of their
last position.
"""

import pytest

from universalchess.utils.settings_persistence import MenuContext


@pytest.fixture
def ctx(monkeypatch):
    """A MenuContext whose persistence is stubbed out.

    save() writes to centaur.ini in production; tests only care about in-memory
    stack behavior, so persistence is neutralized to keep them hermetic.
    """
    context = MenuContext()
    monkeypatch.setattr(context, "save", lambda: None)
    return context


def test_restore_from_path_round_trips_capture(ctx):
    """restore_from_path must reproduce exactly what get_restore_path captured.

    Regression manifestation: if the stacks are not repopulated faithfully, the
    restored menu path diverges from where the user suspended, so the wrong (or
    root) menu is shown.
    """
    captured = [("Settings", 2), ("DisplaySound", 1)]

    ctx.restore_from_path(captured)

    assert ctx.path_stack == ["Settings", "DisplaySound"]
    assert ctx.index_stack == [2, 1]
    assert ctx.get_restore_path() == captured


def test_restore_from_path_rewinds_nav_depth(ctx):
    """restore_from_path must rewind _nav_depth to 0 for level-by-level re-entry.

    Why: _handle_settings restores by calling enter_menu("Settings") then deeper
    levels; enter_menu only enters "restore" mode when _nav_depth points at the
    matching saved level. If depth is not reset to 0, the first enter_menu sees a
    mismatch and truncates the saved path, losing the deeper position.

    Regression manifestation: deep submenu (e.g. DisplaySound) is dropped and the
    user lands in Settings root instead.
    """
    ctx._nav_depth = 5

    ctx.restore_from_path([("Settings", 0)])

    assert ctx._nav_depth == 0
    # And the restored level is actually re-entered in restore mode (returns the
    # saved index and advances depth), not truncated.
    assert ctx.enter_menu("Settings", default_index=9) == 0
    assert ctx._nav_depth == 1


def test_restore_from_empty_path_clears_stacks(ctx):
    """An empty capture (PLAY at the root menu) restores to a clean root.

    Regression manifestation: stale stack entries survive and the root menu
    wrongly re-enters a submenu.
    """
    ctx.path_stack = ["Settings", "Players"]
    ctx.index_stack = [1, 2]

    ctx.restore_from_path([])

    assert ctx.path_stack == []
    assert ctx.index_stack == []
    assert ctx.get_restore_path() == []
