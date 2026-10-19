"""The website's board stands in for the physical board while a chess app is connected.

Why these tests exist
---------------------
In local play a move dragged on the web board lets the engine reply at once, the
physical board is left out of sync, and touching a real piece enters correction.
With a Millennium, Pegasus or Chessnut app connected none of that happened: the
app's reply only lit LEDs and waited for a physical move, touching a piece never
entered correction (the session suppresses it), the touch was forwarded to the
app as if it were a move, and Pegasus never heard about the web move at all.

The contract pinned here:
- every move played on the web board is reported as the lift/place events a
  physical board would have produced (on_web_piece_event), and the app is told
  the game's occupancy, not the stale physical one, while the web board leads;
- the app's lit reply plays immediately when it names exactly one legal move for
  the app's seat; a promotion (or any other single from/to) becomes the web's
  pending arrow so the user drags it and picks the piece;
- the first physical touch after web play enters correction even during the
  session, with its LEDs live, and the app sees no physical events until the
  board matches again.

How a regression manifests
--------------------------
The app's reply stays unplayed (move stack unchanged); a promotion is played
with an invented piece; a touch leaves correction inactive; physical events
reach the app during correction; the app is sent physical occupancy that no
longer matches the game.
"""

from unittest.mock import MagicMock

import chess
import pytest

from universalchess.board.piece_events import PIECE_LIFT, PIECE_PLACE
from universalchess.managers.game.game_manager import GameManager
from universalchess.state.chess_game import reset_chess_game
from universalchess.utils.led import LedCallbacks

GAME_MANAGER_MODULE = "universalchess.managers.game.game_manager"
START_OCCUPANCY = bytearray([1] * 16 + [0] * 32 + [1] * 16)
TOUCH_TIME_SECONDS = 1.0
# White to move; after White's web move Kf2, Black's b2 pawn can promote on b1 or
# by capturing a1/c1. The app (Black) lighting b2+b1 names four promotion moves.
APP_PROMOTION_FEN = "4k3/8/8/8/8/8/1p6/R1N1K3 w - - 0 1"


def _recording_led(calls):
    return LedCallbacks(
        from_to=lambda *a, **k: calls.append("from_to"),
        array=lambda *a, **k: calls.append("array"),
        single=lambda *a, **k: calls.append("single"),
        off=lambda *a, **k: calls.append("off"),
        from_to_hint=lambda *a, **k: calls.append("from_to_hint"),
        array_hint=lambda *a, **k: calls.append("array_hint"),
        array_fast=lambda *a, **k: calls.append("array_fast"),
        from_to_fast=lambda *a, **k: calls.append("from_to_fast"),
        single_fast=lambda *a, **k: calls.append("single_fast"),
    )


@pytest.fixture
def physical(monkeypatch):
    """The physical occupancy the board reports; tests replace ``state``.

    Starts at the opening occupancy, which is what the pieces show after a web
    move nobody mirrored on the board.
    """
    holder = {"state": bytearray(START_OCCUPANCY)}
    monkeypatch.setattr(f"{GAME_MANAGER_MODULE}.board.getChessState", lambda: holder["state"])
    return holder


@pytest.fixture
def pending_arrows(monkeypatch):
    """Values handed to the web's pending-move side channel."""
    values = []
    monkeypatch.setattr(f"{GAME_MANAGER_MODULE}.set_pending_move", values.append)
    return values


@pytest.fixture
def gm(monkeypatch, physical):
    """GameManager on a fresh game with board hardware stubbed and LEDs recorded.

    ``gm.led_calls`` collects every LED callback that actually reaches the board,
    so a test can tell muted session LEDs from live correction LEDs. Web piece
    events are recorded in ``gm.web_events`` with the move that was on top of the
    stack when each was emitted.
    """
    reset_chess_game()
    monkeypatch.setattr(f"{GAME_MANAGER_MODULE}.board.ledsOff", lambda: None)
    monkeypatch.setattr(f"{GAME_MANAGER_MODULE}.board.beep", lambda *a, **k: None)
    manager = GameManager(save_to_database=False)
    manager.led_calls = []
    manager.set_led_callbacks(_recording_led(manager.led_calls))
    manager.web_events = []
    manager.on_web_piece_event = lambda piece_event, field: manager.web_events.append(
        (piece_event, field, manager.chess_board.peek().uci())
    )
    manager._is_ready = True
    yield manager
    manager._stop_event.set()


def _web_play_with_app(gm, *, app_color=chess.BLACK, web_move="e2e4"):
    """Connect an app on ``app_color`` and play ``web_move`` on the web board."""
    gm.set_remote_app_session(True)
    gm.set_remote_app_color(app_color)
    assert gm.submit_web_move(web_move) is True
    gm.web_events.clear()


def _stack(gm):
    return [move.uci() for move in gm.chess_board.move_stack]


def _arrows(pending_values):
    """Pending arrows posted; None is the clear that follows any played move."""
    return [value for value in pending_values if value is not None]


# ---------------------------------------------------------------------------
# The app's reply
# ---------------------------------------------------------------------------


def test_app_reply_plays_immediately_after_a_web_move(gm):
    """A lit reply naming one legal move for the app's seat is played at once.

    This is the reported behavior: in local web play the engine replies without
    waiting for the physical board, and an app must too. The reply is reported to
    the app as piece events, like the web move before it. Regression: the stack
    stays ["e2e4"] because the reply waits on physical pieces that never move.
    """
    _web_play_with_app(gm)

    gm.indicate_app_move({chess.E7, chess.E5})

    assert _stack(gm) == ["e2e4", "e7e5"]
    assert gm.web_events == [(PIECE_LIFT, chess.E7, "e7e5"), (PIECE_PLACE, chess.E5, "e7e5")]


def _app_connected_physical_play(gm):
    """App on Black, White's e2e4 made on the physical board (no web move)."""
    gm.set_remote_app_session(True)
    gm.set_remote_app_color(chess.BLACK)
    assert gm._on_player_move(chess.Move.from_uci("e2e4")) is True


def _web_move_without_app(gm):
    """White's e2e4 played on the web board with no app connected."""
    assert gm.submit_web_move("e2e4") is True


@pytest.mark.parametrize(
    "reach_black_to_move",
    [
        pytest.param(_app_connected_physical_play, id="physical-play-with-app"),
        pytest.param(_web_move_without_app, id="web-move-without-app"),
    ],
)
def test_app_reply_is_left_to_the_board_outside_web_play_with_an_app(
    gm, pending_arrows, reach_black_to_move
):
    """The decode acts only when an app is connected and the web board leads.

    During physical play with an app the user transcribes the reply as before;
    without an app there is no app reply to read (the engine path handles it).
    Regression: a reply is played (stack grows) or an arrow is posted while the
    user is still expected to move the piece on the board.
    """
    reach_black_to_move(gm)

    gm.indicate_app_move({chess.E7, chess.E5})

    assert _stack(gm) == ["e2e4"]
    assert _arrows(pending_arrows) == []


def test_app_promotion_reply_becomes_the_pending_arrow(gm, pending_arrows):
    """A promotion reply is shown, not played: the user picks the piece on the web.

    The app's LEDs carry no piece, so playing it would invent one. The from/to is
    posted (without a piece letter) as the pending arrow; dragging it opens the web
    board's promotion picker, as the e-paper menu would on a physical board.
    Regression: the stack grows by a b2b1? move, or no arrow is posted.
    """
    gm._game_state.set_position(APP_PROMOTION_FEN)
    _web_play_with_app(gm, web_move="e1f2")

    gm.indicate_app_move({chess.B2, chess.B1})

    assert _stack(gm) == ["e1f2"]
    assert _arrows(pending_arrows) == ["b2b1"]


def test_reply_lit_for_a_side_the_app_does_not_hold_becomes_the_pending_arrow(gm, pending_arrows):
    """A single legal move on a side not seated by the app is shown, not played.

    An app may light a hint for the user's own move, and with no recorded app seat
    (no engine was replaced) the mover is unknown. Either way the move is the
    user's to make, so it is offered as the arrow. Regression: the hint is played
    for the user (stack grows).
    """
    _web_play_with_app(gm, app_color=chess.WHITE)

    gm.indicate_app_move({chess.E7, chess.E5})

    assert _stack(gm) == ["e2e4"]
    assert _arrows(pending_arrows) == ["e7e5"]


def test_undecodable_indication_changes_nothing(gm, pending_arrows):
    """Lights that name no legal move (a hint pattern, a setup matrix) are ignored.

    Regression: a move is played or an arrow posted for squares that describe no move.
    """
    _web_play_with_app(gm)

    gm.indicate_app_move({chess.A7, chess.H2, chess.D4})

    assert _stack(gm) == ["e2e4"]
    assert _arrows(pending_arrows) == []


# ---------------------------------------------------------------------------
# Web-played moves reported as piece events
# ---------------------------------------------------------------------------


def test_web_move_is_reported_as_piece_events_after_it_is_played(gm):
    """A web move produces the physical board's lift/place sequence, post-move.

    Pegasus follows piece events only, so without these the DGT app never learns
    the web move. They are emitted after the move is on the stack, so an emulator
    reading the game position while handling them sees the new position.
    Regression: web_events is empty, or an entry carries no move on the stack.
    """
    assert gm.submit_web_move("e2e4") is True

    assert gm.web_events == [(PIECE_LIFT, chess.E2, "e2e4"), (PIECE_PLACE, chess.E4, "e2e4")]


def test_engine_reply_auto_played_on_the_web_board_is_reported_as_piece_events(gm):
    """An engine reply applied for the web player is reported like any web move.

    One rule for every move the web board plays keeps the app's view consistent.
    Regression: the engine reply is played but web_events stays empty.
    """
    gm.submit_web_move("e2e4")
    gm.web_events.clear()

    gm._on_pending_move(chess.Move.from_uci("e7e5"))

    assert gm.web_events == [(PIECE_LIFT, chess.E7, "e7e5"), (PIECE_PLACE, chess.E5, "e7e5")]


def test_rejected_web_move_reports_no_piece_events(gm):
    """An illegal web move changes nothing, so nothing is reported.

    Regression: the app is told a piece moved that the game never moved.
    """
    assert gm.submit_web_move("e2e5") is False

    assert gm.web_events == []


@pytest.mark.parametrize(
    ("web_leads", "expected"),
    [
        pytest.param(True, "game", id="web-board-leads"),
        pytest.param(False, "physical", id="physical-board-leads"),
    ],
)
def test_reported_occupancy_follows_the_board_that_leads(gm, physical, web_leads, expected):
    """The app is told the occupancy of whichever board currently leads.

    After a web move the pieces still show the opening, and reporting them would
    tell the app the web move was taken back. Regression: the web-leads case
    returns the opening occupancy, or the physical case ignores the real pieces.
    """
    gm.submit_web_move("e2e4")
    if not web_leads:
        gm._web_move_mode = False
    game_state = gm._chess_board_to_state(gm.chess_board)
    states = {"game": game_state, "physical": physical["state"]}

    assert gm.reported_occupancy() == states[expected]


# ---------------------------------------------------------------------------
# Correction after web play
# ---------------------------------------------------------------------------


def test_first_touch_after_web_play_enters_correction_during_the_session(gm, monkeypatch):
    """Touching a real piece after web play reconciles the board, app or not.

    The session suppresses correction because the app is the referee of physical
    play; after web play the game is the authority and the pieces lag it. The
    correction LEDs must reach the board. Regression: correction stays inactive,
    or it is active but led_calls stays empty because session LEDs are muted.
    """
    monkeypatch.setattr(gm, "_process_field_event", lambda *args: None)
    _web_play_with_app(gm)
    gm.led_calls.clear()

    gm.receive_field(PIECE_LIFT, chess.E2, TOUCH_TIME_SECONDS)

    assert gm.correction_mode.is_active is True
    assert gm.is_withholding_physical_board is True
    assert gm.led_calls != []


def test_first_touch_on_a_mirrored_board_needs_no_correction(gm, physical, monkeypatch):
    """If the user also moved the pieces, the touch hands back without correction.

    Regression: correction (and withholding) starts on a board that already
    matches the game.
    """
    processed = []
    monkeypatch.setattr(gm, "_process_field_event", lambda *args: processed.append(args))
    _web_play_with_app(gm)
    physical["state"] = gm._chess_board_to_state(gm.chess_board)

    gm.receive_field(PIECE_LIFT, chess.G1, TOUCH_TIME_SECONDS)

    assert gm.correction_mode.is_active is False
    assert gm.is_withholding_physical_board is False
    assert processed == [(PIECE_LIFT, chess.G1, TOUCH_TIME_SECONDS)]


def test_completed_correction_returns_the_lights_to_the_app(gm, monkeypatch):
    """Once the board matches, withholding ends and session LEDs are muted again.

    Regression: is_withholding_physical_board stays True (the app never hears the
    board again), or GameManager keeps writing LEDs over the app's lights.
    """
    monkeypatch.setattr(gm, "_process_field_event", lambda *args: None)
    _web_play_with_app(gm)
    gm.receive_field(PIECE_LIFT, chess.E2, TOUCH_TIME_SECONDS)

    gm._exit_correction_mode()
    gm.led_calls.clear()
    gm.led.off()

    assert gm.is_withholding_physical_board is False
    assert gm.led_calls == []


def test_app_disconnect_during_correction_keeps_correcting_locally(gm, monkeypatch):
    """Disconnecting mid-correction ends withholding; local correction continues.

    Regression: withholding survives the session (a later app sees nothing), or
    correction is dropped although the pieces still do not match the game.
    """
    monkeypatch.setattr(gm, "_process_field_event", lambda *args: None)
    _web_play_with_app(gm)
    gm.receive_field(PIECE_LIFT, chess.E2, TOUCH_TIME_SECONDS)

    gm.set_remote_app_session(False)

    assert gm.is_withholding_physical_board is False
    assert gm.correction_mode.is_active is True


# ---------------------------------------------------------------------------
# Routing between the boards and the app
# ---------------------------------------------------------------------------


def _controller_manager_with_app(game_manager):
    """ControllerManager whose remote controller has a detected app protocol."""
    from universalchess.controllers.manager import ControllerManager

    manager = ControllerManager(game_manager)
    manager.create_remote_controller(MagicMock())
    remote = MagicMock()
    remote.is_protocol_detected = True
    manager._remote = remote
    return manager, remote


def test_web_piece_events_are_forwarded_to_the_app(gm):
    """The controller routes web-board events to the app, with no hardware time.

    A web event has no Centaur timestamp, so None is passed rather than an
    invented time. Regression: on_field_event is never called (the app misses
    the web move) or is called with a fabricated time.
    """
    gm.on_web_piece_event = None
    _manager, remote = _controller_manager_with_app(gm)

    gm.submit_web_move("e2e4")

    assert remote.on_field_event.call_args_list == [
        ((PIECE_LIFT, chess.E2, None),),
        ((PIECE_PLACE, chess.E4, None),),
    ]


@pytest.mark.parametrize(
    ("withheld_before", "withheld_after"),
    [
        pytest.param(False, True, id="touch-that-starts-correction"),
        pytest.param(True, True, id="touch-during-correction"),
        pytest.param(True, False, id="touch-that-completes-correction"),
    ],
)
def test_physical_events_during_correction_are_withheld_from_the_app(
    withheld_before, withheld_after
):
    """No physical event of a post-web-play correction reaches the app.

    The app's board is the web board's position; the pieces being put back are
    not moves. The touch that starts correction and the one that completes it
    are withheld too. Regression: on_field_event is called for any of them.
    """
    game_manager = MagicMock()
    game_manager.is_withholding_physical_board = withheld_before

    def receive_field(*_args):
        game_manager.is_withholding_physical_board = withheld_after

    game_manager.receive_field.side_effect = receive_field
    manager, remote = _controller_manager_with_app(game_manager)

    manager.on_field_event(PIECE_PLACE, chess.E2, TOUCH_TIME_SECONDS)

    game_manager.receive_field.assert_called_once_with(PIECE_PLACE, chess.E2, TOUCH_TIME_SECONDS)
    remote.on_field_event.assert_not_called()


def test_physical_events_outside_correction_still_reach_the_app():
    """Ordinary physical play keeps forwarding every event to the app.

    Regression: the withholding gate swallows normal moves.
    """
    game_manager = MagicMock()
    game_manager.is_withholding_physical_board = False
    manager, remote = _controller_manager_with_app(game_manager)

    manager.on_field_event(PIECE_LIFT, chess.E2, TOUCH_TIME_SECONDS)

    remote.on_field_event.assert_called_once_with(PIECE_LIFT, chess.E2, TOUCH_TIME_SECONDS)


def test_protocol_detection_records_the_app_seat_and_disconnect_clears_it():
    """The engine's colour becomes the app's seat, and is cleared on disconnect.

    Regression: set_remote_app_color is never called (replies are never played)
    or keeps a stale seat after the app leaves.
    """
    from universalchess.managers.protocol import ProtocolManager
    from universalchess.players.engine import EnginePlayer
    from universalchess.players.human import HumanPlayer
    from universalchess.players.manager import PlayerManager

    game_manager = MagicMock()
    protocol = ProtocolManager(game_manager=game_manager)
    engine = EnginePlayer()
    engine.start = lambda: True
    engine.stop = lambda: None
    protocol.set_player_manager(PlayerManager(HumanPlayer(), engine))

    protocol.on_protocol_detected(ProtocolManager.CLIENT_MILLENNIUM)
    protocol.on_app_disconnected()

    assert game_manager.set_remote_app_color.call_args_list == [((chess.BLACK,),), ((None,),)]
