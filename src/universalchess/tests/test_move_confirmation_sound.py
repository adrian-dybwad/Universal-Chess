"""Tests that the piece place/move-confirmation beep is gated by piece_event.

Background / why these tests exist
----------------------------------
Completing a move on the physical board produces a confirmation beep that fires
together with the place-confirmation LED flash (led.single_fast on the target
square) in execute_complete_move. That beep must be categorised as a
"piece_event" so the Sound menu's "Piece Events" switch controls it. Previously
it was tagged "game_event", so toggling "Piece Events" had no effect on it (and
the dedicated piece_event switch gated nothing at all).

These tests pin that the confirmation beep is emitted with event_type
"piece_event" and coincides with the place-confirmation flash.
"""

import chess

from universalchess.managers.game.player_moves import (
    PlayerMoveContext,
    execute_complete_move,
)
from universalchess.state.chess_game import reset_chess_game
from universalchess.utils.led import LedCallbacks


class _FakeBoard:
    """Records beep calls so the event_type categorisation can be asserted."""

    SOUND_GENERAL = "general"
    SOUND_WRONG_MOVE = "wrong"

    def __init__(self):
        self.beeps = []

    def beep(self, sound, event_type=None):
        self.beeps.append((sound, event_type))


class _MoveState:
    def reset(self):
        pass


def _noop_led(flashes):
    """LedCallbacks with no-op LEDs except single_fast, which records squares."""
    return LedCallbacks(
        from_to=lambda *a, **k: None,
        array=lambda *a, **k: None,
        single=lambda *a, **k: None,
        off=lambda *a, **k: None,
        from_to_hint=lambda *a, **k: None,
        array_hint=lambda *a, **k: None,
        array_fast=lambda *a, **k: None,
        from_to_fast=lambda *a, **k: None,
        single_fast=lambda sq, repeat=0: flashes.append(sq),
    )


def _context(fake_board, flashes):
    game_state = reset_chess_game()
    return PlayerMoveContext(
        chess_board=game_state.board,
        game_state=game_state,
        move_state=_MoveState(),
        board_module=fake_board,
        led=_noop_led(flashes),
        get_game_db_id_fn=lambda: -1,
        switch_turn_with_event_fn=lambda: None,
        enqueue_post_move_tasks_fn=lambda **k: None,
        enter_correction_mode_fn=lambda: None,
        chess_board_to_state_fn=lambda b: None,
        provide_correction_guidance_fn=lambda a, b: None,
        player_supports_late_castling_fn=lambda: False,
        detect_late_castling_fn=lambda m: None,
        execute_late_castling_from_move_fn=lambda m: None,
        set_is_showing_promotion_fn=lambda v: None,
        on_promotion_needed_fn=None,
    )


def test_move_confirmation_beep_is_tagged_piece_event():
    """The single success beep is SOUND_GENERAL tagged piece_event.

    Why: this beep is the audible piece-place confirmation, so it must be gated
    by the "Piece Events" sound switch (event_type="piece_event"), not lumped in
    with game_event (check/checkmate) sounds.

    How the regression manifests: if it reverts to "game_event" (or any other
    type), this exact-list assertion fails, because the confirmation beep would
    no longer be controlled by the piece_event switch.
    """
    fake_board = _FakeBoard()
    flashes = []
    ctx = _context(fake_board, flashes)

    execute_complete_move(ctx, chess.Move.from_uci("e2e4"))

    assert fake_board.beeps == [("general", "piece_event")]


def test_confirmation_beep_coincides_with_place_flash():
    """The confirmation beep fires with the place-confirmation flash on target.

    Why: the user identifies this sound as happening "at the same time as the
    place confirmation flash". Asserting both the flash square and the beep ties
    the gated beep to that exact confirmation, so a future change that moves the
    beep elsewhere (and leaves a different, ungated beep on the flash) is caught.

    How the regression manifests: the flash list is empty / wrong square, or the
    beep is not the piece_event-tagged confirmation.
    """
    fake_board = _FakeBoard()
    flashes = []
    ctx = _context(fake_board, flashes)

    execute_complete_move(ctx, chess.Move.from_uci("e2e4"))

    assert flashes == [chess.parse_square("e4")]
    assert ("general", "piece_event") in fake_board.beeps
