"""Event-pipeline tests for pawn promotion piece selection.

Background / why these tests exist
----------------------------------
A DGT-style board reports only piece *presence* per square, never piece
identity. Every promotion of a given source->destination (queen, rook, bishop,
knight) therefore produces an identical post-move presence pattern. The board
cannot know which piece the player promoted to, so the player must be asked --
that is what ``check_and_handle_promotion`` (invoked from ``on_player_move``
when a submitted move carries no promotion piece) does via the
``on_promotion_needed`` chooser.

These tests drive the real physical pipeline
(``process_field_event`` -> state-match shortcut / ``Player.on_piece_event`` ->
``on_player_move`` -> ``check_and_handle_promotion``) to pin the regression
where the normal-move state-match shortcut in ``field_events`` picked the first
legal candidate (the queen, because ``legal_moves`` yields it first) and
submitted it fully specified, skipping the chooser and silently auto-queening.
"""

from typing import List, Optional, Tuple

import chess

from universalchess.managers.game.correction_mode import CorrectionMode
from universalchess.managers.game.field_events import (
    FieldEventContext,
    process_field_event,
)
from universalchess.managers.game.move_state import MoveState
from universalchess.managers.game.player_moves import (
    PlayerMoveContext,
    on_player_move,
)
from universalchess.players.base import Player, PlayerType
from universalchess.state.chess_game import reset_chess_game
from universalchess.utils.led import LedCallbacks

# White pawn on a7 one square from promotion; kings placed so the position is
# legal and the promotion square (a8) is empty. Only the a-pawn matters.
PROMOTION_FEN = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
# White pawn a7 with a black rook on b8: promotion is available both by advance
# (a7a8) and by capture (a7xb8), exercising candidate disambiguation.
PROMOTION_CAPTURE_FEN = "1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1"


def _presence(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for sq in chess.SQUARES:
        state[sq] = 1 if board.piece_at(sq) is not None else 0
    return state


class _FakeBoard:
    """Board module stub: records beeps, reports the mutable physical state."""

    SOUND_GENERAL = "general"
    SOUND_WRONG_MOVE = "wrong"

    def __init__(self, physical: bytearray):
        self.beeps = []
        self._physical = physical

    def beep(self, sound, event_type=None):
        self.beeps.append((sound, event_type))

    def getChessState(self):
        return bytearray(self._physical)

    def getChessStateLowPriority(self):
        return bytearray(self._physical)


class _TestPlayer(Player):
    """Minimal concrete player that forms moves from piece events."""

    @property
    def player_type(self) -> PlayerType:
        return PlayerType.HUMAN

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        pass

    def on_new_game(self) -> None:
        pass


class _Game:
    """Integrated harness wiring field events -> player -> player_moves.

    ``promotion_choice`` is what the (spied) chooser returns; ``promotion_calls``
    records each ``is_white`` it was asked about so a test can assert the chooser
    was consulted rather than the piece being decided silently.
    """

    def __init__(self, fen: str = PROMOTION_FEN, promotion_choice: str = "n"):
        self.game_state = reset_chess_game()
        self.game_state.set_position(fen)
        self.board = self.game_state.board
        self.move_state = MoveState()
        self.correction_mode = CorrectionMode()
        self.physical = self.game_state.to_piece_presence_state()
        self.board_module = _FakeBoard(self.physical)
        self.correction_calls = 0
        self.pending_move: Optional[chess.Move] = None

        self.promotion_choice = promotion_choice
        self.promotion_calls: List[bool] = []
        # Every move actually handed to on_player_move, so a test can prove the
        # move was submitted WITHOUT a premature promotion piece.
        self.submitted_moves: List[chess.Move] = []

        self.led = LedCallbacks(
            from_to=lambda *a, **k: None,
            array=lambda *a, **k: None,
            single=lambda *a, **k: None,
            off=lambda *a, **k: None,
            from_to_hint=lambda *a, **k: None,
            array_hint=lambda *a, **k: None,
            array_fast=lambda *a, **k: None,
            from_to_fast=lambda *a, **k: None,
            single_fast=lambda *a, **k: None,
        )

        self.player = _TestPlayer()
        self.player.set_move_callback(self._on_player_move)

    def _on_promotion_needed(self, is_white: bool) -> str:
        self.promotion_calls.append(is_white)
        return self.promotion_choice

    # -- player_manager protocol used by field_events --
    def get_current_pending_move(self, _board: chess.Board) -> Optional[chess.Move]:
        return self.pending_move

    def _enter_correction(self) -> None:
        self.correction_calls += 1
        self.correction_mode.enter(_presence(self.board))

    def _move_ctx(self) -> PlayerMoveContext:
        return PlayerMoveContext(
            chess_board=self.board,
            game_state=self.game_state,
            move_state=self.move_state,
            board_module=self.board_module,
            led=self.led,
            get_game_db_id_fn=lambda: -1,
            switch_turn_with_event_fn=lambda: None,
            enqueue_post_move_tasks_fn=lambda **k: None,
            enter_correction_mode_fn=self._enter_correction,
            chess_board_to_state_fn=_presence,
            provide_correction_guidance_fn=lambda a, b: None,
            set_is_showing_promotion_fn=lambda v: None,
            on_promotion_needed_fn=self._on_promotion_needed,
        )

    def _on_player_move(self, move: chess.Move) -> bool:
        self.submitted_moves.append(move)
        return on_player_move(self._move_ctx(), move)

    def _field_ctx(self) -> FieldEventContext:
        return FieldEventContext(
            chess_board=self.board,
            move_state=self.move_state,
            correction_mode=self.correction_mode,
            player_manager=self,
            board_module=self.board_module,
            led=self.led,
            event_callback=None,
            enter_correction_mode_fn=self._enter_correction,
            provide_correction_guidance_fn=lambda a, b: None,
            handle_field_event_in_correction_mode_fn=lambda *a, **k: None,
            handle_piece_event_without_player_fn=lambda *a, **k: None,
            on_piece_event_fn=lambda et, sq, b: self.player.on_piece_event(et, sq, b),
            on_player_move_fn=self._on_player_move,
            handle_king_lift_resign_fn=lambda field, color: None,
            execute_pending_move_fn=lambda m: None,
            check_takeback_fn=lambda: False,
            get_kings_in_center_menu_active_fn=lambda: False,
            set_kings_in_center_menu_active_fn=lambda _v: None,
            on_kings_in_center_cancel_fn=None,
            get_king_lift_resign_menu_active_fn=lambda: False,
            set_king_lift_resign_menu_active_fn=lambda _v: None,
            on_king_lift_resign_cancel_fn=None,
            chess_board_to_state_fn=_presence,
        )

    def lift(self, sq: int) -> None:
        self.physical[sq] = 0
        process_field_event(self._field_ctx(), 0, sq, 0.0)

    def place(self, sq: int) -> None:
        self.physical[sq] = 1
        process_field_event(self._field_ctx(), 1, sq, 0.0)


def test_physical_promotion_asks_for_piece_and_honours_choice():
    """Lift a7, place a8 must consult the chooser and promote to its choice.

    Why: presence sensing cannot distinguish which promotion piece was placed,
    so the player must be asked. This drives the state-match shortcut in
    field_events (the path a physical promotion actually takes) and pins that it
    does not decide the piece itself.

    How the regression manifests: the shortcut submitted the first legal
    candidate (a7a8q) fully specified, so on_player_move skipped
    check_and_handle_promotion -> the chooser was never called
    (promotion_calls empty) and a queen appeared on a8 instead of the chosen
    knight.
    """
    g = _Game(promotion_choice="n")

    g.lift(chess.A7)
    g.place(chess.A8)

    assert g.promotion_calls == [True]
    assert g.board.piece_at(chess.A8) == chess.Piece(chess.KNIGHT, chess.WHITE)
    assert g.board.piece_at(chess.A7) is None
    assert g.board.peek().uci() == "a7a8n"
    assert g.correction_calls == 0


def test_state_match_shortcut_submits_promotion_without_a_piece():
    """The shortcut hands on_player_move a promotion move with promotion=None.

    Why: routing every promotion through the single chooser (in
    check_and_handle_promotion) requires the shortcut to submit the move without
    a promotion piece; committing any specific piece here would bypass the
    chooser. This asserts the exact contract at the boundary.

    How the regression manifests: the submitted move carries promotion set to a
    queen (move.promotion is not None) -> the chooser is bypassed downstream.
    """
    g = _Game(promotion_choice="r")

    g.lift(chess.A7)
    g.place(chess.A8)

    promotion_submissions: List[Tuple[chess.Move, Optional[int]]] = [
        (m, m.promotion) for m in g.submitted_moves if m.to_square == chess.A8
    ]
    assert promotion_submissions, "expected a submission for the promotion move"
    first_move, first_promotion = promotion_submissions[0]
    assert first_move.from_square == chess.A7
    assert first_promotion is None
    assert g.board.piece_at(chess.A8) == chess.Piece(chess.ROOK, chess.WHITE)


def test_physical_promotion_by_capture_asks_and_honours_choice():
    """Lift a7, place b8 (capturing) must ask and promote to the choice.

    Why: capture-promotions share the same presence ambiguity; the capture
    disambiguation (destination b8) must still route through the chooser.

    How the regression manifests: a7xb8 is submitted as a7b8q (first legal
    candidate to b8) -> chooser skipped and a queen lands on b8 instead of the
    chosen bishop.
    """
    g = _Game(fen=PROMOTION_CAPTURE_FEN, promotion_choice="b")

    # Lift the captured rook first, then the pawn, then place on b8 -- the
    # ordinary physical capture gesture.
    g.lift(chess.B8)
    g.lift(chess.A7)
    g.place(chess.B8)

    assert g.promotion_calls == [True]
    assert g.board.piece_at(chess.B8) == chess.Piece(chess.BISHOP, chess.WHITE)
    assert g.board.piece_at(chess.A7) is None
    assert g.board.peek().uci() == "a7b8b"
