"""ProtocolManager talks to remote-session capabilities, not LichessPlayer.

Why these tests exist
---------------------
set_player_manager imported LichessPlayer and called set_clock_callback /
set_game_info_callback only after isinstance. stop_lichess was start/stop
players under a provider name. is_lichess_connected read self.is_lichess,
which was never assigned.

How a regression manifests
--------------------------
protocol.py imports the Lichess plugin; clock updates never reach GameManager;
stop_lichess / CLIENT_LICHESS return, so main keeps a provider-named API.
"""

import inspect
from unittest.mock import MagicMock

from universalchess.managers.protocol import ProtocolManager
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer
from universalchess.players.manager import PlayerManager


def test_protocol_manager_does_not_import_lichess_plugin():
    """The protocol layer must not name a player plugin.

    Failure: ``from universalchess.players.lichess`` is back, so adding
    Chess.com would need another isinstance branch here.
    """
    from universalchess.managers import protocol as protocol_module

    source = inspect.getsource(protocol_module)
    assert "from universalchess.players.lichess" not in source
    assert not hasattr(ProtocolManager, "stop_lichess")
    assert not hasattr(ProtocolManager, "start_lichess")
    assert not hasattr(ProtocolManager, "is_lichess_connected")
    assert not hasattr(ProtocolManager, "CLIENT_LICHESS")


def test_set_player_manager_binds_remote_clock_and_game_info():
    """Clock and names from a remote player must reach GameManager.

    Failure: bind_remote_session is skipped, so set_clock / set_game_info
    are never called when the stream updates.
    """
    from universalchess.state.time_control import TimeControl

    game_manager = MagicMock()
    protocol = ProtocolManager(game_manager=game_manager)
    remote = LichessPlayer()
    protocol.set_player_manager(PlayerManager(HumanPlayer(), remote))

    remote._clock_callback(120, 90)
    game_manager.set_clock.assert_called_once_with(120, 90)

    remote._game_info_callback("alice", "1500", "bob", "1400")
    game_manager.set_game_info.assert_called_once_with(
        "", "", "", "alice(1500)", "bob(1400)"
    )

    spec = TimeControl.fischer_minutes(5, 3)
    remote._time_control_callback(spec)
    game_manager.set_time_control.assert_called_once_with(spec)


def test_is_app_connected_is_only_board_protocols():
    """Lichess is a player, not a BLE chess-app protocol.

    Failure: is_app_connected reads self.is_lichess (AttributeError) or
    treats a Lichess game as a Millennium/Pegasus/Chessnut connection.
    """
    protocol = ProtocolManager(game_manager=MagicMock())
    protocol.set_player_manager(PlayerManager(HumanPlayer(), LichessPlayer()))
    assert protocol.is_app_connected() is False
    protocol.is_millennium = True
    assert protocol.is_app_connected() is True
