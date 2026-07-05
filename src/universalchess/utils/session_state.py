"""Session view-state persistence and startup restoration decisions.

The database already restores an in-progress game's position, clocks and eval
history on boot, but it cannot express *which view the user was looking at*:
the plain board, a coach panel on a specific move, or the menu with the game
paused behind it. This module persists that view-state so a service restart or
shutdown brings the app back up in the exact same state, "like nothing
happened".

Two pieces live here:

- :class:`SessionSnapshot` -- the persisted view-state (which app view, which
  coach move, and a crash-loop guard counter). It shares the ``[SessionState]``
  section of ``centaur.ini`` with :class:`MenuContext` (menu navigation path),
  so the whole session is one on-disk concept.

- :func:`plan_startup` -- a pure function mapping ``(snapshot,
  has_incomplete_game)`` to a :class:`StartupPlan` describing what boot should
  do. Keeping the decision pure makes the branching (which is otherwise buried
  in the 700-line ``main()``) directly unit-testable without any hardware.

Crash-loop safety
-----------------
Because the systemd unit restarts the process ``on-failure`` and the snapshot is
written through on every state change, a snapshot that itself triggers a
startup crash could loop forever re-restoring the poison state. The
``restore_attempts`` counter guards against this: ``main()`` increments and
persists it *before* attempting a snapshot-driven restore, and resets it to 0
only after the app has run healthily for a stable period. Once the counter
reaches :data:`MAX_RESTORE_ATTEMPTS`, :func:`plan_startup` discards the
view-state and falls back to the shipped default (a resumed game comes up on the
plain board; otherwise the menu), which is a known-good state.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from universalchess.utils.settings_persistence import (
    load_int,
    load_str,
    save_setting,
)

SESSION_STATE_SECTION = "SessionState"

MAX_RESTORE_ATTEMPTS = 3
"""Snapshot-driven restores allowed before falling back to the safe default.

A genuine restore-triggered crash loops quickly through the systemd restart and
hits this bound within seconds; a healthy run resets the counter well before it.
"""

VIEW_NONE = "none"
VIEW_MENU = "menu"
VIEW_GAME = "game"
VIEW_SETTINGS = "settings"

# Views that represent an actually-recorded on-screen state. VIEW_NONE is the
# sentinel for "no session recorded" (fresh device, or a value that failed to
# parse); it drives the shipped default rather than a specific restoration, so a
# device upgraded mid-game still resumes to the board as it always has.
_VALID_VIEWS = frozenset({VIEW_MENU, VIEW_GAME, VIEW_SETTINGS})

# Views that represent "the menu is on screen" for restoration purposes. SETTINGS
# is the menu with a Settings-rooted navigation path; the path (owned by
# MenuContext) re-enters the exact submenu, so both collapse to the same menu
# restore behavior here.
_MENU_VIEWS = frozenset({VIEW_MENU, VIEW_SETTINGS})


@dataclass
class SessionSnapshot:
    """Persisted UI view-state for exact restoration across restarts.

    Attributes:
        app_view: What the user was looking at -- ``"menu"``, ``"game"`` or
            ``"settings"``. Stored verbatim (not collapsed) so the record stays
            truthful to what the app was actually doing. Defaults to
            ``"none"`` (unrecorded) so a freshly loaded snapshot with no saved
            value drives the shipped default instead of a specific restoration.
        game_db_id: Database id of the game currently being played/viewed, or 0
            when no game is current. Recorded explicitly (rather than inferred
            as "the most recent game") because a *finished* game has a non-NULL
            result and stays viewable for takebacks/pondering, so it must be
            resumed by id and cleared when the user dismisses it to the menu.
        analysis_selection: The selected analysis/coach index for a game view.
            0 is the board/eval view; N (>=1) is the coach panel on ply N. Only
            meaningful when ``app_view == "game"``.
        restore_attempts: Crash-loop guard counter (see module docstring).
    """

    app_view: str = VIEW_NONE
    game_db_id: int = 0
    analysis_selection: int = 0
    restore_attempts: int = 0
    _section: str = field(default=SESSION_STATE_SECTION, repr=False)
    _log: Optional[Any] = field(default=None, repr=False)

    def attempts_exhausted(self) -> bool:
        """Whether snapshot-driven restore has failed too many times.

        When True, :func:`plan_startup` discards the view-state and boots into a
        known-good default instead of re-applying the poison state.
        """
        return self.restore_attempts >= MAX_RESTORE_ATTEMPTS

    def save(self) -> None:
        """Persist the snapshot to the ``[SessionState]`` section.

        Broadcast is suppressed: this is internal boot/session bookkeeping, not a
        user-facing setting, so it must not spam web clients with
        ``settings_changed`` on every menu step or coach selection.
        """
        save_setting(self._section, "app_view", self.app_view, broadcast=False)
        save_setting(self._section, "game_db_id", self.game_db_id, broadcast=False)
        save_setting(
            self._section, "analysis_selection", self.analysis_selection, broadcast=False
        )
        save_setting(
            self._section, "restore_attempts", self.restore_attempts, broadcast=False
        )
        if self._log:
            self._log.debug(
                f"[SessionSnapshot] Saved: app_view={self.app_view}, "
                f"game_db_id={self.game_db_id}, "
                f"analysis_selection={self.analysis_selection}, "
                f"restore_attempts={self.restore_attempts}"
            )

    @classmethod
    def load(cls, section: str = SESSION_STATE_SECTION, log=None) -> "SessionSnapshot":
        """Load the snapshot from ``centaur.ini``.

        An absent value loads as ``"none"`` (unrecorded). A garbage value is
        also treated as unrecorded (with a warning), and a non-integer
        selection/counter falls back to 0 (handled by ``load_int``), so a
        corrupt or partially written section never prevents boot -- the worst
        case is the shipped default view.
        """
        app_view = load_str(section, "app_view", VIEW_NONE)
        if app_view != VIEW_NONE and app_view not in _VALID_VIEWS:
            if log:
                log.warning(
                    f"[SessionSnapshot] Unrecognized app_view '{app_view}', "
                    f"treating as unrecorded"
                )
            app_view = VIEW_NONE

        game_db_id = load_int(section, "game_db_id", 0)
        if game_db_id < 0:
            game_db_id = 0

        analysis_selection = load_int(section, "analysis_selection", 0)
        if analysis_selection < 0:
            analysis_selection = 0

        restore_attempts = load_int(section, "restore_attempts", 0)
        if restore_attempts < 0:
            restore_attempts = 0

        snapshot = cls(
            app_view=app_view,
            game_db_id=game_db_id,
            analysis_selection=analysis_selection,
            restore_attempts=restore_attempts,
            _section=section,
            _log=log,
        )
        if log:
            log.info(
                f"[SessionSnapshot] Loaded: app_view={app_view}, "
                f"game_db_id={game_db_id}, "
                f"analysis_selection={analysis_selection}, "
                f"restore_attempts={restore_attempts}"
            )
        return snapshot


@dataclass
class StartupPlan:
    """What boot should do to reproduce the persisted view-state.

    Produced purely from ``(snapshot, has_incomplete_game)`` so the decision is
    testable in isolation from the hardware-heavy ``main()``.

    Attributes:
        resume_game: Rebuild the in-progress game's managers from the database.
        suspend_after_resume: After resuming, immediately suspend the game back
            behind the menu (paused clock, board torn down) -- the "game paused,
            menu showing" state. Only set together with ``resume_game``.
        target_view: The top-level view to end on: ``"game"`` (board/coach on
            screen) or ``"menu"`` (full menu on screen).
        analysis_selection: Coach ply to re-select once the game screen is up
            (0 = plain board/eval view). Only meaningful with ``target_view ==
            "game"``.
        restore_menu_path: Re-enter the saved menu navigation path (owned by
            MenuContext) rather than starting at the menu root.
        fell_back: True when the snapshot was discarded (crash-loop guard
            tripped) and this plan is the safe shipped default rather than an
            exact restoration.
    """

    resume_game: bool
    suspend_after_resume: bool
    target_view: str
    analysis_selection: int
    restore_menu_path: bool
    fell_back: bool


def _default_plan(has_resumable_game: bool, fell_back: bool) -> StartupPlan:
    """The shipped default boot behavior, used for fresh boots and fallbacks.

    A resumable game resumes to the plain board (its historical behavior);
    otherwise the menu is shown. When reached as a crash-loop fallback
    (``fell_back=True``) the saved menu path is *not* restored, so a poison menu
    position cannot keep crashing the boot -- the menu opens at its root.
    """
    if has_resumable_game:
        return StartupPlan(
            resume_game=True,
            suspend_after_resume=False,
            target_view=VIEW_GAME,
            analysis_selection=0,
            restore_menu_path=False,
            fell_back=fell_back,
        )
    return StartupPlan(
        resume_game=False,
        suspend_after_resume=False,
        target_view=VIEW_MENU,
        analysis_selection=0,
        restore_menu_path=not fell_back,
        fell_back=fell_back,
    )


def plan_startup(snapshot: SessionSnapshot, has_resumable_game: bool) -> StartupPlan:
    """Decide how to restore the app view at startup.

    Pure decision function; performs no I/O and touches no hardware.

    Decision matrix (when the crash-loop guard has not tripped):

    - view ``game`` + a resumable game exists -> resume to the game screen and
      re-select the saved coach ply. The game may be in progress or finished;
      the resume implementation reproduces the game-over state from the stored
      result when the game is over.
    - view ``game`` + no resumable game (it was cleared) -> show the menu at its
      saved path.
    - view ``menu``/``settings`` + a resumable game exists -> resume the game's
      managers but suspend them behind the menu (the "paused game, menu showing"
      state), and restore the saved menu path.
    - view ``menu``/``settings`` + no game -> show the menu at its saved path.
    - unrecorded view (fresh device / unparseable) -> shipped default.

    When ``snapshot.attempts_exhausted()`` is True the view-state is discarded
    and the shipped default is returned (see :func:`_default_plan`).

    Args:
        snapshot: The persisted view-state.
        has_resumable_game: Whether a game can be resumed for this snapshot --
            for a recorded view, the game identified by ``snapshot.game_db_id``
            exists with at least one played move (any result); for an unrecorded
            view, the legacy "most recent in-progress game" exists.

    Returns:
        The :class:`StartupPlan` for ``main()`` to execute.
    """
    if snapshot.attempts_exhausted():
        return _default_plan(has_resumable_game, fell_back=True)

    if snapshot.app_view == VIEW_GAME:
        if has_resumable_game:
            return StartupPlan(
                resume_game=True,
                suspend_after_resume=False,
                target_view=VIEW_GAME,
                analysis_selection=snapshot.analysis_selection,
                restore_menu_path=False,
                fell_back=False,
            )
        # We were on the game screen but no resumable game remains (it ended or
        # was cleared). Fall back to the menu rather than an empty board.
        return StartupPlan(
            resume_game=False,
            suspend_after_resume=False,
            target_view=VIEW_MENU,
            analysis_selection=0,
            restore_menu_path=True,
            fell_back=False,
        )

    if snapshot.app_view in _MENU_VIEWS:
        if has_resumable_game:
            # A resumable game plus a menu view means the game was paused behind
            # the menu. Rebuild its managers so PLAY/RESUME continues it, but keep
            # it suspended so the menu (not the board) is what shows.
            return StartupPlan(
                resume_game=True,
                suspend_after_resume=True,
                target_view=VIEW_MENU,
                analysis_selection=0,
                restore_menu_path=True,
                fell_back=False,
            )
        return StartupPlan(
            resume_game=False,
            suspend_after_resume=False,
            target_view=VIEW_MENU,
            analysis_selection=0,
            restore_menu_path=True,
            fell_back=False,
        )

    # Unrecorded view (fresh device or unparseable): no exact state to restore,
    # so use the shipped default (resumable game -> board; otherwise menu).
    return _default_plan(has_resumable_game, fell_back=False)
