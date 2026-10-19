"""Emulators read the app's reply into the game and report the game's occupancy.

Why these tests exist
---------------------
For the web board to stand in for the physical board while an app is connected,
each emulator has two duties beyond driving LEDs:

- hand the squares the app lit for its reply to GameManager.indicate_app_move,
  which decides whether to play it (only while the web board leads);
- report occupancy through GameManager.reported_occupancy, which is the game's
  occupancy while the web board leads, instead of reading the hardware directly.

How a regression manifests
--------------------------
indicate_app_move is never called, so the app's reply waits on physical pieces;
Millennium answers its "l" acknowledgement after a status that already contains
the reply; Millennium or Pegasus report the stale physical pieces and the app
sees the web move taken back; a Chessnut setup matrix is read as a move.
"""

from unittest.mock import MagicMock

import chess
import pytest

from universalchess.emulators import chessnut as chessnut_module
from universalchess.emulators import millennium as millennium_module
from universalchess.emulators import pegasus as pegasus_module
from universalchess.emulators.chessnut import Chessnut
from universalchess.emulators.millennium import Millennium
from universalchess.emulators.pegasus import Pegasus
from universalchess.managers.events import EVENT_LIFT_PIECE

AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
START_OCCUPANCY = [1] * 16 + [0] * 32 + [1] * 16
APP_REPLY_SQUARES = {chess.E7, chess.E5}
LED_INTENSITY = 5
PEGASUS_LED_SPEED = 7
PEGASUS_APP_INTENSITY = 5


def _occupancy_after_e4():
    state = list(START_OCCUPANCY)
    state[chess.E2] = 0
    state[chess.E4] = 1
    return state


def _hardware_index(square):
    """Centaur hardware order (0=a8 .. 63=h1) for a chess square (0=a1 .. 63=h8)."""
    return (7 - chess.square_rank(square)) * 8 + chess.square_file(square)


class _LedBoard:
    """Board module stand-in: accepts LED calls, reports opening occupancy."""

    def ledFromTo(self, *args, **kwargs):
        pass

    def ledArray(self, *args, **kwargs):
        pass

    def led(self, *args, **kwargs):
        pass

    def ledsOff(self):
        pass

    def getChessState(self):
        return list(START_OCCUPANCY)

    def getBoardState(self):
        return bytes(START_OCCUPANCY)

    def rotateFieldHex(self, field):
        return field


@pytest.fixture
def game_manager():
    """GameManager stand-in after White's web move e2e4, recording indications."""
    manager = MagicMock()
    manager.get_fen.return_value = AFTER_E4_FEN
    manager.reported_occupancy.return_value = _occupancy_after_e4()
    manager.order = []
    manager.indicate_app_move.side_effect = lambda squares: manager.order.append(
        ("indicate", set(squares))
    )
    return manager


@pytest.fixture(autouse=True)
def board_stub(monkeypatch):
    stub = _LedBoard()
    for module in (millennium_module, pegasus_module, chessnut_module):
        monkeypatch.setattr(module, "board", stub)
        monkeypatch.setattr(module, "get_led_intensity_from_settings", lambda: LED_INTENSITY)
    monkeypatch.setattr(pegasus_module, "get_pegasus_override_brightness", lambda: True)
    return stub


def _millennium_led_payload(emulator, squares):
    """An L packet payload: slot time, then 81 LED codes with each square's 4 corners lit."""
    codes = [0] * 81
    for square in squares:
        for index in emulator.square_led_indices(chess.square_name(square)):
            codes[index] = 0xFF
    text = "00" + "".join(f"{code:02X}" for code in codes)
    return [ord(char) for char in text]


def test_millennium_hands_the_lit_reply_to_the_game_after_acknowledging(game_manager, monkeypatch):
    """The L packet is acknowledged first, then its squares go to GameManager.

    Playing the reply sends a board status; sent before the "l" acknowledgement,
    the app would receive a status in reply to its LED command. e7e5 also lights
    every corner of e6 (the corners are shared), so a decode by fully lit
    squares hands over {e7, e6, e5}, which is no move. Regression: no indicate
    entry, an entry naming e6, or one that precedes the "l".
    """
    emulator = Millennium(sendMessage_callback=MagicMock(), manager=game_manager)
    monkeypatch.setattr(
        emulator,
        "encode_millennium_command",
        lambda text: game_manager.order.append(("send", text)),
    )

    emulator.handle_l(_millennium_led_payload(emulator, APP_REPLY_SQUARES))

    assert game_manager.order == [("send", "l"), ("indicate", APP_REPLY_SQUARES)]


def test_millennium_status_uses_the_reported_occupancy(game_manager, monkeypatch):
    """A piece event's status is the game position masked by reported occupancy.

    The physical board still shows e4 empty; masking the game position with it
    blanks e4, so the app is told White's web-played pawn vanished. Regression:
    the status sent is not the game position's own eONE string.
    """
    emulator = Millennium(sendMessage_callback=MagicMock(), manager=game_manager)
    sent = []
    monkeypatch.setattr(emulator, "encode_millennium_command", sent.append)

    emulator.handle_manager_event(EVENT_LIFT_PIECE, 0, chess.E2, None)

    assert sent == ["s" + emulator.fen_to_eone(AFTER_E4_FEN)]
    game_manager.reported_occupancy.assert_called_once_with()


def test_pegasus_board_dump_reports_the_reported_occupancy_in_hardware_order(
    game_manager, monkeypatch
):
    """The DGT app's board dump is the game's occupancy, in Pegasus wire order.

    Regression: the dump shows the stale physical pieces (e2 occupied, e4 empty),
    or the game occupancy in chess order (rank 1 first) instead of hardware order.
    """
    emulator = Pegasus(sendMessage_callback=MagicMock(), manager=game_manager)
    packets = []
    monkeypatch.setattr(
        emulator, "send_packet", lambda _kind, payload=b"": packets.append(list(payload))
    )

    emulator.board_dump()

    expected = [0] * 64
    for square, occupied in enumerate(_occupancy_after_e4()):
        expected[_hardware_index(square)] = occupied
    assert packets == [expected]


def _unreadable_through_manager(game_manager, _board_stub, _monkeypatch):
    game_manager.reported_occupancy.return_value = None
    return game_manager


def _unreadable_hardware_without_manager(_game_manager, board_stub, monkeypatch):
    monkeypatch.setattr(board_stub, "getBoardState", lambda: None)
    return None


@pytest.mark.parametrize(
    "make_unreadable",
    [
        pytest.param(_unreadable_through_manager, id="manager-occupancy-unreadable"),
        pytest.param(_unreadable_hardware_without_manager, id="hardware-unreadable"),
    ],
)
def test_pegasus_board_dump_sends_nothing_when_occupancy_is_unreadable(
    game_manager, board_stub, monkeypatch, make_unreadable
):
    """A failed board read is not reported as an empty board.

    An all-empty dump tells the DGT app every piece was lifted, which it acts on
    as a real position. With nothing readable there is no dump to give, so none
    is sent and the packet is reported unhandled. Regression: packets holds one
    64-zero payload, or board_dump returns True.
    """
    manager = make_unreadable(game_manager, board_stub, monkeypatch)
    emulator = Pegasus(sendMessage_callback=MagicMock(), manager=manager)
    packets = []
    monkeypatch.setattr(
        emulator, "send_packet", lambda _kind, payload=b"": packets.append(list(payload))
    )

    handled = emulator.board_dump()

    assert packets == []
    assert handled is False


def test_pegasus_hands_the_lit_reply_to_the_game(game_manager):
    """A mode-5 LED packet's fields go to GameManager as chess squares.

    Regression: indicate_app_move is not called, or is called with the hardware
    indices (12, 28) instead of e7/e5.
    """
    emulator = Pegasus(sendMessage_callback=MagicMock(), manager=game_manager)
    fields = [_hardware_index(square) for square in sorted(APP_REPLY_SQUARES)]

    emulator.led_control([5, PEGASUS_LED_SPEED, 0, PEGASUS_APP_INTENSITY, *fields])

    assert game_manager.order == [("indicate", APP_REPLY_SQUARES)]


def _chessnut_led_payload(squares):
    """8-byte matrix: byte 0 is rank 8, bit 7 is file a."""
    rows = [0] * 8
    for square in squares:
        rows[7 - chess.square_rank(square)] |= 1 << (7 - chess.square_file(square))
    return rows


def test_chessnut_hands_a_move_indicator_to_the_game(game_manager):
    """A Chessnut move matrix goes to GameManager after the LEDs are set.

    Regression: indicate_app_move is not called, so the reply waits on the board.
    """
    emulator = Chessnut(sendMessage_callback=MagicMock(), manager=game_manager)

    emulator._handle_led_command(_chessnut_led_payload(APP_REPLY_SQUARES))

    assert game_manager.order == [("indicate", APP_REPLY_SQUARES)]


def test_chessnut_setup_matrix_is_not_a_move_indicator(game_manager, monkeypatch):
    """In puzzle setup the matrix guides piece placement; it is never a reply.

    Regression: indicate_app_move is called with setup squares.
    """
    emulator = Chessnut(sendMessage_callback=MagicMock(), manager=game_manager)
    emulator._setup_mode = True
    monkeypatch.setattr(emulator, "_handle_led_in_setup", lambda squares: None)

    emulator._handle_led_command(_chessnut_led_payload(APP_REPLY_SQUARES))

    game_manager.indicate_app_move.assert_not_called()
