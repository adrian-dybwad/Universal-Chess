"""Event-pipeline tests for king-first castling.

Background / why these tests exist
----------------------------------
Castling is exactly one gesture: the king moves two squares, then the rook
follows to its castling square. There is no "rook-first" castle. `python-chess`
`legal_moves` already enforces every castling rule (castling rights, empty
intervening squares, king not in/through/into check), so castling validity is
just legality of the king's two-square move.

These tests drive the real lift/place pipeline
(`process_field_event` -> `Player.on_piece_event` -> `on_player_move` ->
`execute_complete_move`) and the move-validation entry point
(`on_player_move`) with a mutable physical board, to pin:

- King-first castling is recognised and executed; the rook physically following
  completes cleanly with no SOUND_WRONG_MOVE (including the race path where the
  rook is lifted immediately, before any async validation).
- Invalid castling (no rights / through check / king-onto-rook) is rejected:
  the move is NOT pushed and correction mode is entered.
- A rook moved first is just a rook move; a following king move is NOT rescued
  into a castle (the deleted "late castling" behaviour must stay deleted).
- The same holds while an engine move is pending (vs-engine).
"""

from typing import Optional

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

# Position with both sides able to castle either way; ranks 2-7 empty so only
# the king/rook squares matter for castling reasoning.
CASTLE_FEN = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"


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

    Physical occupancy is mutated by `lift`/`place` to mirror the sensor seeing
    a square vacated/occupied before the corresponding event is dispatched.
    """

    def __init__(self, fen: str = CASTLE_FEN):
        self.game_state = reset_chess_game()
        self.game_state.set_position(fen)
        self.board = self.game_state.board
        self.move_state = MoveState()
        self.correction_mode = CorrectionMode()
        self.physical = self.game_state.to_piece_presence_state()
        self.board_module = _FakeBoard(self.physical)
        self.flashes = []
        self.correction_calls = 0
        self.pending_move: Optional[chess.Move] = None

        self.led = LedCallbacks(
            from_to=lambda *a, **k: None,
            array=lambda *a, **k: None,
            single=lambda *a, **k: None,
            off=lambda *a, **k: None,
            from_to_hint=lambda *a, **k: None,
            array_hint=lambda *a, **k: None,
            array_fast=lambda *a, **k: None,
            from_to_fast=lambda *a, **k: None,
            single_fast=lambda sq, repeat=0: self.flashes.append(sq),
        )

        self.player = _TestPlayer()
        self.player.set_move_callback(self._on_player_move)

    # -- player_manager protocol used by field_events --
    def get_current_pending_move(self, _board: chess.Board) -> Optional[chess.Move]:
        return self.pending_move

    # -- wiring --
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
            on_promotion_needed_fn=None,
        )

    def _on_player_move(self, move: chess.Move) -> bool:
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

    @property
    def wrong_move_beeped(self) -> bool:
        return any(sound == _FakeBoard.SOUND_WRONG_MOVE for sound, _ in self.board_module.beeps)


# ---------------------------------------------------------------------------
# King-first castling is recognised and executed
# ---------------------------------------------------------------------------

def test_king_first_kingside_is_recognised_and_arms_rook_follow():
    """King e1->g1 must be accepted as castling and arm the rook-follow.

    Why: castling is the king's two-square move; the rook then follows. After a
    successful castle the logical board must hold the castled position and the
    pending rook completion (h1->f1) must be recorded.

    How the regression manifests: on_player_move returns False / the board is not
    castled / castling_rook_pending is not armed -> the rook-follow can never be
    recognised and the rook move would later look like an error.
    """
    g = _Game()

    accepted = g._on_player_move(chess.Move.from_uci("e1g1"))

    assert accepted is True
    assert g.board.piece_at(chess.G1) == chess.Piece(chess.KING, chess.WHITE)
    assert g.board.piece_at(chess.F1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert g.board.piece_at(chess.E1) is None
    assert g.board.piece_at(chess.H1) is None
    assert g.move_state.castling_rook_pending == (chess.H1, chess.F1)
    assert not g.wrong_move_beeped


def test_king_first_queenside_arms_correct_rook_follow():
    """King e1->c1 arms the queenside rook-follow a1->d1.

    Why: queenside castling moves the a-rook to d1; the armed completion squares
    must match, or queenside castling cannot be completed on the physical board.

    How the regression manifests: castling_rook_pending is None or holds the
    kingside squares -> queenside rook placement is treated as an error.
    """
    g = _Game()

    accepted = g._on_player_move(chess.Move.from_uci("e1c1"))

    assert accepted is True
    assert g.board.piece_at(chess.C1) == chess.Piece(chess.KING, chess.WHITE)
    assert g.board.piece_at(chess.D1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert g.move_state.castling_rook_pending == (chess.A1, chess.D1)


def test_king_first_black_kingside_arms_correct_rook_follow():
    """Black king e8->g8 arms the rook-follow h8->f8.

    Why: the rook squares are colour-dependent; black kingside must arm h8->f8.

    How the regression manifests: wrong/none pending squares -> black castling
    cannot complete.
    """
    g = _Game("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1")

    accepted = g._on_player_move(chess.Move.from_uci("e8g8"))

    assert accepted is True
    assert g.board.piece_at(chess.G8) == chess.Piece(chess.KING, chess.BLACK)
    assert g.board.piece_at(chess.F8) == chess.Piece(chess.ROOK, chess.BLACK)
    assert g.move_state.castling_rook_pending == (chess.H8, chess.F8)


# ---------------------------------------------------------------------------
# Full physical sequence: king two squares, then rook follows
# ---------------------------------------------------------------------------

def test_full_kingside_sequence_completes_without_wrong_move_beep():
    """Lift e1, place g1, lift h1, place f1 must castle cleanly.

    Why: the rook physically following the king is part of the single castling
    gesture. It must not be mistaken for an illegal interaction ("piece has no
    legal moves" on the opponent's turn) and must not emit SOUND_WRONG_MOVE.

    How the regression manifests: lifting the rook on the opponent's turn trips
    the no-legal-moves guard -> SOUND_WRONG_MOVE beep and correction mode, and
    castling_rook_pending is never cleared.
    """
    g = _Game()

    g.lift(chess.E1)
    g.place(chess.G1)
    # Castling executed at king placement; rook still physically on h1.
    assert g.board.piece_at(chess.F1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert g.move_state.castling_rook_pending == (chess.H1, chess.F1)

    g.lift(chess.H1)
    g.place(chess.F1)

    assert g.move_state.castling_rook_pending is None
    assert g.correction_calls == 0
    assert not g.wrong_move_beeped
    # Two piece-place confirmations: king, then rook.
    assert g.board_module.beeps.count((_FakeBoard.SOUND_GENERAL, "piece_event")) == 2


def test_rook_follow_completes_even_while_engine_move_pending():
    """The rook-follow completes even when an engine move is pending.

    Why: after the human castles, the engine (to move) produces a pending move.
    The human then slides the rook to f1. That completion must be recognised
    ahead of the pending-move "wrong piece" guard, with no SOUND_WRONG_MOVE.

    How the regression manifests: the rook lift is treated as "wrong piece
    during forced move" -> SOUND_WRONG_MOVE and correction mode.
    """
    g = _Game()

    g.lift(chess.E1)
    g.place(chess.G1)
    # Engine (black) now has a pending reply.
    g.pending_move = chess.Move.from_uci("e8g8")

    g.lift(chess.H1)
    g.place(chess.F1)

    assert g.move_state.castling_rook_pending is None
    assert g.correction_calls == 0
    assert not g.wrong_move_beeped


# ---------------------------------------------------------------------------
# Invalid castling must be rejected (no rights / through check / king-onto-rook)
# ---------------------------------------------------------------------------

def test_castling_without_rights_is_rejected():
    """King e1->g1 with no castling rights is rejected, not pushed.

    Why: castling legality includes castling rights; a two-square king move
    without rights is illegal and must enter correction, never execute.

    How the regression manifests: the move is pushed (board castled) or no
    correction occurs -> illegal castling silently accepted.
    """
    g = _Game("r3k2r/8/8/8/8/8/8/R3K2R w kq - 0 1")  # white has no rights

    accepted = g._on_player_move(chess.Move.from_uci("e1g1"))

    assert accepted is False
    assert g.board.piece_at(chess.E1) == chess.Piece(chess.KING, chess.WHITE)
    assert len(g.board.move_stack) == 0
    assert g.move_state.castling_rook_pending is None
    assert g.correction_calls == 1


def test_castling_through_check_is_rejected():
    """King may not castle through an attacked square (f1).

    Why: a black rook on f8 attacks f1; the king passing through f1 makes
    kingside castling illegal. This must be rejected.

    How the regression manifests: castling is accepted despite f1 being attacked
    -> a core castling rule is violated.
    """
    g = _Game("5rk1/8/8/8/8/8/8/R3K2R w KQ - 0 1")  # black rook f8 attacks f1

    accepted = g._on_player_move(chess.Move.from_uci("e1g1"))

    assert accepted is False
    assert len(g.board.move_stack) == 0
    assert g.correction_calls == 1


def test_king_onto_rook_gesture_is_rejected():
    """Moving the king onto the rook (e1->h1) is not castling and is rejected.

    Why: the enforced gesture is the king moving two squares to g1/c1, not onto
    the rook. e1->h1 is not a legal move and must enter correction.

    How the regression manifests: e1h1 is interpreted/accepted as a castle ->
    a non-existent castling form is supported.
    """
    g = _Game()

    accepted = g._on_player_move(chess.Move(chess.E1, chess.H1))

    assert accepted is False
    assert len(g.board.move_stack) == 0
    assert g.move_state.castling_rook_pending is None
    assert g.correction_calls == 1


# ---------------------------------------------------------------------------
# A rook moved first is just a rook move - never rescued into a castle
# ---------------------------------------------------------------------------

def test_rook_first_is_not_rescued_into_a_castle():
    """Rf1 then e1g1 must NOT retroactively castle.

    Why: there is no rook-first castle. Moving the rook to f1 is the ordinary
    move Rf1; a following king move to g1 is illegal (rights lost, wrong turn)
    and must be rejected, not converted into a castle by undoing the rook move.

    How the regression manifests: the king move is accepted by reviving the
    deleted late-castling path -> the rook move is silently undone and a castle
    is produced from an illegal gesture.
    """
    g = _Game()

    rook_move_accepted = g._on_player_move(chess.Move.from_uci("h1f1"))
    assert rook_move_accepted is True
    assert g.board.peek().uci() == "h1f1"
    assert g.move_state.castling_rook_pending is None  # Rf1 is not a castle

    king_move_accepted = g._on_player_move(chess.Move.from_uci("e1g1"))

    assert king_move_accepted is False
    assert g.board.peek().uci() == "h1f1"  # rook move NOT undone
    assert g.move_state.castling_rook_pending is None
    assert g.correction_calls == 1
