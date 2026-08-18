"""Tests for noticing that the board has stopped responding to its buttons.

Every key press is routed to something: an overlay, the menu widget, or the
game. When routing finds nothing to hand a key to, the board is in a state it
should not be in -- a menu with no widget, or a game whose managers are gone --
and from the user's side it is simply dead: presses do nothing and there is no
way back except pulling the power.

Counting those presses and forcing a recovery after enough of them is what makes
the board recoverable. The counter was two module globals in the middle of the
key handler, so the threshold and the reset-on-success rule could only be read
there and were never tested.
"""

import pytest

from universalchess.app.key_recovery import KeyRecovery


@pytest.fixture
def recovery():
    """A recovery counter with the board's own threshold."""
    return KeyRecovery()


def test_a_board_that_is_answering_keys_never_recovers(recovery):
    """A handled key resets the count, so ordinary use never triggers recovery.

    Why: recovery tears down the live game and returns to the main menu, which
    would destroy a game in progress. Unhandled keys have to be consecutive for
    the board to be considered stuck. How a regression manifests: a game is
    abandoned mid-play because a handful of stray presses accumulated over an
    entire session.
    """
    for _ in range(KeyRecovery.THRESHOLD * 3):
        assert recovery.record_unhandled() is False
        recovery.record_handled()

    assert recovery.unhandled_count == 0


def test_recovery_is_due_after_the_threshold_of_consecutive_unhandled_keys(recovery):
    """Enough consecutive unhandled keys means the board is stuck.

    Why: this is the only way out of a state where nothing responds, so the
    threshold must actually be reached -- and must not be reached early, since
    recovering discards the running game. How a regression manifests: either
    the board stays dead however many times the user presses, or a couple of
    stray keys throw away a game.
    """
    for _ in range(KeyRecovery.THRESHOLD - 1):
        assert recovery.record_unhandled() is False

    assert recovery.record_unhandled() is True


def test_the_count_restarts_after_a_recovery(recovery):
    """Reaching the threshold consumes the count.

    Why: without the reset, every further unhandled key would demand another
    recovery, so a board that is genuinely stuck would tear itself down
    repeatedly instead of once. How a regression manifests: repeated recovery
    beeps and menu resets while the user presses buttons.
    """
    for _ in range(KeyRecovery.THRESHOLD):
        recovery.record_unhandled()

    assert recovery.unhandled_count == 0
    assert recovery.record_unhandled() is False


def test_the_threshold_is_more_than_one(recovery):
    """A single unhandled key is never enough.

    Why: unhandled keys happen benignly -- a press that arrives during a screen
    transition has nowhere to go -- and recovering on the first one would
    destroy games for no reason. How a regression manifests: the threshold is
    lowered to 1 and any transient press ends the game.
    """
    assert KeyRecovery.THRESHOLD > 1
