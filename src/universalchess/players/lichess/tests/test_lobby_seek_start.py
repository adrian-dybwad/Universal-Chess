"""Seek New Game must seek, whatever the Players slots happen to be set to.

Why these tests exist
---------------------
The lobby's New Game row only stashed a join and left ``_start_game_mode``
to build players from settings. With no slot set to Lichess that produced a
local game -- pressing Seek in the Lichess lobby started Player 1 against
Drawfish, with no seek posted at all. The two pieces that fix it are pure and
live here: the effective pairing a lobby start runs with, and the fact that
it is derived rather than saved.

How a regression manifests
--------------------------
A lobby start returns a pairing with no Lichess slot (so a local game begins),
overwrites the user's saved Players settings, or drops the human in favour of
two non-human slots.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess.lobby import (
    build_lichess_menu_entries,
    effective_lichess_players,
    handle_lichess_menu,
)


def _player(**overrides):
    base = dict(type="human", color="white", elo="1500", engine="")
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeConnection:
    """Stands in for LichessConnection, which the menu closes when it exits."""

    def __init__(self):
        self.client = object()
        self.closes = 0

    def close(self) -> int:
        self.closes += 1
        return 0


@pytest.mark.parametrize(
    "p1_type, p2_type, expected",
    [
        # The engine slot yields to Lichess; the human is always kept.
        ("human", "engine", ("human", "lichess")),
        ("engine", "human", ("lichess", "human")),
        # No human at all: slot 1 becomes the human, since White stays on
        # player 1's physical side of the board.
        ("engine", "engine", ("human", "lichess")),
        # Two humans: slot 2 is the one that yields, for the same reason.
        ("human", "human", ("human", "lichess")),
    ],
)
def test_a_lobby_start_derives_a_human_versus_lichess_pairing(
    p1_type, p2_type, expected
):
    """Every lobby start runs Human vs Lichess, whatever the slots were.

    Why this test exists: a Lichess game needs one slot on each side, and the
    lobby is an explicit request for one. Without the substitution the stashed
    join was ignored and a local game started instead.

    How a regression manifests: the returned pair contains no ``lichess`` (a
    local game starts and no seek is posted) or no ``human`` (nobody is left to
    play the moves on the board).
    """
    player1 = _player(type=p1_type)
    player2 = _player(type=p2_type)

    effective = effective_lichess_players(player1, player2, lobby_start=True)

    assert tuple(p.type for p in effective) == expected


def test_a_lobby_start_leaves_a_configured_lichess_pairing_untouched():
    """A pairing that is already Human vs Lichess must pass through as-is.

    Why this test exists: the substitution exists only to fill a missing slot.
    Rewriting a configured pairing would move the human to the other side of the
    board mid-setup.

    How a regression manifests: the slots come back swapped or copied, so the
    human changes sides or the colour control no longer describes the game.
    """
    player1 = _player(type="human", color="black")
    player2 = _player(type="lichess")

    effective = effective_lichess_players(player1, player2, lobby_start=True)

    assert effective == (player1, player2)


def test_a_start_that_is_not_from_the_lobby_keeps_the_configured_players():
    """PLAY outside the lobby must not be turned into a Lichess game.

    Why this test exists: the substitution is what makes the lobby's buttons
    unconditional, and it has to stay confined to them. Applying it to every
    start would make a plain Human vs Engine PLAY post a seek.

    How a regression manifests: a Lichess slot appears in a pairing the user
    configured as engine, so PLAY at the menu root seeks an opponent.
    """
    player1 = _player(type="human")
    player2 = _player(type="engine")

    effective = effective_lichess_players(player1, player2, lobby_start=False)

    assert effective == (player1, player2)


def test_the_substituted_pairing_does_not_edit_the_saved_settings():
    """The pairing is for this game only and must not persist.

    Why this test exists: the slots handed in are the live PlayerSettings that
    centaur.ini is written from. Mutating them would silently convert the user's
    saved engine opponent into a Lichess one, and it would survive the game.

    How a regression manifests: the original objects come back with
    ``type`` changed, so Players shows Lichess after one lobby game.
    """
    player1 = _player(type="engine", engine="drawfish")
    player2 = _player(type="engine", engine="stockfish")

    effective = effective_lichess_players(player1, player2, lobby_start=True)

    assert (player1.type, player2.type) == ("engine", "engine")
    assert effective[0] is not player1
    assert effective[1] is not player2
    # The substituted slots keep everything the original had but their type, so
    # the section they save to and the name shown on the board are unchanged.
    assert effective[1].engine == "stockfish"


def test_the_lobby_new_game_row_says_seek_new_game():
    """The row must name what it does: post a seek.

    Why this test exists: the row read "New Game", which is also what the row
    that starts a local game reads, and what it did depended on the Players
    slots. The label is the only thing on screen that distinguishes them.

    How a regression manifests: the label reverts to "New Game", so the board
    offers two rows with the same name and different effects.
    """
    entries = {entry.key: entry for entry in build_lichess_menu_entries("alice")}

    assert entries["NewGame"].label.replace("\n", " ") == "Seek New Game"


def test_the_seek_row_says_seek():
    """The submenu's last row posts the seek and must say so.

    Why: Seek New Game now opens settings; the action that posts must not
    still read Seek New Game. How a regression manifests: the label is New
    Game, so the board offers two rows with the same name.
    """
    from universalchess.players.lichess.lobby import build_lichess_seek_menu_entries

    entries = {entry.key: entry for entry in build_lichess_seek_menu_entries()}

    assert entries["Seek"].label == "Seek"


def test_play_inside_the_lobby_leaves_to_the_board():
    """PLAY leaves the lobby; it does not seek or join.

    Why this test exists: PLAY toggles menu and board. The lobby used to
    intercept it as a mixed Lichess start, so a press posted a seek or opened
    a leftover-game picker instead of resuming the suspended game.

    How a regression manifests: start_lichess_game_fn is called, or the result
    is START_GAME instead of PLAY.
    """
    started = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            return MenuSelection.from_key("PLAY")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda config: started.append(config) or True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert started == []
    assert getattr(result, "key", result) == "PLAY"
