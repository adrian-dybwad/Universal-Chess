"""Field event routing for GameManager.

This module extracts the orchestration of physical board field events (LIFT/PLACE)
from `GameManager._process_field_event` while preserving behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import chess

from universalchess.board.logging import log
from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
from universalchess.state.chess_game import ChessGameState
from universalchess.utils.led import LedCallbacks

from .move_state import INVALID_SQUARE


@dataclass(frozen=True)
class FieldEventContext:
    chess_board: chess.Board
    move_state: object
    correction_mode: object
    player_manager: object
    board_module: object
    led: LedCallbacks

    # Callbacks
    event_callback: Optional[Callable]
    enter_correction_mode_fn: Callable[[], None]
    provide_correction_guidance_fn: Callable[[object, object], None]
    handle_field_event_in_correction_mode_fn: Callable[[int, int, float], None]
    handle_piece_event_without_player_fn: Callable[[int], None]
    on_piece_event_fn: Callable[[str, int, chess.Board], None]
    on_player_move_fn: Callable[[chess.Move], bool]
    handle_king_lift_resign_fn: Callable[[int, object], None]
    execute_pending_move_fn: Callable[[chess.Move], None]
    check_takeback_fn: Callable[[], bool]

    # Menu state and callbacks
    get_kings_in_center_menu_active_fn: Callable[[], bool]
    set_kings_in_center_menu_active_fn: Callable[[bool], None]
    on_kings_in_center_cancel_fn: Optional[Callable[[], None]]

    get_king_lift_resign_menu_active_fn: Callable[[], bool]
    set_king_lift_resign_menu_active_fn: Callable[[bool], None]
    on_king_lift_resign_cancel_fn: Optional[Callable[[], None]]

    # Expected state helper
    chess_board_to_state_fn: Callable[[chess.Board], Optional[bytearray]]

    # Setup mode (Chessnut): when active, the emulator tracks lift/place as a board
    # setup (relocations), so this module must NOT interpret them as chess moves.
    setup_mode_active_fn: Callable[[], bool] = lambda: False


def _place_is_noise_on_matching_board(ctx: FieldEventContext) -> bool:
    """Return True when a PLACE cannot be a move: occupancy already matches and nothing is in progress.

    Used to drop reed-bounce and trailing-duplicate PLACE events that would
    otherwise become destination-only moves and enter correction mode. Returns
    False when occupancy cannot be read, so a failed state query does not
    swallow a real PLACE.
    """
    if getattr(ctx.move_state, "source_square", INVALID_SQUARE) != INVALID_SQUARE:
        return False
    if getattr(ctx.move_state, "pending_move_source_lifted", INVALID_SQUARE) != INVALID_SQUARE:
        return False
    expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
    physical_state = ctx.board_module.getChessState()
    if expected_state is None or physical_state is None:
        return False
    try:
        physical_len = len(physical_state)
    except TypeError:
        # Unreadable occupancy (tests, a failed state query) must not swallow a PLACE.
        return False
    if physical_len != 64:
        return False
    return ChessGameState.states_match(physical_state, expected_state)


def process_field_event(
    ctx: FieldEventContext, piece_event: int, field: int, time_in_seconds: float
) -> None:
    """Process one field event (LIFT=0, PLACE=1)."""
    field_name = chess.square_name(field)

    # Piece color selection rules:
    # - LIFT: use color_at(field)
    # - PLACE: use stored source_piece_color (captures), fallback to color_at(field)
    if piece_event == 0:
        if ctx.event_callback is not None:
            ctx.event_callback(EVENT_LIFT_PIECE, piece_event, field, time_in_seconds)
        piece_color = ctx.chess_board.color_at(field)
    else:
        if ctx.event_callback is not None:
            ctx.event_callback(EVENT_PLACE_PIECE, piece_event, field, time_in_seconds)
        if getattr(ctx.move_state, "source_piece_color", None) is not None:
            piece_color = ctx.move_state.source_piece_color
        else:
            piece_color = ctx.chess_board.color_at(field)

    # In setup mode the emulator has already been notified above (event_callback)
    # and is tracking this lift/place as a board-setup relocation. Suppress all
    # move interpretation so arbitrary setup manipulation is never read as a move.
    if ctx.setup_mode_active_fn():
        log.debug(
            f"[GameManager.receive_field] setup mode active - suppressing move "
            f"interpretation for {'LIFT' if piece_event == 0 else 'PLACE'} {field_name}"
        )
        return

    log.info(
        f"[GameManager.receive_field] piece_event={piece_event} field={field} fieldname={field_name} "
        f"color_at={'White' if piece_color else 'Black'} time_in_seconds={time_in_seconds}"
    )

    is_lift = piece_event == 0

    # ===========================================================================
    # KING LIFT RESIGN: Always handle this gesture, even during correction mode
    # or Hand+Brain mode. This is a board-level safety mechanism.
    # ===========================================================================
    if is_lift:
        # Check if a king was lifted - triggers resign timer
        ctx.handle_king_lift_resign_fn(field, piece_color)
    else:
        # Cancel king-lift resign timer on any piece placement
        if ctx.move_state.king_lift_timer is not None:
            ctx.move_state._cancel_king_lift_timer()
            log.debug("[process_field_event] Cancelled king-lift resign timer on PLACE")

            if ctx.get_king_lift_resign_menu_active_fn():
                log.info("[process_field_event] King placed - cancelling resign menu")
                ctx.set_king_lift_resign_menu_active_fn(False)
                if ctx.on_king_lift_resign_cancel_fn:
                    ctx.on_king_lift_resign_cancel_fn()

            ctx.move_state.king_lifted_square = INVALID_SQUARE
            ctx.move_state.king_lifted_color = None

    # ===========================================================================
    # TAKEBACK DETECTION: After every PLACE event, check if the physical board
    # matches the position before the last move. This catches takebacks regardless
    # of how the pieces were moved (any order, with or without preceding LIFTs).
    # ===========================================================================
    if not is_lift and len(ctx.chess_board.move_stack) > 0:
        if ctx.check_takeback_fn():
            log.info("[process_field_event] Takeback detected and executed")
            return

    # ===========================================================================
    # CASTLING ROOK-FOLLOW: after the king's two-square move the rook must be
    # physically moved to its castling square. That follow-up is the completion of
    # the castle, not a new move - recognise it here (before the pending-move and
    # no-legal-moves guards, which would otherwise see the rook as an illegal piece
    # on the opponent's turn). Any interaction other than the expected rook move is
    # treated as invalid and handed to correction mode.
    # ===========================================================================
    rook_pending = getattr(ctx.move_state, "castling_rook_pending", None)
    if rook_pending is not None:
        rook_from, rook_to = rook_pending
        if is_lift:
            if field == rook_from:
                return  # expected: wait for the rook to be placed on its castling square
            log.warning(
                f"[process_field_event] Expected castling rook from "
                f"{chess.square_name(rook_from)}, but {chess.square_name(field)} was lifted - "
                "entering correction mode"
            )
        elif field == rook_to:
            ctx.move_state.castling_rook_pending = None
            log.info(
                f"[process_field_event] Castling completed: rook placed on {chess.square_name(rook_to)}"
            )
            ctx.board_module.beep(ctx.board_module.SOUND_GENERAL, event_type="piece_event")
            ctx.led.off()
            ctx.led.single_fast(rook_to, repeat=1)
            return
        else:
            log.warning(
                f"[process_field_event] Castling rook expected on {chess.square_name(rook_to)}, "
                f"but placed on {chess.square_name(field)} - entering correction mode"
            )

        # Anything other than the expected rook move invalidates the rook-follow;
        # hand off to correction mode for full board reconciliation.
        ctx.move_state.castling_rook_pending = None
        ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
        ctx.enter_correction_mode_fn()
        current_state = ctx.board_module.getChessState()
        expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
        if current_state is not None and expected_state is not None:
            ctx.provide_correction_guidance_fn(current_state, expected_state)
        return

    def _pending_move_context():
        """Build pending-move context safely.

        This function centralizes all pending-move derived flags so they stay consistent
        and avoids dereferencing ctx.player_manager when not present.
        """
        if not ctx.player_manager:
            return None, False, None
        pending = ctx.player_manager.get_current_pending_move(ctx.chess_board)
        is_capture = pending is not None and ctx.chess_board.is_capture(pending)
        capture_sq = pending.to_square if is_capture else None
        return pending, is_capture, capture_sq

    def _physical_matches_expected_after_pending_move(
        *,
        pending: chess.Move,
        require_capture_square_event: bool,
        capture_square: int | None,
    ) -> bool:
        """Return True if the physical board matches the expected post-move state.

        For captures, this can be gated on whether we've seen any event (LIFT or PLACE)
        on the capture square.
        """
        if require_capture_square_event:
            if capture_square is None:
                return False
            if not ctx.move_state.has_seen_capture_square_event(capture_square):
                return False

        expected_board_after = ctx.chess_board.copy()
        expected_board_after.push(pending)
        expected_state_after = ctx.chess_board_to_state_fn(expected_board_after)
        current_physical_state = ctx.board_module.getChessState()
        return (
            expected_state_after is not None
            and current_physical_state is not None
            and ChessGameState.states_match(current_physical_state, expected_state_after)
        )

    pending_move, is_pending_capture, pending_capture_square = _pending_move_context()

    def _try_execute_normal_move_from_physical_state(*, placed_square: int) -> bool:
        """Attempt to execute a normal (non-pending) move based on physical board state.

        This is the normal-move analogue of the pending-move "post-state match" shortcut.
        It exists to make move acceptance robust against noisy event sequences:
        if the physical board matches the expected state after a legal move from the lifted
        source square, accept the move even if other pieces were bumped and replaced.
        
        IMPORTANT: Moves to the placed_square are checked first to avoid ambiguity
        when piece presence alone cannot distinguish between moves (e.g., g5f7 vs g5h7
        when both result in the same presence pattern due to sensor timing).
        """
        source_square = getattr(ctx.move_state, "source_square", INVALID_SQUARE)
        if source_square == INVALID_SQUARE:
            return False

        current_physical_state = ctx.board_module.getChessState()
        if current_physical_state is None:
            return False

        # If the player put the lifted piece back on its source square and the physical board
        # matches the logical board, treat this as a cancelled move and clear the source square.
        expected_state_now = ctx.chess_board_to_state_fn(ctx.chess_board)
        if expected_state_now is not None and ChessGameState.states_match(current_physical_state, expected_state_now):
            if placed_square == source_square:
                ctx.move_state.source_square = INVALID_SQUARE
            return False

        candidate_moves = [m for m in ctx.chess_board.legal_moves if m.from_square == source_square]
        
        # Prioritize moves to the placed_square - this is the most likely intended move.
        # Sort so moves to placed_square come first, then others.
        candidate_moves.sort(key=lambda m: (0 if m.to_square == placed_square else 1))
        
        # Only consider moves to the placed_square - this is the destination the user chose.
        # This prevents selecting a wrong move that happens to match the board state by coincidence
        # (e.g., g5h7 selected when user placed on f7, just because both result in similar presence patterns).
        move_to_placed = chess.Move(source_square, placed_square)
        
        # Check if there's a legal move to the placed square (with any promotion)
        candidate = None
        for move in candidate_moves:
            if move.to_square == placed_square:
                candidate = move
                break
        
        if candidate is None:
            return False
        
        expected_board_after = ctx.chess_board.copy()
        expected_board_after.push(candidate)
        expected_state_after = ctx.chess_board_to_state_fn(expected_board_after)
        if expected_state_after is None:
            return False
        
        if ChessGameState.states_match(current_physical_state, expected_state_after):
            # Presence sensing cannot tell which piece a promotion produced: all
            # four promotions of source->placed yield the same occupancy, so
            # `candidate` is merely whichever `legal_moves` listed first (the
            # queen). Committing it would silently auto-queen and skip the piece
            # chooser. Submit the move WITHOUT a promotion piece so
            # on_player_move's check_and_handle_promotion -- the single place that
            # asks the user which piece to promote to -- runs. Non-promotion moves
            # are unaffected (candidate.promotion is None, submitted unchanged).
            submitted_move = (
                chess.Move(candidate.from_square, candidate.to_square)
                if candidate.promotion is not None
                else candidate
            )
            log.info(
                f"[GameManager.receive_field] Physical board matches expected state after {candidate.uci()} - "
                f"accepting normal move {submitted_move.uci()} directly"
            )
            accepted = bool(ctx.on_player_move_fn(submitted_move))
            # Defensive: clear the source square on acceptance so subsequent PLACE events
            # during a noisy sequence don't attempt to "re-accept" the same move.
            if accepted:
                ctx.move_state.source_square = INVALID_SQUARE
            return accepted

        return False

    # When a resign menu is active (kings-in-center or king-lift), check for:
    # 1. Board corrected (pieces returned to position) → cancel menu
    # 2. LIFT event → cancel menu and enter correction mode to guide pieces back
    if ctx.get_kings_in_center_menu_active_fn() or ctx.get_king_lift_resign_menu_active_fn():
        expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
        current_state = ctx.board_module.getChessState()

        if current_state is not None and expected_state is not None:
            if ChessGameState.states_match(current_state, expected_state):
                log.info("[GameManager.receive_field] Board corrected while resign menu active - cancelling menu")
                if ctx.get_kings_in_center_menu_active_fn():
                    ctx.set_kings_in_center_menu_active_fn(False)
                    if ctx.on_kings_in_center_cancel_fn:
                        ctx.on_kings_in_center_cancel_fn()
                if ctx.get_king_lift_resign_menu_active_fn():
                    ctx.set_king_lift_resign_menu_active_fn(False)
                    ctx.move_state._cancel_king_lift_timer()
                    ctx.move_state.king_lifted_square = INVALID_SQUARE
                    ctx.move_state.king_lifted_color = None
                    if ctx.on_king_lift_resign_cancel_fn:
                        ctx.on_king_lift_resign_cancel_fn()
                return

        if is_lift:
            log.info(
                "[GameManager.receive_field] Piece lifted while resign menu active - cancelling menu and entering correction mode"
            )
            if ctx.get_kings_in_center_menu_active_fn():
                ctx.set_kings_in_center_menu_active_fn(False)
                if ctx.on_kings_in_center_cancel_fn:
                    ctx.on_kings_in_center_cancel_fn()
            if ctx.get_king_lift_resign_menu_active_fn():
                ctx.set_king_lift_resign_menu_active_fn(False)
                ctx.move_state._cancel_king_lift_timer()
                ctx.move_state.king_lifted_square = INVALID_SQUARE
                ctx.move_state.king_lifted_color = None
                if ctx.on_king_lift_resign_cancel_fn:
                    ctx.on_king_lift_resign_cancel_fn()
            ctx.enter_correction_mode_fn()
            if current_state is not None and expected_state is not None:
                ctx.provide_correction_guidance_fn(current_state, expected_state)
            return

        return  # Skip all other processing while menu is active (PLACE events)

    # Handle correction mode - piece events help correct the board
    if ctx.correction_mode.is_active:
        # IMPORTANT: Even while correction mode is active, allow the forced/pending move
        # to be executed if the physical board already matches the expected post-move state.
        #
        # This prevents a deadlock where an unrelated bump triggers correction mode mid-sequence,
        # and then placing the forced move on the correct target never gets accepted because
        # correction mode compares against the pre-move logical state.
        if (
            pending_move is not None
            and piece_event == 1  # PLACE
        ):
            if (not is_pending_capture) or (
                pending_capture_square is not None
                and ctx.move_state.has_seen_capture_square_event(pending_capture_square)
            ):
                if _physical_matches_expected_after_pending_move(
                    pending=pending_move,
                    require_capture_square_event=is_pending_capture,
                    capture_square=pending_capture_square,
                ):
                    log.info(
                        f"[GameManager.receive_field] (correction_mode) Physical board matches expected state after "
                        f"{pending_move.uci()} - executing pending move directly"
                    )
                    ctx.execute_pending_move_fn(pending_move)
                    return

        ctx.handle_field_event_in_correction_mode_fn(piece_event, field, time_in_seconds)
        return

    # If no PlayerManager, handle piece events directly
    if not ctx.player_manager:
        if not is_lift:
            ctx.handle_piece_event_without_player_fn(field)
        return

    # BOARD STATE VALIDATION FOR PENDING MOVES (must happen BEFORE forwarding to player)
    # If there's a pending move (engine/Lichess) and the physical board matches the
    # expected state AFTER the move, execute it directly regardless of event sequence.
    # This handles nudges, missed lifts, or any other noise - if the board is right, the move succeeded.
    # 
    # This check MUST happen before on_piece_event_fn() because otherwise the player
    # may form an incorrect move from a noisy event sequence and report an error.
    #
    # For captures: require at least one event (LIFT or PLACE) on the capture square
    # before using the board state shortcut. This ensures the user has interacted with
    # the captured piece (even if some events were missed/fumbled).
    if pending_move is not None:
        is_capture = ctx.chess_board.is_capture(pending_move)
        capture_square = pending_move.to_square if is_capture else None
        
        # For captures, record any event on the capture square (LIFT or PLACE)
        if is_capture and field == capture_square:
            if not ctx.move_state.has_seen_capture_square_event(capture_square):
                ctx.move_state.record_capture_square_event(capture_square)
                log.debug(f"[GameManager.receive_field] Recorded {'LIFT' if is_lift else 'PLACE'} event on "
                         f"capture square {chess.square_name(capture_square)}")
        
        # Board state check only on PLACE events (not LIFT)
        if not is_lift:
            # For captures: only use shortcut if we've seen an event on the capture square
            can_use_shortcut = not is_capture or ctx.move_state.has_seen_capture_square_event(capture_square)
            
            if can_use_shortcut:
                if _physical_matches_expected_after_pending_move(
                    pending=pending_move,
                    require_capture_square_event=is_capture,
                    capture_square=capture_square,
                ):
                        log.info(
                            f"[GameManager.receive_field] Physical board matches expected state after {pending_move.uci()} - "
                            "executing pending move directly"
                        )
                        ctx.execute_pending_move_fn(pending_move)
                        return
            elif is_capture:
                log.debug(f"[GameManager.receive_field] Pending capture {pending_move.uci()} - "
                         "waiting for event on capture square")

    # Track normal move source square on LIFT so bumps can be tolerated and the final board state can be used.
    if (
        pending_move is None
        and is_lift
        and piece_color is not None
        and piece_color == ctx.chess_board.turn
        and getattr(ctx.move_state, "source_square", INVALID_SQUARE) == INVALID_SQUARE
    ):
        # Only treat this as a move start if the square has at least one legal move.
        if any(move.from_square == field for move in ctx.chess_board.legal_moves):
            ctx.move_state.source_square = field

    # PLACE with no move in progress on a board that already matches: reed bounce,
    # trailing duplicate after occupancy already accepted the move, or a ghost
    # PLACE on an occupied square. Forming a destination-only move from this
    # enters correction mode -- Lichess rejects it unless the square is the
    # pending destination, which a bounce on the source or after turn-switch
    # never is. A real missed-lift move vacates a source square, so occupancy
    # no longer matches and this guard does not fire. Checked before
    # _try_execute_normal_move_from_physical_state, which clears source_square
    # on a put-back; that PLACE must still reach the player so the lift buffer
    # is cleared.
    if not is_lift and _place_is_noise_on_matching_board(ctx):
        log.info(
            f"[GameManager.receive_field] PLACE {field_name} while physical board already "
            "matches logical position and no move is in progress - ignoring"
        )
        return

    # BOARD STATE VALIDATION FOR NORMAL MOVES (must happen BEFORE forwarding to player)
    if pending_move is None and not is_lift:
        if _try_execute_normal_move_from_physical_state(placed_square=field):
            return

    # Check for "wrong piece lifted during forced move" on LIFT events
    # If there's a pending move (engine/Lichess) and the user lifts a piece that is NOT
    # the source of the pending move AND NOT the capture target, enter correction mode.
    # This prevents confusion when the user picks up the wrong piece during a forced move.
    # 
    # Valid lifts during a pending move:
    # - The piece that needs to move (pending_move.from_square)
    # - The piece being captured (pending_move.to_square, if it's a capture)
    #
    # IMPORTANT: Skip this check if the user has already lifted the correct piece for
    # the pending move. When the forced move source has been lifted and the user is now
    # bumping/adjusting another piece (e.g., removing the captured piece), we should
    # not trigger an error.
    pending_move_in_progress = (
        ctx.move_state.pending_move_source_lifted != INVALID_SQUARE
        and (not is_pending_capture or (pending_capture_square is not None and ctx.move_state.has_seen_capture_square_event(pending_capture_square)))
    )
    if is_lift and pending_move is not None and piece_color is not None and not pending_move_in_progress:
        pending_from_square = pending_move.from_square
        pending_to_square = pending_move.to_square
        is_pending_capture_local = ctx.chess_board.is_capture(pending_move)
        
        # Allow lifting from: source square OR capture target square
        is_valid_lift = (field == pending_from_square or 
                         (is_pending_capture_local and field == pending_to_square))
        
        # Track when the correct source piece is lifted for the pending move
        if is_valid_lift and field == pending_from_square:
            ctx.move_state.pending_move_source_lifted = pending_from_square
            log.debug(f"[GameManager.receive_field] Pending move source {chess.square_name(pending_from_square)} lifted - "
                     "subsequent bumps/adjustments allowed")
        
        if not is_valid_lift:
            pending_piece = ctx.chess_board.piece_at(pending_from_square)
            pending_piece_name = chess.piece_name(pending_piece.piece_type) if pending_piece else "piece"
            log.warning(
                f"[GameManager.receive_field] Wrong piece lifted at {chess.square_name(field)} - "
                f"expected {pending_piece_name} at {chess.square_name(pending_from_square)} for pending move {pending_move.uci()} - "
                "entering correction mode"
            )
            ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
            ctx.enter_correction_mode_fn()
            current_state = ctx.board_module.getChessState()
            expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
            if current_state is not None and expected_state is not None:
                ctx.provide_correction_guidance_fn(current_state, expected_state)
            return

    # Check for "piece with no legal moves" on LIFT events
    # ANY piece lifted without valid moves should trigger correction mode - not just
    # the current player's piece. This handles:
    # - Current player lifting a blocked/pinned piece
    # - Player lifting opponent's piece (which has no legal moves since it's not their turn)
    # - Lifting an empty square (piece_color is None, handled separately)
    if is_lift and piece_color is not None:
        # During a forced/pending move sequence, once the correct source piece has been lifted,
        # allow subsequent bumps/adjustments without triggering correction mode based on
        # the current position's legal moves (which are turn-dependent).
        #
        # Example: black forced move is pending, user lifts black source piece, then bumps a
        # white pawn. That pawn has no legal moves because it's not White's turn, but this
        # should not force correction mode mid-sequence.
        #
        # IMPORTANT: For pending CAPTURES, lifting the capture square first (to remove the captured piece)
        # is a normal/valid sequence. Do not treat that as "no legal moves" just because it's the
        # opponent's piece on the opponent's turn.
        allow_bumps_without_legal_move_check = False
        if pending_move is not None:
            if is_pending_capture and pending_capture_square is not None and field == pending_capture_square:
                allow_bumps_without_legal_move_check = True
            else:
                allow_bumps_without_legal_move_check = (
                    ctx.move_state.pending_move_source_lifted != INVALID_SQUARE
                    and (
                        (not is_pending_capture)
                        or (
                            pending_capture_square is not None
                            and ctx.move_state.has_seen_capture_square_event(pending_capture_square)
                        )
                    )
                )

        # If the lifted piece is the opponent's piece, this is often the first step of a legal capture
        # (players frequently lift the captured piece before moving their own piece).
        #
        # Do not enter correction mode just because the opponent piece has no legal moves on this turn;
        # instead, only treat it as an error if there is no legal capture to this square.
        # If a normal move is already in progress (source lifted), tolerate bumps without
        # forcing correction mode due to turn-dependent legal move evaluation.
        normal_move_in_progress = getattr(ctx.move_state, "source_square", INVALID_SQUARE) != INVALID_SQUARE
        skip_no_legal_moves_check = allow_bumps_without_legal_move_check or normal_move_in_progress
        if not skip_no_legal_moves_check and piece_color != ctx.chess_board.turn:
            has_legal_capture_to_square = any(
                (move.to_square == field and ctx.chess_board.is_capture(move))
                for move in ctx.chess_board.legal_moves
            )
            if has_legal_capture_to_square:
                skip_no_legal_moves_check = True

        if not skip_no_legal_moves_check:
            # Check if this piece has any legal moves from this square (turn-dependent).
            has_legal_moves = any(move.from_square == field for move in ctx.chess_board.legal_moves)
            if not has_legal_moves:
                log.warning(
                    f"[GameManager.receive_field] Piece at {chess.square_name(field)} has no legal moves - "
                    "entering correction mode"
                )
                ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
                ctx.enter_correction_mode_fn()
                current_state = ctx.board_module.getChessState()
                expected_state = ctx.chess_board_to_state_fn(ctx.chess_board)
                if current_state is not None and expected_state is not None:
                    ctx.provide_correction_guidance_fn(current_state, expected_state)
                return

    # Forward to player manager (after board state validation to avoid incorrect move formation)
    ctx.on_piece_event_fn("lift" if is_lift else "place", field, ctx.chess_board)

    # Note: King lift resign is now handled at the start of process_field_event
    # to ensure it works even during correction mode or Hand+Brain mode.


__all__ = ["FieldEventContext", "process_field_event"]


