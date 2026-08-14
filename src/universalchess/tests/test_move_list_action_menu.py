"""Tests for the move-list long-press-OK menu (take back / new game from a ply).

Why these tests exist
---------------------
UP/DOWN during a game highlights a played move. A short OK still pages coach
text or forces a full refresh; a long-press OK (LONG_TICK) must open a menu
with "Take back to this position" and "New game from this position" without
stealing that short press. These tests pin the menu rows, when takeback is
disabled (already at the live tip, or players that cannot take back), that
review mode is detected so LONG_TICK can open the overlay, that closing
the overlay resumes the clock for takeback (play continues) but not for
new_game (the current game is torn down), and that New game copies the
moves through the highlighted ply rather than starting from a bare FEN.
"""

import sys
import types
from unittest.mock import MagicMock

# Hardware/Linux-only modules must be mocked before importing display.py.
for _mod in ["spidev", "RPi", "RPi.GPIO", "gpiozero", "smbus", "smbus2", "bluetooth"]:
    sys.modules[_mod] = MagicMock()
for _mod in ["serial", "serial.tools", "serial.tools.list_ports"]:
    sys.modules[_mod] = MagicMock()

_board_pkg = types.ModuleType("DGTCentaurMods.board")
_board_pkg.board = MagicMock()
_board_pkg.centaur = MagicMock()
sys.modules["DGTCentaurMods.board"] = _board_pkg
sys.modules["DGTCentaurMods.board.board"] = _board_pkg.board
sys.modules["DGTCentaurMods.board.centaur"] = _board_pkg.centaur
sys.modules["DGTCentaurMods.board.logging"] = MagicMock()
sys.modules["DGTCentaurMods.board.settings"] = MagicMock()

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.display import DisplayManager
from universalchess.menus.move_list_menu import (
    build_move_list_action_entries,
    history_to_transfer,
    players_support_takeback,
    should_open_move_list_action_menu,
    snapshot_analyses_for_positions,
    takeback_is_available,
)


class _FakeAnalysis:
    """Analysis widget stand-in reporting a fixed selected ply."""

    def __init__(self, ply=None):
        self._ply = ply

    def selected_ply(self):
        return self._ply


class _FakeMenu:
    """Minimal stand-in for a non-blocking menu widget."""

    def __init__(self, selection_result: str):
        self._selection_result = selection_result
        self.deactivated = False

    def deactivate(self) -> None:
        self.deactivated = True


def _bare_display_manager():
    """DisplayManager with only the attributes the methods under test use."""
    manager = object.__new__(DisplayManager)
    manager.analysis_widget = None
    manager._clock = MagicMock()
    manager._clock_paused_for_menu = False
    manager._menu_active = False
    manager._current_menu = None
    manager._menu_result_callback = None
    manager._draw_offer_resolver = None
    manager._sync_clock_refresh_mode = MagicMock()
    return manager


# --- Menu rows --------------------------------------------------------------


def test_menu_offers_takeback_new_game_and_cancel():
    """Long-press OK on a highlighted move must present both actions plus Cancel.

    Why: the requested overlay is take-back-to-this-ply and new-game-from-this-
    ply; Cancel (and BACK) dismisses without changing the game, matching every
    other in-game overlay. How a regression manifests: a missing key, so the
    overlay cannot dispatch that action, or Cancel disappearing so the only
    way out is BACK (which the finalize path already maps, but the row is the
    discoverable affordance).
    """
    entries = build_move_list_action_entries(takeback_enabled=True)
    keys = [e.key for e in entries]
    assert keys == ["takeback", "new_game", "cancel"]
    assert all(isinstance(e, IconMenuEntry) for e in entries)


def test_takeback_row_disabled_when_already_at_the_live_tip():
    """Take back is unavailable when the highlighted ply is already the last move.

    Why: there are no later moves to undo; enabling the row would look like it
    undoes the highlighted move itself. How a regression manifests: the
    takeback entry is enabled, so selecting it would call takeback_to_ply as a
    no-op and appear broken.
    """
    entries = build_move_list_action_entries(takeback_enabled=False)
    by_key = {e.key: e for e in entries}
    assert by_key["takeback"].enabled is False
    assert by_key["new_game"].enabled is True
    assert by_key["cancel"].enabled is True


def test_new_game_row_stays_enabled_when_takeback_is_not():
    """Forking from the live tip is valid even when takeback is a no-op.

    Why: "New game from this position" at the last ply still leaves the old
    game resumable and starts a fresh one from the current board. Disabling it
    together with takeback would hide the only useful action. How a regression
    manifests: new_game.enabled is False whenever takeback is.
    """
    entries = build_move_list_action_entries(takeback_enabled=False)
    assert {e.key: e.enabled for e in entries}["new_game"] is True


def test_takeback_available_only_when_later_moves_exist_and_players_allow_it():
    """Take back is offered only for a ply behind the tip on a local game.

    Why: highlighting the last move has nothing to undo; Lichess (and any
    player that reports supports_takeback False) cannot take back from this
    board. How a regression manifests: True for ply==num_plies (a no-op row)
    or True when supports_takeback is False (an action that cannot complete).
    """
    assert takeback_is_available(selected_ply=2, num_plies=5, supports_takeback=True) is True
    assert takeback_is_available(selected_ply=5, num_plies=5, supports_takeback=True) is False
    assert takeback_is_available(selected_ply=2, num_plies=5, supports_takeback=False) is False
    assert takeback_is_available(selected_ply=None, num_plies=5, supports_takeback=True) is False
    assert takeback_is_available(selected_ply=0, num_plies=5, supports_takeback=True) is False


def test_players_support_takeback_reads_bool_property_not_a_method():
    """PlayerManager.supports_takeback is a @property; it must not be called.

    Why: LONG_TICK used to call player_manager.supports_takeback(), which
    raises TypeError: 'bool' object is not callable. The events thread
    catches that, so the overlay never opens after a held OK. How a
    regression manifests: this helper raises TypeError on a property-shaped
    object, or returns True for a manager whose property is False.
    """
    class _AllowsTakeback:
        supports_takeback = True

    class _ForbidsTakeback:
        supports_takeback = False

    assert players_support_takeback(_AllowsTakeback()) is True
    assert players_support_takeback(_ForbidsTakeback()) is False
    assert players_support_takeback(None) is True


_FOUR_PLY = [
    {"fen": "start-fen", "san": None, "uci": None},
    {"fen": "after-e4", "san": "e4", "uci": "e2e4"},
    {"fen": "after-e5", "san": "e5", "uci": "e7e5"},
    {"fen": "after-nf3", "san": "Nf3", "uci": "g1f3"},
    {"fen": "after-nc6", "san": "Nc6", "uci": "b8c6"},
]


def test_history_to_transfer_keeps_moves_through_highlighted_ply():
    """New game from a highlighted ply copies the opening plus moves 1..ply.

    Why: the overlay used to start from that ply's FEN with an empty history,
    so the new game had no PGN and no database row until a later move was
    played. How a regression manifests: only the last UCI, an empty list, or
    the FEN after ply 2 as the start instead of the opening.
    """
    start, moves = history_to_transfer(positions=_FOUR_PLY, ply=2)
    assert start == "start-fen"
    assert moves == ["e2e4", "e7e5"]


def test_history_to_transfer_includes_the_live_tip():
    """Forking at the last move still copies the full history into a new game.

    Why: highlighting the tip is a valid new-game target (unlike takeback,
    which has nothing to undo). How a regression manifests: None for
    ply == len(positions)-1, so no recorded game is created from the tip.
    """
    start, moves = history_to_transfer(positions=_FOUR_PLY, ply=4)
    assert start == "start-fen"
    assert moves == ["e2e4", "e7e5", "g1f3", "b8c6"]


def test_history_to_transfer_rejects_the_opening_and_out_of_range():
    """Ply 0 is the start (no moves); out-of-range must not invent a sequence.

    Why: create_game_from_moves rejects an empty list, and a fabricated UCI
    would persist a game that cannot resume. How a regression manifests: a
    (fen, []) tuple for ply 0, or a truncated list for ply 99.
    """
    assert history_to_transfer(positions=_FOUR_PLY, ply=0) is None
    assert history_to_transfer(positions=_FOUR_PLY, ply=5) is None
    assert history_to_transfer(positions=_FOUR_PLY, ply=-1) is None
    assert history_to_transfer(positions=[], ply=1) is None


def test_history_to_transfer_rejects_a_ply_with_no_uci():
    """A history entry without UCI cannot be transferred.

    Why: the start row has uci None; if a later row did too, persisting it
    would fail resume. How a regression manifests: the None is skipped and a
    shorter move list is returned as if that ply never happened.
    """
    broken = [
        {"fen": "start-fen", "san": None, "uci": None},
        {"fen": "after-e4", "san": "e4", "uci": "e2e4"},
        {"fen": "mystery", "san": "??", "uci": None},
    ]
    assert history_to_transfer(positions=broken, ply=2) is None


def test_snapshot_analyses_prefers_live_cache_and_falls_back_to_stored():
    """Forked-game evals come from the live cache, then the source game's rows.

    Why: starting the new game resets AnalysisService, so the graph is restored
    only from what is persisted on the new rows. A just-finished search may
    not have been backfilled on the source game yet (live cache wins); a ply
    already written to the source game and evicted from the cache is recovered
    from stored rows. How a regression manifests: live 30 is overwritten by
    stored 99, or after-e5 is missing because it was only in storage.
    """
    live = {"after-e4": "live-e4"}
    stored = {"after-e4": "stored-e4", "after-e5": "stored-e5"}
    snapshot = snapshot_analyses_for_positions(
        positions=_FOUR_PLY,
        ply=2,
        live_lookup=live.get,
        stored_lookup=stored.get,
    )
    assert snapshot.get("start-fen") is None
    assert snapshot["after-e4"] == "live-e4"
    assert snapshot["after-e5"] == "stored-e5"
    assert "after-nf3" not in snapshot


def test_long_tick_opens_the_menu_only_while_reviewing_a_move():
    """A short OK must not open the overlay; a long OK must, and only in review.

    Why: short TICK still pages coach text / refreshes. LONG_TICK outside
    review is a no-op (other keys' long-press abort is unchanged). How a
    regression manifests: True for a short press (steals paging/refresh) or
    True when no ply is highlighted (opens an empty-target menu).
    """
    assert should_open_move_list_action_menu(reviewing=True, long_tick=True) is True
    assert should_open_move_list_action_menu(reviewing=True, long_tick=False) is False
    assert should_open_move_list_action_menu(reviewing=False, long_tick=True) is False
    assert should_open_move_list_action_menu(reviewing=False, long_tick=False) is False


# --- When a ply is highlighted (review mode) --------------------------------


def test_move_review_active_when_a_ply_is_highlighted():
    """Review mode is a highlighted played move, not the eval/board view.

    Why: LONG_TICK opens the overlay only in review; selection 0 is where
    short OK still means full refresh (or paging a hint). How a regression
    manifests: is_move_review_active is False for ply 3, so the key handler
    never opens the overlay.
    """
    manager = _bare_display_manager()
    manager.analysis_widget = _FakeAnalysis(ply=3)
    assert manager.is_move_review_active() is True


def test_move_review_inactive_on_the_analysis_view():
    """The eval/board view is not move-list navigation.

    Why: LONG_TICK there must stay a no-op so it does not open an empty-target
    menu; short OK still pages coach/hint text or forces a full refresh. How a
    regression manifests: is_move_review_active is True when selected_ply is
    None.
    """
    manager = _bare_display_manager()
    manager.analysis_widget = _FakeAnalysis(ply=None)
    assert manager.is_move_review_active() is False


def test_move_review_inactive_without_analysis_widget():
    """Layouts without a move list have no review mode.

    Why: a clock-only or board-only layout must not claim a highlighted ply.
    How a regression manifests: is_move_review_active raises or returns True
    when analysis_widget is None.
    """
    manager = _bare_display_manager()
    manager.analysis_widget = None
    assert manager.is_move_review_active() is False


# --- Overlay close: clock and board rebuild --------------------------------


def _finalize(manager, selection_result, shutdown_result="exit"):
    events = []
    results = []
    manager._init_widgets = lambda: events.append("init")
    manager._clock.resume = MagicMock(side_effect=lambda: events.append("resume"))

    def _callback(result):
        events.append("callback")
        results.append(result)

    manager._menu_result_callback = _callback
    menu = _FakeMenu(selection_result)
    manager._menu_active = True
    manager._current_menu = menu
    manager._clock_paused_for_menu = True
    manager._finalize_menu_selection(menu, shutdown_result=shutdown_result)
    return events, results


def test_takeback_rebuilds_board_and_resumes_clock():
    """Take back continues the same game, so the clock must run again.

    Why: the overlay pauses the clock (same as resign/draw) so navigation is
    not charged to the player on move. Takeback returns to live play on the
    truncated game; leaving the clock paused would freeze both the countdown
    and later widget refreshes. How a regression manifests: no "resume" event,
    or "init" missing so the board stays as the overlay.
    """
    events, results = _finalize(_bare_display_manager(), "takeback")
    assert events == ["init", "resume", "callback"]
    assert results == ["takeback"]


def test_new_game_skips_board_rebuild_and_does_not_resume_clock():
    """New game tears the current managers down, so rebuilding them is wasted.

    Why: the callback starts a fresh game (cleanup + new DisplayManager).
    Rebuilding the old board would flash the discarded position, and resuming
    the old clock would restart a countdown about to be destroyed. How a
    regression manifests: an "init" or "resume" event for new_game.
    """
    events, results = _finalize(_bare_display_manager(), "new_game")
    assert "init" not in events
    assert "resume" not in events
    assert events == ["callback"]
    assert results == ["new_game"]
