"""Player move submission and completion helpers for GameManager.

This module extracts the player-submitted move handling pipeline from `GameManager`:
- destination-only recovery (missed lift)
- promotion completion
- legality validation + execution
- correction mode entry for illegal moves
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import chess

from universalchess.board.logging import log
from universalchess.utils.led import LedCallbacks

BOARD_SIZE = 64


def _castling_rook_move(board: chess.Board, move: chess.Move):
    """Return the (rook_from, rook_to) squares for a castling king move.

    Must be called before the move is pushed (legality query needs the pre-move
    board). Returns None when ``move`` is not a castling move.

    The rook always lands on the f-file (kingside) or d-file (queenside) of the
    king's rank. Its origin depends on the variant:

    - Standard chess: the rook is in the corner (kingside h-file, queenside
      a-file), and ``move`` is the king's two-square move.
    - Chess960: castling is encoded king-onto-rook, so ``move.to_square`` is the
      rook's own square (which is not necessarily a corner). ``on_player_move``
      resolves the physical gesture to this canonical form before this is called.
    """
    if not board.is_castling(move):
        return None
    rank = chess.square_rank(move.from_square)
    kingside = board.is_kingside_castling(move)
    rook_to = chess.square(5, rank) if kingside else chess.square(3, rank)  # f / d file
    if board.chess960:
        rook_from = move.to_square  # king-onto-rook encoding: to_square is the rook
    else:
        rook_from = chess.square(7, rank) if kingside else chess.square(0, rank)  # h / a
    return rook_from, rook_to


def _is_king_onto_rook_castle(board: chess.Board, move: chess.Move) -> bool:
    """Return True for an unsupported "king onto rook" castling gesture.

    python-chess accepts both the king's two-square move (e1g1/e1c1) and the
    king-onto-rook form (e1h1/e1a1) as legal castling in standard chess. Only the
    two-square gesture is supported for standard chess, so the rook-square form is
    rejected there.

    In Chess960 the king-onto-rook form is the ONLY canonical representation
    (python-chess does not emit a two-square form when the destination files
    differ from standard), so it must never be rejected for a 960 game.
    """
    if board.chess960:
        return False
    if not board.is_castling(move):
        return False
    return abs(chess.square_file(move.to_square) - chess.square_file(move.from_square)) != 2


def _resolve_castling_gesture(board: chess.Board, move: chess.Move) -> chess.Move:
    """Translate a physical Chess960 castling gesture to python-chess's move.

    python-chess only accepts the king-onto-rook encoding for Chess960 castling
    (``move.to_square`` is the rook's square). A player at the physical board may
    instead slide the king to its final square (g-file kingside, c-file
    queenside). Map either gesture to the matching legal castling move so it is
    recognised instead of being rejected as illegal.

    No-op for standard chess, for non-king moves, and for promotions. Returns
    ``move`` unchanged when it does not correspond to a legal castling.
    """
    if not board.chess960 or move.promotion is not None:
        return move
    king_square = board.king(board.turn)
    if king_square is None or move.from_square != king_square:
        return move
    for castling in board.generate_castling_moves():
        if castling.from_square != move.from_square:
            continue
        rank = chess.square_rank(castling.from_square)
        king_dest = (
            chess.square(6, rank)  # g-file
            if board.is_kingside_castling(castling)
            else chess.square(2, rank)  # c-file
        )
        # Accept either the canonical king-onto-rook target or the king's final
        # square as the physical gesture for this castling.
        if move.to_square in (castling.to_square, king_dest):
            return castling
    return move


@dataclass(frozen=True)
class PlayerMoveContext:
    chess_board: chess.Board
    game_state: object
    move_state: object
    board_module: object
    led: LedCallbacks  # LED control callbacks

    # State accessors
    get_game_db_id_fn: Callable[[], int]

    # GameManager helpers
    switch_turn_with_event_fn: Callable[[], None]
    enqueue_post_move_tasks_fn: Callable[..., None]
    enter_correction_mode_fn: Callable[[], None]
    chess_board_to_state_fn: Callable[[chess.Board], Optional[list]]
    provide_correction_guidance_fn: Callable[[list, list], None]

    # Promotion UI
    set_is_showing_promotion_fn: Callable[[bool], None]
    on_promotion_needed_fn: Optional[Callable[[bool], str]]


def _abort_move_attempt(ctx: PlayerMoveContext) -> None:
    """Reject a move attempt before it is applied, leaving clean state.

    Used by every non-success exit of execute_complete_move so that cleanup is a
    guaranteed post-condition rather than tied to the success path. Resets the
    in-progress lift tracking (so a stale source_square cannot bleed into the
    next physical event) and recovers via correction mode + guidance, mirroring
    on_player_move's illegal-move handling: when a move is rejected at the push,
    a piece may be sitting on a square the logical board never accepted.
    """
    ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
    ctx.led.off()
    ctx.move_state.reset()
    ctx.enter_correction_mode_fn()
    current_state = ctx.board_module.getChessState()
    expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
    if current_state is not None and expected_state is not None:
        ctx.provide_correction_guidance_fn(current_state, expected_state)


def execute_complete_move(ctx: PlayerMoveContext, move: chess.Move) -> None:
    """Execute a complete move submitted by a player (behavior-identical to prior GameManager impl)."""
    outcome = ctx.chess_board.outcome(claim_draw=True)
    if outcome is not None:
        log.warning(
            "[GameManager._execute_complete_move] Attempted to execute move after game ended. "
            f"Result: {ctx.chess_board.result()}, Termination: {outcome.termination}"
        )
        _abort_move_attempt(ctx)
        return

    move_uci = move.uci()
    target_square = move.to_square

    fen_before_move = str(ctx.chess_board.fen())
    is_first_move = ctx.get_game_db_id_fn() < 0

    # Castling is the king's two-square move; capture the rook-follow before the
    # push so the rook physically following the king is recognised as completion.
    rook_follow = _castling_rook_move(ctx.chess_board, move)

    try:
        ctx.game_state.push_move(move)
    except (ValueError, AssertionError) as e:
        log.error(f"[GameManager._execute_complete_move] Chess engine push failed: {move_uci}. Error: {e}")
        _abort_move_attempt(ctx)
        return

    ctx.led.off()
    # Move/place confirmation beep: fires with the place-confirmation flash on the
    # target square, so it is categorised as a piece_event (controlled by the
    # Sound menu's "Piece Events" switch), not a game_event (check/checkmate).
    ctx.board_module.beep(ctx.board_module.SOUND_GENERAL, event_type="piece_event")
    ctx.led.single_fast(target_square, repeat=1)

    fen_after_move = str(ctx.chess_board.fen())

    game_ended = False
    result_string = None
    termination = None
    outcome = ctx.chess_board.outcome(claim_draw=True)
    if outcome is not None:
        game_ended = True
        result_string = str(ctx.chess_board.result())
        termination = str(outcome.termination)

    ctx.move_state.reset()

    if rook_follow is not None:
        # Arm the rook-follow: the player must now slide the rook to its castling
        # square. field_events recognises this as the castle's completion instead
        # of an illegal interaction. Guide the rook with the from/to LEDs.
        ctx.move_state.castling_rook_pending = rook_follow
        rook_from, rook_to = rook_follow
        ctx.led.from_to(rook_from, rook_to, repeat=0)

    if not game_ended:
        ctx.switch_turn_with_event_fn()

    ctx.enqueue_post_move_tasks_fn(
        target_square=target_square,
        move_uci=move_uci,
        fen_before_move=fen_before_move,
        fen_after_move=fen_after_move,
        is_first_move=is_first_move,
        game_ended=game_ended,
        result_string=result_string,
        termination=termination,
    )


def complete_destination_only_move(ctx: PlayerMoveContext, destination: int) -> Optional[chess.Move]:
    """Complete a destination-only move by finding the missing source square."""
    current_state = ctx.board_module.getChessState()
    if current_state is None:
        log.warning("[GameManager._complete_destination_only_move] Could not get physical board state")
        return None

    expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
    if expected_state is None:
        log.warning("[GameManager._complete_destination_only_move] Could not get expected game state")
        return None

    source_squares = []
    for sq in range(BOARD_SIZE):
        if sq == destination:
            continue
        if expected_state[sq] == 1 and current_state[sq] == 0:
            source_squares.append(sq)

    if len(source_squares) == 0:
        log.warning(
            "[GameManager._complete_destination_only_move] No source square found for destination "
            f"{chess.square_name(destination)}"
        )
        return None

    if len(source_squares) > 1:
        legal_sources = []
        for src in source_squares:
            test_move = chess.Move(src, destination)
            if test_move in ctx.chess_board.legal_moves:
                legal_sources.append(src)
            for promo in [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]:
                test_move = chess.Move(src, destination, promotion=promo)
                if test_move in ctx.chess_board.legal_moves and src not in legal_sources:
                    legal_sources.append(src)

        if len(legal_sources) == 1:
            source_squares = legal_sources
            log.info(
                "[GameManager._complete_destination_only_move] Disambiguated to legal source: "
                f"{chess.square_name(source_squares[0])}"
            )
        else:
            log.warning(
                "[GameManager._complete_destination_only_move] Ambiguous sources for destination "
                f"{chess.square_name(destination)}: {[chess.square_name(sq) for sq in source_squares]}"
            )
            return None

    source = source_squares[0]
    completed_move = chess.Move(source, destination)
    log.info(
        "[GameManager._complete_destination_only_move] MISSED LIFT RECOVERY: Found source "
        f"{chess.square_name(source)} for destination {chess.square_name(destination)}, "
        f"completed move: {completed_move.uci()}"
    )
    return completed_move


def check_and_handle_promotion(ctx: PlayerMoveContext, move: chess.Move) -> Optional[chess.Move]:
    """If move is a pawn-to-last-rank without promotion, request a piece and return promotion move."""
    piece = ctx.chess_board.piece_at(move.from_square)
    if piece is None or piece.piece_type != chess.PAWN:
        return None

    to_rank = chess.square_rank(move.to_square)
    is_white_promotion = piece.color == chess.WHITE and to_rank == 7
    is_black_promotion = piece.color == chess.BLACK and to_rank == 0
    if not (is_white_promotion or is_black_promotion):
        return None

    log.info(f"[GameManager._check_and_handle_promotion] Promotion detected for {move.uci()}")
    ctx.board_module.beep(ctx.board_module.SOUND_GENERAL, event_type="game_event")

    ctx.set_is_showing_promotion_fn(True)
    try:
        if ctx.on_promotion_needed_fn:
            promotion_choice = ctx.on_promotion_needed_fn(is_white_promotion)
        else:
            log.warning("[GameManager._check_and_handle_promotion] No promotion callback, defaulting to queen")
            promotion_choice = "q"
    finally:
        ctx.set_is_showing_promotion_fn(False)

    promotion_map = {
        "q": chess.QUEEN,
        "r": chess.ROOK,
        "b": chess.BISHOP,
        "n": chess.KNIGHT,
    }
    promotion_piece = promotion_map.get(str(promotion_choice).lower(), chess.QUEEN)

    promotion_move = chess.Move(move.from_square, move.to_square, promotion=promotion_piece)
    log.info(f"[GameManager._check_and_handle_promotion] Created promotion move: {promotion_move.uci()}")
    return promotion_move


def on_player_move(ctx: PlayerMoveContext, move: chess.Move) -> bool:
    """Handle a player-submitted move; return True if accepted and executed."""
    log.info(f"[GameManager._on_player_move] Received move: {move.uci()}")

    if move.from_square == move.to_square:
        completed_move = complete_destination_only_move(ctx, move.to_square)
        if completed_move is None:
            log.warning(
                "[GameManager._on_player_move] Could not complete destination-only move to "
                f"{chess.square_name(move.to_square)}"
            )
            return False
        move = completed_move
        log.info(f"[GameManager._on_player_move] Completed destination-only move: {move.uci()}")

    move_to_execute = move
    if move.promotion is None:
        promotion_move = check_and_handle_promotion(ctx, move)
        if promotion_move:
            move_to_execute = promotion_move
            log.info(f"[GameManager._on_player_move] Promotion handled: {move.uci()} -> {move_to_execute.uci()}")

    # Map a Chess960 castling gesture (king to its final square, or king onto the
    # rook) to python-chess's canonical king-onto-rook castling move so it is not
    # rejected as illegal. No-op for standard chess and non-castling moves.
    move_to_execute = _resolve_castling_gesture(ctx.chess_board, move_to_execute)

    if move_to_execute in ctx.chess_board.legal_moves and not _is_king_onto_rook_castle(ctx.chess_board, move_to_execute):
        log.info(f"[GameManager._on_player_move] Legal move, executing: {move_to_execute.uci()}")
        execute_complete_move(ctx, move_to_execute)
        return True

    log.warning(f"[GameManager._on_player_move] Illegal move: {move_to_execute.uci()}, entering correction mode")
    ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
    ctx.enter_correction_mode_fn()

    current_state = ctx.board_module.getChessState()
    expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
    if current_state is not None and expected_state is not None:
        ctx.provide_correction_guidance_fn(current_state, expected_state)

    return False


__all__ = [
    "PlayerMoveContext",
    "execute_complete_move",
    "complete_destination_only_move",
    "check_and_handle_promotion",
    "on_player_move",
    "_castling_rook_move",
    "_is_king_onto_rook_castle",
    "_resolve_castling_gesture",
]


