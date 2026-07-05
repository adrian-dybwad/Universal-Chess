"""Tests for session view-state persistence and startup restoration decisions.

Why these tests exist
---------------------
On a service restart or shutdown the app must come back up in the exact state it
was in -- the plain board, a coach panel on a specific move, or the menu with a
game paused behind it -- rather than always showing the board. The persisted
:class:`SessionSnapshot` and the pure :func:`plan_startup` decision drive that
restoration. These tests pin:

- the snapshot's load/save round-trip and its resilience to corrupt values, so a
  partially written or garbage section never blocks boot;
- the crash-loop guard, so a snapshot that keeps crashing boot is eventually
  discarded instead of looping forever;
- every branch of the startup decision matrix, so each recorded view maps to the
  correct resume/suspend/coach-selection behavior.

A regression in any of these makes restart drop the user out of their prior
state (or, for the guard, risks an unrecoverable boot loop).
"""

import pytest

from universalchess.utils.session_state import (
    MAX_RESTORE_ATTEMPTS,
    SESSION_STATE_SECTION,
    VIEW_GAME,
    VIEW_MENU,
    VIEW_NONE,
    VIEW_SETTINGS,
    SessionSnapshot,
    StartupPlan,
    plan_startup,
)


@pytest.fixture
def ini_store(monkeypatch):
    """Back Settings.read/write with an in-memory dict for hermetic persistence.

    The snapshot persists via the shared settings_persistence helpers, which
    ultimately call Settings.read/write against centaur.ini. Replacing those with
    a dict keyed by (section, key) exercises the real save/load code paths
    (including type coercion) without touching the filesystem.
    """
    store = {}

    def fake_read(section, key, default=""):
        return store.get((section, key), default)

    def fake_write(section, key, value, default=""):
        store[(section, key)] = str(value)

    from universalchess.board import settings as settings_mod

    monkeypatch.setattr(settings_mod.Settings, "read", staticmethod(fake_read))
    monkeypatch.setattr(settings_mod.Settings, "write", staticmethod(fake_write))
    return store


# ---------------------------------------------------------------------------
# SessionSnapshot persistence
# ---------------------------------------------------------------------------

def test_load_defaults_when_nothing_stored(ini_store):
    """A first boot with no saved section loads as unrecorded, not a real view.

    The unrecorded sentinel is what lets a fresh (or upgraded-mid-game) device
    fall through to the shipped default. Regression manifestation: defaulting to
    a concrete view (e.g. "menu") would make an in-progress game on an upgraded
    device suspend to the menu instead of resuming to the board.
    """
    snapshot = SessionSnapshot.load()

    assert snapshot.app_view == VIEW_NONE
    assert snapshot.game_db_id == 0
    assert snapshot.analysis_selection == 0
    assert snapshot.restore_attempts == 0


def test_save_then_load_round_trips_all_fields(ini_store):
    """Every persisted field survives a save/load cycle unchanged.

    Regression manifestation: a dropped or mistyped field means the restored
    view diverges from what was saved (e.g. coach ply lost, so restart shows the
    board instead of the coach panel).
    """
    SessionSnapshot(
        app_view=VIEW_GAME, game_db_id=42, analysis_selection=7, restore_attempts=2
    ).save()

    loaded = SessionSnapshot.load()

    assert loaded.app_view == VIEW_GAME
    assert loaded.game_db_id == 42
    assert loaded.analysis_selection == 7
    assert loaded.restore_attempts == 2


def test_save_writes_to_session_state_section(ini_store):
    """State is stored in the merged [SessionState] section, not a separate one.

    Regression manifestation: writing to the wrong section would split session
    state across two places, defeating the MenuState/SessionState merge and
    leaving the loader unable to find its own keys.
    """
    SessionSnapshot(app_view=VIEW_GAME, game_db_id=7, analysis_selection=3).save()

    assert ini_store[(SESSION_STATE_SECTION, "app_view")] == VIEW_GAME
    assert ini_store[(SESSION_STATE_SECTION, "game_db_id")] == "7"
    assert ini_store[(SESSION_STATE_SECTION, "analysis_selection")] == "3"


def test_load_rejects_unknown_view(ini_store):
    """A garbage app_view is treated as unrecorded rather than propagating.

    Uses an explicitly invalid value to hit the validation branch. Regression
    manifestation: an unknown view leaking through would drive plan_startup down
    an unhandled path (or crash), turning a corrupt byte into a failed boot.
    """
    ini_store[(SESSION_STATE_SECTION, "app_view")] = "garbage"

    assert SessionSnapshot.load().app_view == VIEW_NONE


def test_load_clamps_negative_selection(ini_store):
    """A negative coach selection is clamped to the board view (0).

    A negative value can only arise from corruption; -1 is the boundary just
    below the valid range. Regression manifestation: a negative ply would index
    incorrectly when re-selecting the coach move on restore.
    """
    ini_store[(SESSION_STATE_SECTION, "analysis_selection")] = "-4"

    assert SessionSnapshot.load().analysis_selection == 0


# ---------------------------------------------------------------------------
# Crash-loop guard
# ---------------------------------------------------------------------------

def test_attempts_not_exhausted_below_threshold():
    """One short of the threshold is still allowed to restore.

    MAX-1 is the boundary that must NOT trip the guard. Regression
    manifestation: an off-by-one would abandon exact restoration one attempt too
    early, showing the default view when the real state was still recoverable.
    """
    snapshot = SessionSnapshot(restore_attempts=MAX_RESTORE_ATTEMPTS - 1)

    assert snapshot.attempts_exhausted() is False


def test_attempts_exhausted_at_threshold():
    """Reaching the threshold trips the guard.

    MAX is the boundary that must trip. Regression manifestation: failing to
    trip here lets a poison snapshot loop the boot indefinitely under systemd's
    Restart=on-failure.
    """
    snapshot = SessionSnapshot(restore_attempts=MAX_RESTORE_ATTEMPTS)

    assert snapshot.attempts_exhausted() is True


def test_exhausted_guard_falls_back_to_default(ini_store):
    """Once exhausted, the plan discards view-state for the safe default.

    A game-view snapshot with a coach ply that keeps crashing must, after the
    guard trips, come up on the plain board (selection 0) -- the shipped
    behavior -- not keep re-selecting the poison ply. Regression manifestation:
    ignoring the guard re-applies the crashing state forever.
    """
    snapshot = SessionSnapshot(
        app_view=VIEW_GAME,
        analysis_selection=9,
        restore_attempts=MAX_RESTORE_ATTEMPTS,
    )

    plan = plan_startup(snapshot, has_resumable_game=True)

    assert plan.fell_back is True
    assert plan.resume_game is True
    assert plan.target_view == VIEW_GAME
    assert plan.analysis_selection == 0
    assert plan.suspend_after_resume is False


def test_exhausted_guard_no_game_skips_menu_path_restore(ini_store):
    """Exhausted fallback with no game shows the menu root, not the saved path.

    If the saved menu path itself triggers the crash, restoring it again would
    perpetuate the loop; the fallback must open the menu at its root.
    Regression manifestation: restoring the path under the tripped guard keeps
    crashing on the same submenu.
    """
    snapshot = SessionSnapshot(
        app_view=VIEW_MENU, restore_attempts=MAX_RESTORE_ATTEMPTS
    )

    plan = plan_startup(snapshot, has_resumable_game=False)

    assert plan.fell_back is True
    assert plan.target_view == VIEW_MENU
    assert plan.restore_menu_path is False


# ---------------------------------------------------------------------------
# plan_startup decision matrix
# ---------------------------------------------------------------------------

def test_game_view_with_game_resumes_to_board_and_coach_ply():
    """Game view + resumable game restores the board and the saved coach ply.

    This is the "coach showing on the last move, come up exactly like that"
    case. Regression manifestation: losing analysis_selection here brings the
    game up on the plain board instead of the coach panel.
    """
    snapshot = SessionSnapshot(app_view=VIEW_GAME, analysis_selection=12)

    plan = plan_startup(snapshot, has_resumable_game=True)

    assert plan == StartupPlan(
        resume_game=True,
        suspend_after_resume=False,
        target_view=VIEW_GAME,
        analysis_selection=12,
        restore_menu_path=False,
        fell_back=False,
    )


def test_game_view_without_game_falls_to_menu():
    """Game view but no resumable game (it ended) shows the menu.

    Regression manifestation: resuming a non-existent game would leave the app
    on an empty/stale board instead of a usable menu.
    """
    snapshot = SessionSnapshot(app_view=VIEW_GAME, analysis_selection=5)

    plan = plan_startup(snapshot, has_resumable_game=False)

    assert plan.resume_game is False
    assert plan.target_view == VIEW_MENU
    assert plan.analysis_selection == 0
    assert plan.restore_menu_path is True


def test_menu_view_with_game_resumes_then_suspends():
    """Menu view + resumable game reproduces the paused-game-behind-menu state.

    This is the "menu showing with the game paused" case. The managers are
    rebuilt (so RESUME continues the game) but immediately suspended so the menu,
    not the board, is on screen. Regression manifestation: without
    suspend_after_resume the board would show; without resume_game the paused
    game would be unrecoverable.
    """
    snapshot = SessionSnapshot(app_view=VIEW_MENU)

    plan = plan_startup(snapshot, has_resumable_game=True)

    assert plan == StartupPlan(
        resume_game=True,
        suspend_after_resume=True,
        target_view=VIEW_MENU,
        analysis_selection=0,
        restore_menu_path=True,
        fell_back=False,
    )


def test_settings_view_with_game_treated_as_menu():
    """Settings view collapses to the menu-with-path restore behavior.

    Settings is the menu with a Settings-rooted path; the same resume+suspend
    logic applies. Regression manifestation: treating settings as a distinct,
    unhandled view would skip the game resume and drop the paused game.
    """
    snapshot = SessionSnapshot(app_view=VIEW_SETTINGS)

    plan = plan_startup(snapshot, has_resumable_game=True)

    assert plan.resume_game is True
    assert plan.suspend_after_resume is True
    assert plan.target_view == VIEW_MENU
    assert plan.restore_menu_path is True


def test_menu_view_without_game_shows_menu_at_saved_path():
    """Menu view + no game restores the menu at its saved navigation path.

    Regression manifestation: failing to set restore_menu_path drops the user at
    the menu root instead of the submenu they were in.
    """
    snapshot = SessionSnapshot(app_view=VIEW_MENU)

    plan = plan_startup(snapshot, has_resumable_game=False)

    assert plan.resume_game is False
    assert plan.suspend_after_resume is False
    assert plan.target_view == VIEW_MENU
    assert plan.restore_menu_path is True
    assert plan.fell_back is False


def test_fresh_snapshot_with_game_matches_shipped_default():
    """A default (fresh) snapshot with a game still resumes to the board.

    Guards backward compatibility: devices with no saved session must behave
    exactly as before (in-progress game -> board). Regression manifestation: a
    changed default would alter boot behavior for every existing device.
    """
    plan = plan_startup(SessionSnapshot(), has_resumable_game=True)

    assert plan.resume_game is True
    assert plan.target_view == VIEW_GAME
    assert plan.analysis_selection == 0
    assert plan.suspend_after_resume is False
