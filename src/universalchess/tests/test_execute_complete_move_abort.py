"""Tests that execute_complete_move cleans up and recovers on rejected attempts.

Background / why these tests exist
----------------------------------
execute_complete_move has three exits: the normal success path (which calls
move_state.reset()), a game-over guard, and a push-failure guard. Originally
only the success path reset move_state, so the two guard exits could leave stale
in-progress lift tracking (source_square, legal_destination_squares, capture
events). The next physical event reads that stale state and can mis-attribute a
place to the wrong source.

push_move raises only on an illegal move, and both live callers prove legality
on the same board immediately before pushing, so these guards are defensive and
not reachable in normal operation. These tests pin the defensive contract so the
branch cannot silently become a real bug: every rejected attempt must (a) reset
move_state and (b) recover via correction mode + guidance, mirroring the
illegal-move handling in on_player_move.
"""

import chess

from universalchess.managers.game.player_moves import (
    PlayerMoveContext,
    execute_complete_move,
)
from universalchess.utils.led import LedCallbacks

# Checkmate position (1.f3 e5 2.g4 Qh4#): white to move, board.outcome() is not
# None, so it triggers the game-over guard at the top of execute_complete_move.
CHECKMATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"


class _FakeBoard:
    """Records beep calls and reports a (fake) physical board state."""

    SOUND_GENERAL = "general"
    SOUND_WRONG_MOVE = "wrong"

    def __init__(self):
        self.beeps = []

    def beep(self, sound, event_type=None):
        self.beeps.append((sound, event_type))

    def getChessState(self):
        # Non-None so the guidance branch is exercised.
        return [0] * 64


class _RecordingMoveState:
    """Records reset() calls so cleanup-on-every-exit can be asserted."""

    def __init__(self):
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1


class _RaisingGameState:
    """game_state whose push_move always raises, to force the failure guard."""

    def push_move(self, move):
        raise ValueError(f"forced push failure for {move.uci()}")


def _noop_led():
    return LedCallbacks(
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


def _context(chess_board, game_state, fake_board, move_state, recorder):
    return PlayerMoveContext(
        chess_board=chess_board,
        game_state=game_state,
        move_state=move_state,
        board_module=fake_board,
        led=_noop_led(),
        get_game_db_id_fn=lambda: -1,
        switch_turn_with_event_fn=lambda: recorder["turn_switched"].append(True),
        enqueue_post_move_tasks_fn=lambda **k: recorder["post_move"].append(k),
        enter_correction_mode_fn=lambda: recorder["correction_entered"].append(True),
        chess_board_to_state_fn=lambda b: [0] * 64,
        provide_correction_guidance_fn=lambda a, b: recorder["guidance"].append((a, b)),
        set_is_showing_promotion_fn=lambda v: None,
        on_promotion_needed_fn=None,
    )


def _fresh_recorder():
    return {
        "turn_switched": [],
        "post_move": [],
        "correction_entered": [],
        "guidance": [],
    }


def test_push_failure_resets_move_state_and_enters_correction():
    """A push failure must clean up and recover, not just beep-and-return.

    Why: the failure leaves a piece on a square the logical board never accepted
    and leaves in-progress lift tracking populated. Without reset() the next
    physical event mis-reads source_square; without correction mode the player
    gets no guidance to restore the board.

    How the regression manifests: if the failure branch reverts to beep+return,
    reset_count stays 0 and correction_entered stays empty, so the next move
    starts from corrupted move_state with no recovery prompt.
    """
    game_state = _RaisingGameState()
    fake_board = _FakeBoard()
    move_state = _RecordingMoveState()
    recorder = _fresh_recorder()
    # Legal starting position so the game-over guard is passed and the push guard
    # is the exit under test.
    ctx = _context(chess.Board(), game_state, fake_board, move_state, recorder)

    execute_complete_move(ctx, chess.Move.from_uci("e2e4"))

    assert move_state.reset_count == 1
    assert recorder["correction_entered"] == [True]
    assert recorder["guidance"] == [([0] * 64, [0] * 64)]
    assert ("wrong", "error") in fake_board.beeps
    # The attempt was rejected: no turn switch, no post-move tasks enqueued.
    assert recorder["turn_switched"] == []
    assert recorder["post_move"] == []


def test_game_over_guard_resets_move_state_and_enters_correction():
    """A move arriving after game end must clean up and recover too.

    Why: the same cleanup gap exists in the game-over guard. A disturbed physical
    board after the game ended needs reconciliation, and stale move_state must not
    bleed into later events.

    How the regression manifests: if the guard reverts to beep+return, reset_count
    stays 0 and correction_entered stays empty.
    """
    game_state = _RaisingGameState()  # never reached; guard returns first
    fake_board = _FakeBoard()
    move_state = _RecordingMoveState()
    recorder = _fresh_recorder()
    ctx = _context(chess.Board(CHECKMATE_FEN), game_state, fake_board, move_state, recorder)

    execute_complete_move(ctx, chess.Move.from_uci("e1e2"))

    assert move_state.reset_count == 1
    assert recorder["correction_entered"] == [True]
    assert recorder["guidance"] == [([0] * 64, [0] * 64)]
    assert ("wrong", "error") in fake_board.beeps
    assert recorder["turn_switched"] == []
    assert recorder["post_move"] == []
