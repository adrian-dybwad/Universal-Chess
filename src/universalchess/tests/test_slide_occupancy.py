"""Sliding a piece through non-destination squares must still accept a legal layout.

Background / why these tests exist
----------------------------------
Reed-switch boards emit PLACE on every empty square a piece rests on. A knight
moved "forward two then right one" therefore PLACes on the two orthogonal
intermediates of the L, which are never legal knight destinations. That PLACE
is formed as an illegal move, correction mode starts, and correction only
accepted occupancy that matched the *pre-move* position. The legal final
layout (knight on the L destination) was then rejected.

The same failure is any physical path whose intermediate squares are not legal
destinations of the lifted piece: all 16 knight L-path geometries (8
directions x 2 orthogonal orders) and slider detours off the attack ray.
Sliders resting on an intermediate that *is* a legal destination are a
different case (the short move is legal) and must keep being accepted.
"""

from typing import List, Optional

import chess
import pytest

from universalchess.managers.game.correction_mode import CorrectionMode
from universalchess.managers.game.field_events import (
    FieldEventContext,
    find_legal_move_matching_occupancy,
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

# Isolated knight on e4: all eight L destinations stay on the board, and the
# orthogonal intermediates of every L are empty (so each PLACE along a slide
# is visible to the sensors). Pawns on a2/h7 keep the position playable:
# K+N vs K is already insufficient material, so execute_complete_move would
# refuse the matched move.
KNIGHT_FEN = "4k3/7p/8/8/4N3/8/P7/4K3 w - - 0 1"
KNIGHT_ORIGIN = chess.E4

# Bishop on c1, a3 reachable on the diagonal, b2 empty so c1-a3 is legal.
# Detour c1-b1-a1-a2-a3 visits squares that are not bishop destinations.
# The h7 pawn keeps K+B vs K from being a terminal draw.
BISHOP_FEN = "4k3/7p/8/8/8/8/8/2B1K3 w - - 0 1"

# Eight knight deltas from e4, each with both orthogonal slide orders.
_KNIGHT_DELTAS = (
    (1, 2),
    (-1, 2),
    (1, -2),
    (-1, -2),
    (2, 1),
    (-2, 1),
    (2, -1),
    (-2, -1),
)


def _presence(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for sq in chess.SQUARES:
        state[sq] = 1 if board.piece_at(sq) is not None else 0
    return state


def _knight_dest(origin: int, df: int, dr: int) -> int:
    return chess.square(chess.square_file(origin) + df, chess.square_rank(origin) + dr)


def _knight_slide_path(origin: int, df: int, dr: int, *, long_first: bool) -> List[int]:
    """Squares visited after leaving ``origin``, including the destination.

    The L is walked on the orthogonal axes: either the two-step leg first
    ("forward two then right one") or the one-step leg first. Intermediate
    squares are never legal knight destinations from ``origin``.
    """
    file0 = chess.square_file(origin)
    rank0 = chess.square_rank(origin)
    sf = 1 if df > 0 else -1
    sr = 1 if dr > 0 else -1
    path: List[int] = []
    f, r = file0, rank0

    def walk(steps: int, step_f: int, step_r: int) -> None:
        nonlocal f, r
        for _ in range(steps):
            f += step_f
            r += step_r
            path.append(chess.square(f, r))

    long_is_file = abs(df) == 2
    if long_first:
        if long_is_file:
            walk(2, sf, 0)
            walk(1, 0, sr)
        else:
            walk(2, 0, sr)
            walk(1, sf, 0)
    else:
        if long_is_file:
            walk(1, 0, sr)
            walk(2, sf, 0)
        else:
            walk(1, sf, 0)
            walk(2, 0, sr)
    return path


def _all_knight_slide_cases():
    cases = []
    for df, dr in _KNIGHT_DELTAS:
        dest = _knight_dest(KNIGHT_ORIGIN, df, dr)
        for long_first in (True, False):
            order = "two-then-one" if long_first else "one-then-two"
            name = f"e4-{chess.square_name(dest)}-{order}"
            cases.append(pytest.param(df, dr, long_first, dest, id=name))
    return cases


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
    """Integrated harness wiring field events -> player -> player_moves."""

    def __init__(self, fen: str):
        self.game_state = reset_chess_game()
        self.game_state.set_position(fen)
        self.board = self.game_state.board
        self.move_state = MoveState()
        self.correction_mode = CorrectionMode()
        self.physical = self.game_state.to_piece_presence_state()
        self.board_module = _FakeBoard(self.physical)
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
            single_fast=lambda *a, **k: None,
        )

        self.player = _TestPlayer()
        self.player.set_move_callback(self._on_player_move)

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

    def slide(self, origin: int, path: List[int]) -> None:
        """Lift at origin, then PLACE/LIFT each intermediate, PLACE the last square."""
        self.lift(origin)
        for i, sq in enumerate(path):
            self.place(sq)
            if i < len(path) - 1:
                self.lift(sq)


def test_knight_slide_path_count_is_sixteen():
    """There are 16 knight L-path geometries: 8 destinations x 2 orthogonal orders.

    Why: the reported failure is one geometry ("forward two then right one").
    The other 15 are the same bug class and must stay covered. A count that
    drifts means a direction or order was dropped from the parametrized test.

    How a regression manifests: the generated case list is not 16, so some
    L-path is untested.
    """
    cases = _all_knight_slide_cases()
    assert len(cases) == 16
    legal = {
        m.to_square
        for m in chess.Board(KNIGHT_FEN).legal_moves
        if m.from_square == KNIGHT_ORIGIN
    }
    for case in cases:
        df, dr, long_first, dest = case.values
        path = _knight_slide_path(KNIGHT_ORIGIN, df, dr, long_first=long_first)
        assert path[-1] == dest
        for intermediate in path[:-1]:
            assert intermediate not in legal, (
                f"{chess.square_name(intermediate)} is a legal destination; "
                "this path would not exercise the non-destination PLACE bug"
            )


@pytest.mark.parametrize("df,dr,long_first,dest", _all_knight_slide_cases())
def test_knight_slide_accepts_legal_final_occupancy(df, dr, long_first, dest):
    """Sliding the e4 knight along either orthogonal L-path must play that move.

    Why: PLACE on an L intermediate is not a legal knight move, so it enters
    correction. The piece then rests on the legal destination; that occupancy
    is a legal resulting layout and must be accepted rather than held as a
    mismatch against the pre-move position.

    How a regression manifests: the knight is still on e4 (or correction stays
    active) after the destination PLACE, so the move is never on the board.
    """
    path = _knight_slide_path(KNIGHT_ORIGIN, df, dr, long_first=long_first)
    g = _Game(KNIGHT_FEN)
    g.slide(KNIGHT_ORIGIN, path)

    assert g.board.piece_at(dest) == chess.Piece(chess.KNIGHT, chess.WHITE)
    assert g.board.piece_at(KNIGHT_ORIGIN) is None
    assert g.board.peek() == chess.Move(KNIGHT_ORIGIN, dest)
    assert g.correction_mode.is_active is False


def test_occupancy_match_does_not_require_the_place_square_to_be_the_destination():
    """A PLACE on an L intermediate must still accept when occupancy is already the dest.

    Why: sensors can close the destination reed before the path square opens, so
    the PLACE event names an intermediate while getChessState already shows the
    legal layout. Matching only the PLACE square would reject a valid board.

    How a regression manifests: e4f6 is not played even though occupancy is
    already the post-f6 position when PLACE e6 fires.
    """
    g = _Game(KNIGHT_FEN)
    after = g.board.copy()
    after.push(chess.Move(KNIGHT_ORIGIN, chess.F6))

    g.lift(KNIGHT_ORIGIN)
    # Occupancy already at the destination; the PLACE names the last intermediate.
    g.physical[:] = _presence(after)
    g.board_module._physical = g.physical
    process_field_event(g._field_ctx(), 1, chess.E6, 0.0)

    assert g.board.peek() == chess.Move(KNIGHT_ORIGIN, chess.F6)
    assert g.board.piece_at(chess.F6) == chess.Piece(chess.KNIGHT, chess.WHITE)


def test_bishop_detour_off_the_ray_accepts_legal_final_occupancy():
    """A bishop slid around via non-destination squares must still play the ray move.

    Why: this is the same class as the knight L: intermediates b1/a1/a2 are not
    legal from c1, so they enter correction; a3 is a legal resulting layout.

    How a regression manifests: the bishop stays on c1 after PLACE a3.
    """
    g = _Game(BISHOP_FEN)
    g.slide(chess.C1, [chess.B1, chess.A1, chess.A2, chess.A3])

    assert g.board.piece_at(chess.A3) == chess.Piece(chess.BISHOP, chess.WHITE)
    assert g.board.piece_at(chess.C1) is None
    assert g.board.peek() == chess.Move.from_uci("c1a3")
    assert g.correction_mode.is_active is False


def test_bishop_resting_on_a_legal_intermediate_is_still_the_short_move():
    """A slider that settles on a legal dest along its ray must keep that short move.

    Why: occupancy matching any legal layout must not wait for a further square
    the player never reached. c1-d2 is legal; accepting c1-e3 instead would
    skip a ply the player did not play.

    How a regression manifests: the board has the bishop on e3 (or no move)
    after PLACE d2.
    """
    g = _Game(BISHOP_FEN)
    g.lift(chess.C1)
    g.place(chess.D2)

    assert g.board.peek() == chess.Move.from_uci("c1d2")
    assert g.board.piece_at(chess.D2) == chess.Piece(chess.BISHOP, chess.WHITE)
    assert g.board.piece_at(chess.E3) is None


def test_illegal_settled_occupancy_is_not_accepted_as_a_move():
    """Parking the knight on a non-destination must not invent a legal move.

    Why: occupancy matching is only for a layout that is some legal resulting
    position. e5 is not one; accepting e4f6 (or any other L) would play a move
    the piece is not on.

    How a regression manifests: a move is pushed (peek is not empty) while the
    knight is physically on e5.
    """
    g = _Game(KNIGHT_FEN)
    g.lift(KNIGHT_ORIGIN)
    g.place(chess.E5)

    assert g.board.piece_at(KNIGHT_ORIGIN) == chess.Piece(chess.KNIGHT, chess.WHITE)
    assert g.board.move_stack == []
    assert g.correction_mode.is_active is True


def test_find_occupancy_match_is_unique_among_knight_destinations():
    """Occupancy of e4f6 must not also match e4d6 (or any other L).

    Why: the correction/PLACE shortcut accepts a unique occupancy match. If
    two L-moves compared equal, the wrong knight destination would be played.

    How a regression manifests: find_legal_move_matching_occupancy returns
    d6 (or None) for an f6 layout.
    """
    board = chess.Board(KNIGHT_FEN)
    after = board.copy()
    after.push(chess.Move(KNIGHT_ORIGIN, chess.F6))

    matched = find_legal_move_matching_occupancy(
        board, _presence(after), _presence, from_square=KNIGHT_ORIGIN
    )
    assert matched == chess.Move(KNIGHT_ORIGIN, chess.F6)


def test_find_occupancy_match_strips_promotion_when_four_promotions_share_presence():
    """a7a8q/r/b/n are one occupancy; the match must not pick a piece.

    Why: presence sensing cannot tell which promotion was placed. Returning a
    fully specified queen would skip the chooser (the promotion pipeline
    regression). The shortcut must yield promotion=None.

    How a regression manifests: matched.promotion is QUEEN (or another piece).
    """
    board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
    after = board.copy()
    after.push(chess.Move.from_uci("a7a8q"))

    matched = find_legal_move_matching_occupancy(board, _presence(after), _presence)
    assert matched is not None
    assert matched.from_square == chess.A7
    assert matched.to_square == chess.A8
    assert matched.promotion is None
