"""Joining an ongoing Lichess game must show the position before Join.

Why these tests exist
---------------------
Selecting a nowPlaying row used to start the game immediately. Catch-up then
asked for a physical setup while the remote clock was already running. The
Human needs the diagram first so the pieces can be placed, then Join.

A NEW seek must not silently attach those rows either: after an opponent
takes a 5+0 seek the poller used to pull leftover correspondence onto the
board. Seek New Game itself always seeks. PLAY leaves the lobby for the
board, the same as in any other menu.

How a regression manifests
--------------------------
Accept is never required (the list join starts the stream); BACK on the
diagram still returns the game id; Seek New Game lists nowPlaying; leftover
gameStart attaches during a NEW seek.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from universalchess.managers.menu import MenuSelection
from universalchess.players.lichess import LichessGameMode, LichessPlayer, LichessPlayerConfig
from universalchess.players.lichess.lobby import (
    confirm_ongoing_game,
    handle_lichess_menu,
    ongoing_position_confirm_entries,
    show_lichess_ongoing_games,
)

AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def _row(**overrides):
    row = {
        "id": "g1",
        "opponent": "Bob",
        "rating": 1500,
        "color": "white",
        "fen": AFTER_E4,
        "lastMove": "e2e4",
        "isMyTurn": False,
    }
    row.update(overrides)
    return row


class _ScriptedMenu:
    def __init__(self, keys):
        self._keys = list(keys)
        self.shown = []

    def show_menu(self, entries, initial_index=0, **kwargs):
        self.shown.append(entries)
        key = self._keys.pop(0)
        return MenuSelection.from_key(key)


def _lichess_now_playing(**overrides):
    game = {
        "gameId": "g1",
        "opponent": {"username": "Bob", "rating": 1500},
        "color": "white",
        "fen": AFTER_E4,
    }
    game.update(overrides)
    return game


class _Games:
    def __init__(self, games):
        self._games = games

    def get_ongoing(self, count=10):
        return self._games


class _Connection:
    def __init__(self, games):
        self.client = SimpleNamespace(games=_Games(games))
        self.closes = 0

    def close(self):
        self.closes += 1


_LEFTOVER = {
    "gameId": "arD6VE0v",
    "opponent": {"username": "adriantest", "rating": 1500},
    "color": "black",
    "fen": AFTER_E4,
    "lastMove": "h7h5",
}


def test_position_confirm_lists_the_diagram_then_join():
    """The confirm screen is the position (non-selectable) then Join.

    How a regression manifests: Join is missing, the diagram row is
    selectable (TICK joins without a second look), or the opponent is dropped.
    """
    entries = ongoing_position_confirm_entries(_row())
    assert [e.key for e in entries] == ["position", "Join"]
    assert entries[0].selectable is False
    assert "Bob" in entries[0].label.replace("\n", " ")
    assert entries[1].selectable is True


def test_ongoing_list_join_requires_position_confirm():
    """TICK on a list row must show the diagram; Join then returns the id.

    How a regression manifests: the first TICK on g1 returns g1 without Join,
    so the clock starts before the pieces are set.
    """

    menu = _ScriptedMenu(["g1", "Join"])
    assert (
        show_lichess_ongoing_games(
            SimpleNamespace(games=_Games([_lichess_now_playing()])),
            menu,
            MagicMock(),
        )
        == "g1"
    )
    assert [[e.key for e in entries] for entries in menu.shown] == [
        ["g1"],
        ["position", "Join"],
    ]


def test_ongoing_list_back_on_diagram_returns_to_the_list():
    """BACK on the diagram must not join; a second BACK leaves the list.

    How a regression manifests: BACK on the diagram still returns the game id,
    or the list is not shown again and the user is dumped out after one peek.
    """

    menu = _ScriptedMenu(["g1", "BACK", "BACK"])
    assert (
        show_lichess_ongoing_games(
            SimpleNamespace(games=_Games([_lichess_now_playing()])),
            menu,
            MagicMock(),
        )
        is None
    )
    assert len(menu.shown) == 3
    assert [e.key for e in menu.shown[0]] == ["g1"]
    assert [e.key for e in menu.shown[1]] == ["position", "Join"]
    assert [e.key for e in menu.shown[2]] == ["g1"]


def test_position_confirm_accept_returns_true_and_back_returns_false():
    """Accept joins; BACK returns to the list without starting the stream.

    How a regression manifests: BACK still returns True so the game starts, or
    Accept returns False so Join never attaches.
    """
    accept = _ScriptedMenu(["Join"])
    assert confirm_ongoing_game(accept, _row()) is True
    back = _ScriptedMenu(["BACK"])
    assert confirm_ongoing_game(back, _row()) is False


def test_lobby_new_game_seeks_even_when_now_playing_exists():
    """Seek New Game posts a seek; leftover nowPlaying is not listed here.

    Ongoing Games is the explicit list. How a regression manifests: start is
    ONGOING, or show_menu lists leftover ids.
    """
    started = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            keys = {e.key for e in build_entries()}
            if "Seek" in keys:
                return handle_selection(MenuSelection.from_key("Seek"))
            return handle_selection(MenuSelection.from_key("NewGame"))

        def show_menu(self, entries, initial_index=0, **kwargs):
            raise AssertionError("Seek New Game must not list ongoing games")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_Connection([_LEFTOVER]), "adriandyb", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda config: started.append(config) or True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result == "START_GAME"
    assert len(started) == 1
    assert started[0].mode is LichessGameMode.NEW


def test_play_in_the_lobby_does_not_join_or_seek():
    """PLAY leaves the lobby; unfinished games are Ongoing Games, not PLAY.

    Why: PLAY toggles menu and board. Intercepting it to join leftover
    correspondence (arD6VE0v) started a remote game when the user wanted
    the suspended local board. How a regression manifests: a join or seek
    is stashed, or the result is START_GAME.
    """
    started = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            return MenuSelection.from_key("PLAY")

        def show_menu(self, entries, initial_index=0, **kwargs):
            raise AssertionError("PLAY must not open a Lichess picker")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_Connection([_LEFTOVER]), "adriandyb", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda config: started.append(config) or True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert started == []
    assert getattr(result, "key", result) == "PLAY"


def test_new_seek_does_not_attach_a_preexisting_ongoing_game():
    """gameStart for a game that was already nowPlaying must not start the stream.

    Why: opening /api/stream/event dumps gameStart for leftover correspondence.
    How a regression manifests: _game_id becomes the leftover id after a NEW
    seek records that id as preexisting.
    """
    player = LichessPlayer(LichessPlayerConfig(mode=LichessGameMode.NEW))
    player._username = "adriandyb"
    player._client = MagicMock()
    player._start_game_stream = MagicMock()
    player._preexisting_game_ids = {"arD6VE0v"}
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "arD6VE0v"}}
    )
    assert player._game_id is None
    player._start_game_stream.assert_not_called()
    player._client.games.get_ongoing.return_value = [{"gameId": "arD6VE0v"}]
    assert player._try_attach_ongoing_game() is False
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "new-from-seek"}}
    )
    assert player._game_id == "new-from-seek"
    player._start_game_stream.assert_called_once()


def test_start_new_game_records_preexisting_ids_before_listening():
    """A NEW seek snapshots nowPlaying before the event stream opens.

    How a regression manifests: _listen_for_match runs while
    _preexisting_game_ids is empty, so leftover gameStart attaches.
    """
    player = LichessPlayer(LichessPlayerConfig(mode=LichessGameMode.NEW))
    player._username = "adriandyb"
    player._client = MagicMock()
    player._start_game_stream = MagicMock()
    player._client.games.get_ongoing.return_value = [{"gameId": "arD6VE0v"}]
    seen = {}

    def listen():
        seen["ids"] = set(player._preexisting_game_ids)

    player._listen_for_match = listen
    player._seek_game_thread = MagicMock()
    assert player._start_new_game() is True
    assert seen["ids"] == {"arD6VE0v"}
