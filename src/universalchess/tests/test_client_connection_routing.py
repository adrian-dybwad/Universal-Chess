"""Tests for what happens when a chess app connects, over either transport.

A phone app reaches the board over BLE (as one of the emulated boards) or over
classic RFCOMM. What the board must do is the same either way, but the handlers
were written twice -- the BLE pair at module level, the RFCOMM pair nested inside
main() -- and had drifted in four places, each of which is asserted below for both
transports:

* RFCOMM did not snapshot the menu position, so suspending the game it started
  reopened the top of the main menu instead of the submenu the user was in.
* RFCOMM never activated the remote controller when the app arrived while a game
  was still being built, leaving the board playing locally against a connected app.
* RFCOMM never told the controller the app had gone, leaving it routing moves to a
  dead link after the phone disconnected.
* BLE never recorded the link in the live Bluetooth status, so a connected BLE app
  showed as "not connected" on the board and the web, and the advertising state
  could not report that advertising had paused because a client was connected.

Every test runs for both transports. That is the point: a difference between them
is now a failure rather than something to notice by reading two copies.
"""

import pytest

from universalchess.app.pending_work import PendingWork
from universalchess.app.session import Session
from universalchess.managers.bluetooth_status_state import (
    TRANSPORT_BLE,
    TRANSPORT_RFCOMM,
    BluetoothStatusState,
)

# (transport, emulator, the label the board should use for this client). BLE names
# the emulated board it is speaking as; RFCOMM has no emulator and is named by its
# transport.
TRANSPORTS = [
    (TRANSPORT_BLE, "millennium", "millennium"),
    (TRANSPORT_RFCOMM, None, "rfcomm"),
]
TRANSPORT_IDS = [TRANSPORT_BLE, TRANSPORT_RFCOMM]


class FakeController:
    def __init__(self):
        self.remote_activated = False
        self.told_disconnected = False

    def activate_remote(self):
        self.remote_activated = True

    def on_bluetooth_disconnected(self):
        self.told_disconnected = True


class FakeProtocol:
    def __init__(self):
        self.app_connected = False
        self.app_disconnected = False

    def on_app_connected(self):
        self.app_connected = True

    def on_app_disconnected(self):
        self.app_disconnected = True


class FakeMenuManager:
    def __init__(self, active_widget=None):
        self.active_widget = active_widget
        self.cancelled_with = None

    def cancel_selection(self, reason):
        self.cancelled_with = reason


@pytest.fixture
def board(monkeypatch):
    """The application module with its game, session and menu replaced by fakes.

    Patched on the module because the handlers read these as module globals; the
    routing itself is what is under test, not how the globals are reached.
    """
    from universalchess.app import board_app

    controller = FakeController()
    protocol = FakeProtocol()
    status = BluetoothStatusState(broadcast=lambda *a, **k: None)

    monkeypatch.setattr(board_app, "_session", Session())
    monkeypatch.setattr(board_app, "_pending", PendingWork())
    monkeypatch.setattr(board_app, "_menu_manager", FakeMenuManager())
    monkeypatch.setattr(board_app._game, "controller", controller)
    monkeypatch.setattr(board_app._game, "protocol", protocol)
    monkeypatch.setattr(
        "universalchess.managers.bluetooth_status_state.get_bluetooth_status_state",
        lambda: status,
    )

    board_app.controller = controller
    board_app.protocol = protocol
    board_app.status = status
    return board_app


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestAClientConnectingWhileAMenuIsOpen:
    def test_the_menu_position_is_snapshotted_before_the_game_starts(
        self, board, monkeypatch, transport, emulator, label
    ):
        # The RFCOMM omission. The snapshot has to be taken here, while the menu
        # stack is still standing: cancelling the selection unwinds it and clears
        # the live navigation, so a later suspend has nothing to reopen and drops
        # the user at the top of the main menu.
        captured = []
        monkeypatch.setattr(
            board, "_capture_menu_for_resume", lambda: captured.append(True)
        )
        board._menu_manager.active_widget = object()

        board._on_client_connected(transport, emulator=emulator)

        assert captured == [True]

    def test_the_open_menu_is_cancelled_to_start_the_game(
        self, board, monkeypatch, transport, emulator, label
    ):
        # How the game actually starts from a menu: the blocking menu call returns
        # this sentinel, which the main loop reads as "enter the game".
        monkeypatch.setattr(board, "_capture_menu_for_resume", lambda: None)
        board._menu_manager.active_widget = object()

        board._on_client_connected(transport, emulator=emulator)

        assert board._menu_manager.cancelled_with == "CLIENT_CONNECTED"

    def test_a_connection_between_menus_is_left_for_the_main_loop(
        self, board, transport, emulator, label
    ):
        # With no menu widget on screen there is no selection to cancel, so the
        # request is queued and the loop enters the game on its next pass. The label
        # is carried so the log names the transport that arrived.
        board._menu_manager.active_widget = None

        board._on_client_connected(transport, emulator=emulator)

        request = board._pending.ble_client.take()
        assert request is not None
        assert request.payload == label


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestAClientConnectingDuringAGame:
    def test_the_user_is_asked_before_the_game_is_abandoned(
        self, board, monkeypatch, transport, emulator, label
    ):
        # A game in progress is not thrown away silently. The dialog names the
        # client so the user can tell which app is asking.
        asked = []
        monkeypatch.setattr(
            board, "_show_ble_connection_confirm", lambda client: asked.append(client)
        )
        board._session.enter_game()

        board._on_client_connected(transport, emulator=emulator)

        assert asked == [label]

    def test_the_controller_is_not_handed_over_behind_the_dialog(
        self, board, monkeypatch, transport, emulator, label
    ):
        # The handover waits for the answer. Doing it here would let the app drive a
        # game the user is about to keep playing on the board.
        monkeypatch.setattr(board, "_show_ble_connection_confirm", lambda client: None)
        board._session.enter_game()

        board._on_client_connected(transport, emulator=emulator)

        assert not board.controller.remote_activated


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestAClientConnectingWhileAGameIsBeingBuilt:
    def test_the_board_hands_control_to_the_app(
        self, board, transport, emulator, label
    ):
        # The second RFCOMM omission. This is the window after the game screen is up
        # but before its protocol exists. Without activate_remote the board keeps
        # driving itself, so the connected app sees a board that ignores it.
        board._session.enter_game()
        board._game.protocol = None

        board._on_client_connected(transport, emulator=emulator)

        assert board.controller.remote_activated


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestAClientDisconnecting:
    def test_the_controller_is_told_so_the_board_plays_locally_again(
        self, board, transport, emulator, label
    ):
        # The third RFCOMM omission, and the one a user would notice: left in remote
        # mode, the board goes on routing moves to a link that is gone, so pieces
        # moved on the board do nothing.
        board._on_client_disconnected()

        assert board.controller.told_disconnected

    def test_the_protocol_is_told_the_app_has_gone(
        self, board, transport, emulator, label
    ):
        # Its counterpart, which both handlers already did. Asserted so the unified
        # handler cannot lose it while gaining the line above.
        board._on_client_disconnected()

        assert board.protocol.app_disconnected


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestTheLiveBluetoothStatus:
    def test_the_link_is_recorded_with_its_transport(
        self, board, transport, emulator, label
    ):
        # The BLE omission. Nothing recorded a BLE link, so a connected BLE app read
        # as disconnected on the board and the web, and the advertising state could
        # not report that advertising had paused for a connected client -- it is
        # derived from this transport field.
        board._menu_manager.active_widget = None

        board._on_client_connected(transport, emulator=emulator)

        snapshot = board.status.to_dict()
        assert snapshot["connected"]
        assert snapshot["transport"] == transport
        assert snapshot["emulator"] == emulator
        assert snapshot["connected_since"] is not None

    def test_the_link_is_cleared_when_the_app_goes(
        self, board, transport, emulator, label
    ):
        # A stale link left behind shows a phone still connected after it has gone,
        # and keeps the advertising state reporting "paused" while it advertises.
        board._menu_manager.active_widget = None
        board._on_client_connected(transport, emulator=emulator)

        board._on_client_disconnected()

        snapshot = board.status.to_dict()
        assert not snapshot["connected"]
        assert snapshot["transport"] is None
        assert snapshot["emulator"] is None


@pytest.mark.parametrize(("transport", "emulator", "label"), TRANSPORTS, ids=TRANSPORT_IDS)
class TestABrokenStatusEngineIsNotFatal:
    def test_a_failing_status_record_still_connects_the_client(
        self, board, monkeypatch, transport, emulator, label
    ):
        # Status is for display only. A fault there must not cost the user their
        # connection, which is what an unguarded call would do -- the exception
        # would propagate into the transport's callback thread and kill it.
        def explode():
            raise RuntimeError("status engine unavailable")

        monkeypatch.setattr(
            "universalchess.managers.bluetooth_status_state.get_bluetooth_status_state",
            explode,
        )
        board._menu_manager.active_widget = None

        board._on_client_connected(transport, emulator=emulator)

        request = board._pending.ble_client.take()
        assert request is not None
        assert request.payload == label
