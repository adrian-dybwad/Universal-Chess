"""Board-reset during a Lichess game must ask before posting a new seek.

Why these tests exist
---------------------
PLAY, lobby New Game, and web New Game are explicit and seek immediately.
Setting the pieces back to the start is not: it rebuilds through
``_start_game_mode`` with no join stash, which posts a new seek. That
implicit path must ask what to do (Cancel default) and leave the game without
seeking when the user declines. Seeking is not the only thing wanted there --
an ongoing game, a challenge, or a different account are all in the lobby --
so the lobby is offered beside it.
"""

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    back_cancels_unready_game_start,
    board_reset_rebuild_action,
    choose_lichess_reset_action,
    explicit_lichess_seek_join,
    skip_unsolicited_lichess_start,
)
from universalchess.players.lichess.player import LichessGameMode


class _ScriptedMenuManager:
    """Records show_menu entries and the highlight, then returns queued results."""

    def __init__(self, show_results=None):
        self.show_results = list(show_results or [])
        self.shown = []
        self.show_initial_indexes = []

    def show_menu(self, entries, initial_index=0, on_index_change=None):
        self.shown.append(entries)
        self.show_initial_indexes.append(initial_index)
        return self.show_results.pop(0)


def test_reset_action_follows_the_row_that_was_chosen():
    """Each row means one thing; BACK means the same as Cancel.

    Why: a stray TICK on the prompt must not register a Lichess seek, and the
    lobby row must not be read as one either. How the regression manifests:
    Cancel, BACK, or Lobby returns seek, or Lobby is answered as a cancel so
    the lobby never opens.
    """
    for key, expected in (
        ("Lobby", "lobby"),
        ("Seek", "seek"),
        ("Cancel", "cancel"),
        ("BACK", "cancel"),
    ):
        assert choose_lichess_reset_action(
            _ScriptedMenuManager(show_results=[MenuSelection.from_key(key)])
        ) == expected


def test_reset_prompt_lists_lobby_then_seek_then_cancel_and_highlights_cancel():
    """The lobby comes first, the seek second, Cancel last and highlighted.

    Why: resetting the pieces most often means picking up something else --
    an ongoing game or a challenge -- and posting a seek is never what an
    accidental TICK should do. How the regression manifests: the rows come
    back in another order, the lobby row is missing, the prompt becomes
    selectable, or the highlight lands on a row that acts.
    """
    manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    choose_lichess_reset_action(manager)
    entries = manager.shown[0]
    assert [e.key for e in entries] == ["prompt", "Lobby", "Seek", "Cancel"]
    assert [e.label.replace("\n", " ") for e in entries[1:]] == [
        "Lichess Lobby",
        "Seek New Game",
        "Cancel",
    ]
    assert entries[0].selectable is False
    assert manager.show_initial_indexes == [3]


def test_board_reset_rebuild_skips_confirm_when_not_lichess():
    """Engine/human board-reset rebuilds without a seek prompt.

    Why: that path does not post a Lichess seek. How the regression manifests:
    show_menu is called and a local new game waits on Seek/Cancel.
    """
    manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    assert board_reset_rebuild_action(manager, is_lichess=False) == "rebuild"
    assert manager.shown == []


def test_board_reset_rebuild_seeks_only_after_seek_choice():
    """A Lichess board-reset rebuilds only when Seek is chosen.

    Why: setting the pieces to start used to seek with no prompt. How the
    regression manifests: Cancel still returns seek, so a new seek is posted,
    or the lobby choice seeks instead of opening the lobby.
    """
    seek = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Seek")])
    cancel = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    lobby = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Lobby")])
    assert board_reset_rebuild_action(seek, is_lichess=True) == "seek"
    assert board_reset_rebuild_action(cancel, is_lichess=True) == "menu"
    assert board_reset_rebuild_action(lobby, is_lichess=True) == "lobby"


def test_board_reset_rebuild_without_menu_does_not_seek():
    """If the confirmation cannot be shown, do not post a seek.

    Why: a missing menu manager would otherwise fall through to _start_game_mode
    and seek. How the regression manifests: None still returns rebuild.
    """
    assert board_reset_rebuild_action(None, is_lichess=True) == "menu"


def test_explicit_lichess_seek_join_is_mode_new():
    """PLAY / New Game / confirmed board-reset stash this join so start() seeks.

    Why: join None now means ATTACH. How the regression manifests: the stash
    omits mode NEW and a PLAY start watches without posting a seek.
    """
    join = explicit_lichess_seek_join()
    assert join["mode"] is LichessGameMode.NEW
    assert join["game_id"] == ""
    assert join["challenge_id"] == ""


def test_skip_unsolicited_lichess_start_only_when_lichess_without_join_or_play():
    """Piece lift and client connect must not enter a Lichess seek.

    Why: those paths called _enter_game with join None, which posted a seek.
    PLAY sets explicit_seek; lobby New Game already has a join.

    How the regression manifests: piece lift with a Lichess slot is not skipped,
    or PLAY / an existing join is skipped and the menu never starts.
    """
    assert skip_unsolicited_lichess_start(
        is_lichess=True, join=None, explicit_seek=False
    ) is True
    assert skip_unsolicited_lichess_start(
        is_lichess=True, join=None, explicit_seek=True
    ) is False
    assert skip_unsolicited_lichess_start(
        is_lichess=True, join=explicit_lichess_seek_join(), explicit_seek=False
    ) is False
    assert skip_unsolicited_lichess_start(
        is_lichess=False, join=None, explicit_seek=False
    ) is False


def test_back_cancels_unready_game_start_before_managers_exist():
    """BACK on the waiting splash must cancel even before GameManager exists.

    Why: the splash is painted first. GAME keys with no controller were
    logged unhandled, then start() posted the seek anyway.

    How the regression manifests: BACK before managers exist returns False,
    so the start path still seeks.
    """
    assert back_cancels_unready_game_start(has_game_managers=False) is True
    assert back_cancels_unready_game_start(has_game_managers=True) is False
