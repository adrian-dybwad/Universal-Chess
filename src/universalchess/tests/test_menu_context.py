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
    captured = [("Settings", 2), ("Display", 1)]

    ctx.restore_from_path(captured)

    assert ctx.path_stack == ["Settings", "Display"]
    assert ctx.index_stack == [2, 1]
    assert ctx.get_restore_path() == captured


def test_restore_from_path_rewinds_nav_depth(ctx):
    """restore_from_path must rewind _nav_depth to 0 for level-by-level re-entry.

    Why: _handle_settings restores by calling enter_menu("Settings") then deeper
    levels; enter_menu only enters "restore" mode when _nav_depth points at the
    matching saved level. If depth is not reset to 0, the first enter_menu sees a
    mismatch and truncates the saved path, losing the deeper position.

    Regression manifestation: deep submenu (e.g. Display) is dropped and the
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


def test_deep_path_round_trips_and_replays_level_by_level(ctx):
    """A 4-level path re-enters every level in restore mode, returning each index.

    Why this test exists: full-depth menu restore replays the whole navigation
    chain (Settings -> Connectivity -> Bluetooth -> Devices), not just the first
    submenu. Each level is re-entered with enter_menu(); the engine relies on it
    returning that level's saved index and advancing depth so the deeper level is
    then matched too.

    How a regression manifests: if enter_menu stops matching at some depth (e.g.
    off-by-one in the depth check), that level truncates the saved path and the
    remaining deeper levels are lost -- restore lands short of where the user was.
    """
    captured = [
        ("Settings", 6),
        ("connectivity", 1),
        ("bluetooth", 2),
        ("bluetooth.devices.list", 3),
    ]

    ctx.restore_from_path(captured)

    # Each level is re-entered in order and yields its own saved index while the
    # nav depth advances one per level (mirrors the engine descending the chain).
    assert ctx.enter_menu("Settings", default_index=0) == 6
    assert ctx._nav_depth == 1
    assert ctx.enter_menu("connectivity", default_index=0) == 1
    assert ctx._nav_depth == 2
    assert ctx.enter_menu("bluetooth", default_index=0) == 2
    assert ctx._nav_depth == 3
    assert ctx.enter_menu("bluetooth.devices.list", default_index=0) == 3
    assert ctx._nav_depth == 4
    # The whole chain is preserved by the round-trip.
    assert ctx.get_restore_path() == captured


def test_update_index_targets_current_depth_not_root(ctx):
    """update_index writes the level at the current nav depth, not the root.

    Why this test exists (LONG_PLAY power-down regression): on a SHUTDOWN unwind
    the engine submenu levels deliberately do NOT pop (so a restart can restore
    the full path), which leaves _nav_depth pointing at the deepest submenu when
    control returns to the Settings shell loop. The shell must therefore NOT call
    update_index for the non-entry SHUTDOWN result -- doing so writes that
    result's index (0) at the deepest level and clobbers the live cursor (e.g.
    Bluetooth focused on Devices) that the next launch must restore, landing the
    user on the status/disable button instead. This pins the depth-targeting
    contract that makes the shell's "only persist real entries" guard necessary.

    How a regression manifests: if update_index wrote index_stack[0] (the root)
    regardless of depth, the deep-index reasoning behind the guard would be
    invalid; this asserts the deepest level is the write target.
    """
    ctx.path_stack = ["Settings", "connectivity", "bluetooth"]
    ctx.index_stack = [1, 1, 1]  # bluetooth focused on Devices (index 1)
    ctx._nav_depth = 3  # engine levels did not pop on the SHUTDOWN unwind

    ctx.update_index(0)

    # The deepest level is overwritten, not the root -- which is exactly why the
    # shell must skip this call for a non-entry SHUTDOWN result.
    assert ctx.index_stack == [1, 1, 0]


def test_next_restore_token_peeks_the_saved_child_at_current_depth(ctx):
    """next_restore_token() returns the not-yet-entered token at the nav depth.

    Why this test exists: on restore the engine, sitting in a container, must
    know which child container the saved path descends into next so it can
    auto-dispatch the matching row. next_restore_token() is that peek: it returns
    path_stack[_nav_depth] (the next level down) without consuming it, and None
    once the saved chain is exhausted (deepest level reached -> stop descending).

    How a regression manifests: returning the wrong element (e.g. the current
    level instead of the next) would auto-descend into the wrong container or
    loop; returning non-None at the leaf would try to descend past the saved
    path and land on a stale/absent row.
    """
    ctx.restore_from_path(
        [("Settings", 0), ("connectivity", 0), ("bluetooth", 0)]
    )

    # At the root (depth 0) the next level to descend into is "Settings".
    assert ctx.next_restore_token() == "Settings"
    ctx.enter_menu("Settings")
    assert ctx.next_restore_token() == "connectivity"
    ctx.enter_menu("connectivity")
    assert ctx.next_restore_token() == "bluetooth"
    ctx.enter_menu("bluetooth")
    # Deepest saved level entered: nothing further to descend into.
    assert ctx.next_restore_token() is None


def test_next_restore_token_is_none_after_divergence(ctx):
    """A fresh (diverged) descent clears the peek so no stale auto-descent fires.

    Why this test exists: if the user navigates somewhere other than the saved
    path, enter_menu truncates the saved chain. next_restore_token() must then
    report None so the engine stops trying to replay a path the user abandoned.

    How a regression manifests: a stale token would auto-dispatch a row the user
    did not choose, hijacking live navigation after a divergence.
    """
    ctx.restore_from_path([("Settings", 0), ("connectivity", 0)])

    # Diverge at the first level: enter a different menu than the saved one.
    ctx.enter_menu("Game")

    assert ctx.next_restore_token() is None


def test_freeze_suppresses_persistence_so_shutdown_unwind_keeps_deep_path(monkeypatch):
    """After freeze(), leave_menu unwinding must not overwrite the saved path.

    Why this test exists: on SIGTERM the process exits via sys.exit() from the
    signal handler, unwinding the blocked menu stack and running every
    run_engine_menu ``finally: leave_menu``. Before the fix each pop persisted a
    shallower path (Settings/connectivity/bluetooth -> ... -> Settings), so a
    restart could only restore to the top level. freeze() (called at the start of
    cleanup_and_exit) must make save() a no-op so the deepest position -- already
    on disk from when the user entered it -- is what the next launch restores.

    How a regression manifests: if freeze() fails to gate save(), the recorded
    writes below include the shallower post-pop paths, so last_written collapses
    to "Settings" instead of staying at the deep "Settings/connectivity/bluetooth".
    """
    writes = []
    # Capture what would hit centaur.ini so the test asserts on persistence, not
    # just in-memory state (the bug was on-disk state being overwritten).
    monkeypatch.setattr(
        "universalchess.utils.settings_persistence.save_setting",
        lambda section, key, value, **kw: writes.append((key, value)),
    )
    context = MenuContext()
    # User drilled down to the deepest menu; this is the position on disk.
    context.enter_menu("Settings", 0)
    context.enter_menu("connectivity", 0)
    context.enter_menu("bluetooth", 1)
    deep_path = [(k, v) for (k, v) in writes if k == "path"][-1][1]
    assert deep_path == "Settings/connectivity/bluetooth"

    # Shutdown begins: persistence is frozen, then the stack unwinds (leave_menu
    # per level, exactly as the SystemExit teardown does).
    context.freeze()
    writes.clear()
    context.leave_menu()  # would pop bluetooth
    context.leave_menu()  # would pop connectivity
    context.leave_menu()  # would pop Settings

    # No path write reached disk during the unwind, so the pre-freeze deep path
    # persists for the next launch to restore.
    assert writes == []
