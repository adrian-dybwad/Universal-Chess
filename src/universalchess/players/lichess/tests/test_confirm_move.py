"""Correspondence moves must be confirmed before they are posted to Lichess.

Why these tests exist
---------------------
The Lichess website asks to confirm every correspondence ply. The board was
posting as soon as the piece landed, so a misdrop was already on the server
and could only be undone with a takeback offer. Confirm (checkmark, default)
submits; X or BACK must not post and must rewind the local ply so the pieces
can go back.

How a regression manifests
--------------------------
``make_move`` is called before the dialog, or cancel still posts, or a timed
game waits on the same dialog, or cancel never rewinds so the logical board
keeps a ply Lichess never received.
"""

from unittest.mock import MagicMock

import chess

from universalchess.managers.menu import MenuSelection
from universalchess.players.base import PlayerState
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer
from universalchess.players.lichess.session import (
    LichessPlaySession,
    confirm_move_menu_entries,
)


def _ready_lichess(*, correspondence: bool, color=chess.BLACK) -> LichessPlayer:
    player = LichessPlayer()
    player._set_state(PlayerState.READY)
    player.color = color
    player._game_id = "g1"
    player._client = MagicMock()
    player._speed = "correspondence" if correspondence else "blitz"
    return player


def _board_after_e4() -> chess.Board:
    board = chess.Board()
    board.push_uci("e2e4")
    return board


def test_confirm_move_entries_are_check_then_x_stacked():
    """Two normal selectable rows: check (Confirm Move) then X (Cancel).

    Why: icon-only or a non-selectable header made TICK/UP/DOWN unlike every
    other in-game overlay. How a regression manifests: a prompt row, empty
    labels, or selectable=False on confirm/cancel.
    """
    entries = confirm_move_menu_entries()
    assert [e.key for e in entries] == ["confirm", "cancel"]
    confirm, cancel = entries
    assert confirm.icon_name == "check"
    assert cancel.icon_name == "cancel"
    assert confirm.selectable is True
    assert cancel.selectable is True
    assert confirm.label
    assert cancel.label
    assert confirm.row is None
    assert cancel.row is None


def test_timed_game_posts_without_a_confirm_dialog():
    """Blitz/rapid/classical must still post as soon as the piece lands.

    How a regression manifests: the confirm callback is invoked on a timed
    game, so every live ply waits on the e-paper.
    """
    player = _ready_lichess(correspondence=False)
    asked = []
    player.set_confirm_move_callback(lambda move, remaining: asked.append((move, remaining)) or True)
    player.on_move_made(chess.Move.from_uci("e2e4"), _board_after_e4())
    player._client.board.make_move.assert_called_once_with("g1", "e2e4")
    assert asked == []


def test_correspondence_posts_only_after_confirm():
    """Confirm must run before ``make_move``; declining must not post.

    How a regression manifests: make_move is called even when the callback
    returns False, or is never called when it returns True.
    """
    player = _ready_lichess(correspondence=True)
    player.set_confirm_move_callback(lambda move, remaining: False)
    player.on_move_made(chess.Move.from_uci("e2e4"), _board_after_e4())
    player._client.board.make_move.assert_not_called()

    player = _ready_lichess(correspondence=True)
    seen = []

    def confirm(move, remaining):
        seen.append((move.uci(), remaining))
        return True

    player.set_confirm_move_callback(confirm)
    player.on_move_made(chess.Move.from_uci("e2e4"), _board_after_e4())
    player._client.board.make_move.assert_called_once_with("g1", "e2e4")
    assert seen == [("e2e4", 0)]


def test_opponent_ply_is_not_confirmed_or_posted():
    """A transcribed Lichess move must not be echoed or confirmed.

    After e2e4 it is Black's turn. When Lichess is White, that ply was ours
    from the server. How a regression manifests: make_move is called for
    the opponent's transcribed move.
    """
    player = _ready_lichess(correspondence=True, color=chess.WHITE)
    asked = []
    player.set_confirm_move_callback(lambda move, remaining: asked.append(True) or True)
    player.on_move_made(chess.Move.from_uci("e2e4"), _board_after_e4())
    player._client.board.make_move.assert_not_called()
    assert asked == []


def test_session_confirm_submits_and_restores_the_board():
    """TICK on the checkmark posts; the panel must return to the game widgets.

    How a regression manifests: show_menu is never opened, or confirm returns
    False so the ply is rewound, or the e-paper stays on the empty post-menu frame.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("confirm")
    display = MagicMock()
    rewound = []
    session.attach(
        player_manager=MagicMock(),
        game_display=display,
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        rewind_to_move_count=lambda n: rewound.append(n),
    )
    session._started_splash_held = False
    assert session._confirm_correspondence_move(chess.Move.from_uci("e2e4"), 0) is True
    keys = [e.key for e in menu.show_menu.call_args[0][0]]
    assert keys == ["confirm", "cancel"]
    assert menu.show_menu.call_args.kwargs.get("initial_index") == 0
    display.show_game_widgets.assert_called_once()
    display._pause_clock_for_menu.assert_called_once()
    display._resume_clock_after_menu.assert_called_once()
    assert rewound == []


def test_session_confirm_pauses_the_clock_before_the_overlay():
    """A counting clock must pause before show_menu clears the clock widget.

    Why: clock-driven refresh plus no clock widget leaves selection redraws
    queued forever, so UP/DOWN look dead. How a regression manifests: pause is
    never called, or it is called after show_menu (the overlay is already frozen).
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    display = MagicMock()
    paused = []

    def show_menu(*_args, **_kwargs):
        paused.append(display._pause_clock_for_menu.call_count)
        return MenuSelection.from_key("cancel")

    menu = MagicMock()
    menu.show_menu.side_effect = show_menu
    session.attach(
        player_manager=MagicMock(),
        game_display=display,
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        rewind_to_move_count=lambda *_: None,
    )
    session._started_splash_held = False
    session._confirm_correspondence_move(chess.Move.from_uci("e2e4"), 0)
    assert paused == [1]
    display._resume_clock_after_menu.assert_called_once()


def test_session_cancel_rewinds_the_local_ply():
    """X or BACK must undo the ply Lichess never received.

    remaining_plies is the stack length after the cancelled move is popped.
    How a regression manifests: rewind is not called, so the logical board
    keeps e2e4 while Lichess still has the previous position.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("cancel")
    rewound = []
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        rewind_to_move_count=lambda n: rewound.append(n),
    )
    session._started_splash_held = False
    assert session._confirm_correspondence_move(chess.Move.from_uci("e2e4"), 0) is False
    assert rewound == [0]


def test_session_back_is_the_same_as_cancel():
    """BACK is cancel, matching every other in-game overlay.

    How a regression manifests: BACK is treated as confirm and the ply is posted.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("BACK")
    rewound = []
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        rewind_to_move_count=lambda n: rewound.append(n),
    )
    session._started_splash_held = False
    assert session._confirm_correspondence_move(chess.Move.from_uci("e2e4"), 3) is False
    assert rewound == [3]


def test_attach_wires_the_confirm_callback():
    """The session must be the confirm hook so a correspondence ply opens the dialog.

    How a regression manifests: attach never sets the callback, so the player
    posts immediately in a real game.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    assert remote._confirm_move_callback == session._confirm_correspondence_move
