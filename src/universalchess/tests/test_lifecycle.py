"""Tests for whether the board keeps running, and how it stops.

Two module globals answered one question. ``running`` was a flag and ``kill``
was an int, the loop condition was ``while running and not kill``, and every
place that wanted to stop set both -- except the relay's connect thread, which
set only ``kill``. Nothing enforced that pairing, so a stop that set one and not
the other would have kept the loop turning.

Stopping also has to say what kind of stop it is: a long PLAY press powers the
device off, while every other stop exits the process for the service manager to
restart. That distinction was a third global, set on the events thread and read
by the main loop after it fell out of the loop.
"""

import pytest

from universalchess.app.lifecycle import Lifecycle


@pytest.fixture
def lifecycle():
    """A board that has just started."""
    return Lifecycle()


def test_a_board_that_has_started_keeps_running(lifecycle):
    """The loop turns until something stops it.

    Why: this is the main loop's condition, so a board that starts stopped
    never shows a menu. How a regression manifests: the board boots, the splash
    clears and the process exits.
    """
    assert lifecycle.keep_running is True


def test_stopping_ends_the_loop(lifecycle):
    """One call stops the board, whatever the reason.

    Why: this replaces two flags that every stop had to set together, and one
    caller set only one of them. How a regression manifests: a stop request is
    ignored and the board carries on, most visibly when a relay connection
    fails and the process should end.
    """
    lifecycle.stop("relay could not connect")

    assert lifecycle.keep_running is False
    assert lifecycle.stop_reason == "relay could not connect"


def test_a_power_off_is_distinguishable_from_an_ordinary_stop(lifecycle):
    """A long PLAY press powers the device down; other stops do not.

    Why: the main loop chooses between shutting the device down and exiting for
    the service manager to restart, and it makes that choice after the loop has
    already ended. If the two were indistinguishable, holding PLAY would restart
    the board instead of turning it off -- or every crash would power the device
    down. How a regression manifests: the board reboots when the user asked it
    to shut down.
    """
    assert lifecycle.shutdown_requested is False

    lifecycle.request_shutdown("LONG_PLAY")

    assert lifecycle.shutdown_requested is True
    assert lifecycle.keep_running is False
    assert lifecycle.stop_reason == "LONG_PLAY"


def test_cleanup_runs_once(lifecycle):
    """The second attempt to clean up is refused.

    Why: cleanup is reached from the signal handler and from the main loop's
    finally block, and it ends in ``sys.exit``; running it twice tears down
    managers that are already gone and can raise during shutdown. How a
    regression manifests: shutdown logs a cascade of errors, or hangs.
    """
    assert lifecycle.begin_cleanup() is True
    assert lifecycle.begin_cleanup() is False


def test_stopping_twice_keeps_the_first_reason(lifecycle):
    """The reason recorded is the one that actually stopped the board.

    Why: teardown itself stops the board again on its way out, and that second,
    generic reason would otherwise overwrite the specific one the logs need to
    explain why the process ended. How a regression manifests: every shutdown
    is logged with the same uninformative reason.
    """
    lifecycle.stop("BACK held on the menu")
    lifecycle.stop("cleanup")

    assert lifecycle.stop_reason == "BACK held on the menu"
