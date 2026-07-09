"""Check/queen alert notification on takeback and direct position changes.

Background / why these tests exist
----------------------------------
The CHECK / YOUR QUEEN alert (AlertWidget) is driven only by ChessGameState's
check/queen observers, which fire from _notify_check_and_threats(). Every
position-mutating method that ends a "turn" re-derives the alert (push_move,
configure_start, reset, reset_to_standard) so the alert always matches the shown
position.

pop_move (takeback) and set_position notified the position change but NOT the
alert, so the alert kept whatever it last showed: taking back a checking move
left CHECK on the display over a position with no check, and loading a position
never updated the alert. These tests pin that both paths re-derive the alert.
"""

import chess

from universalchess.state.chess_game import ChessGameState

# White rook a1, kings on e1/e8. After Ra1-a8 the black king on e8 is in check
# along rank 8 (b8..d8 empty); the position before the move is quiet.
QUIET_WHITE_TO_MOVE_FEN = "4k3/8/8/8/8/8/8/R3K3 w - - 0 1"
CHECKING_ROOK_MOVE = "a1a8"

# Black king on e8 already in check from the white rook on a8; Black escapes with
# Ke8-e7. Popping that move returns to this still-in-check position.
BLACK_IN_CHECK_FEN = "R3k3/8/8/8/8/8/8/4K3 b - - 0 1"
BLACK_ESCAPE_MOVE = "e8e7"

QUIET_POSITION_FEN = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"


class _AlertRecorder:
    """Records every check / queen-threat / alert-clear the state emits."""

    def __init__(self, state: ChessGameState):
        self.checks = []
        self.queens = []
        self.clears = []
        state.on_check(lambda is_black, atk, king: self.checks.append((is_black, atk, king)))
        state.on_queen_threat(lambda is_black, atk, q: self.queens.append((is_black, atk, q)))
        state.on_alert_clear(lambda: self.clears.append(True))

    def reset(self) -> None:
        self.checks.clear()
        self.queens.clear()
        self.clears.clear()


def test_takeback_of_checking_move_clears_the_alert():
    """Popping a move that gave check must emit alert_clear for the quiet position.

    Why: this is the reported bug -- take back the checking move and the CHECK
    alert must disappear because the reverted position has no check.

    How the regression manifests: pop_move only fired position_change, never
    alert_clear, so the AlertWidget kept showing CHECK over a position with no
    check (clears stays empty here).
    """
    state = ChessGameState()
    state.configure_start(QUIET_WHITE_TO_MOVE_FEN)
    recorder = _AlertRecorder(state)

    state.push_move(chess.Move.from_uci(CHECKING_ROOK_MOVE))
    # Sanity: the move actually produced a check alert for Black.
    assert recorder.checks == [(True, chess.A8, chess.E8)]

    recorder.reset()
    popped = state.pop_move()

    assert popped == chess.Move.from_uci(CHECKING_ROOK_MOVE)
    assert recorder.clears == [True]
    assert recorder.checks == []


def test_takeback_re_raises_check_when_prior_position_is_also_check():
    """Popping into a still-in-check position must re-emit the check alert.

    Why: the reverted position must be described truthfully. If the position
    before the last move was itself a check, the alert must show CHECK again --
    clearing it would be just as wrong as leaving a stale one.

    How the regression manifests: pop_move fired no alert at all, so the alert
    reflected the escaped (quiet) position instead of the restored check
    (checks stays empty here).
    """
    state = ChessGameState()
    state.configure_start(BLACK_IN_CHECK_FEN)
    recorder = _AlertRecorder(state)

    # Black escapes the check; the resulting position is quiet.
    state.push_move(chess.Move.from_uci(BLACK_ESCAPE_MOVE))
    assert recorder.clears == [True]

    recorder.reset()
    state.pop_move()

    assert recorder.checks == [(True, chess.A8, chess.E8)]
    assert recorder.clears == []


def test_set_position_to_check_emits_check_alert():
    """set_position onto a checking position must emit the check alert.

    Why: loading/adopting a position must update the alert to match it, exactly
    as a move would.

    How the regression manifests: set_position skipped _notify_check_and_threats,
    so a loaded in-check position showed no CHECK alert (checks stays empty).
    """
    state = ChessGameState()
    state.configure_start(chess.STARTING_FEN)
    recorder = _AlertRecorder(state)

    state.set_position(BLACK_IN_CHECK_FEN)

    assert recorder.checks == [(True, chess.A8, chess.E8)]
    assert recorder.clears == []


def test_set_position_to_quiet_clears_a_stale_alert():
    """set_position onto a quiet position must clear any prior alert.

    Why: adopting a quiet position after a check must not leave the CHECK alert
    on screen.

    How the regression manifests: set_position fired no alert event, so a
    previously shown CHECK stayed visible (clears stays empty here).
    """
    state = ChessGameState()
    state.configure_start(BLACK_IN_CHECK_FEN)
    recorder = _AlertRecorder(state)

    state.set_position(QUIET_POSITION_FEN)

    assert recorder.clears == [True]
    assert recorder.checks == []
