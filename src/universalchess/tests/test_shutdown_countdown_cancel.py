"""PLAY release must cancel the shutdown countdown even after a slow splash paint.

Why these tests exist
---------------------
Long-press PLAY starts a 3s shutdown countdown. Releasing PLAY is the cancel.
On dgt-64 the countdown splash's first e-paper refresh blocks in
``future.result`` for up to 2s, which is longer than the user holds the button.
The key-up is queued during that wait, then a drain-all loop throws it away
before the countdown looks for PLAY. The countdown then runs to completion
(or only cancels on a second press). The board log from 2026-08-16 12:53
showed PLAY ↑ at 12:53:49.689 during the splash wait, then "Shutdown in 2"
still advancing until a later press.

How a regression manifests
--------------------------
``shutdown_countdown`` returns True (proceed with shutdown) when PLAY was
queued during the splash wait, or it still returns True when PLAY arrives
in the countdown loop. Either path powers the board off after a cancelled
hold.
"""

from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from universalchess.board import board
from universalchess.board.sync_centaur import Key


@pytest.fixture
def countdown_env(monkeypatch):
    """Board globals for shutdown_countdown with no hardware or real sleeps.

    ``add_widget`` returns a Future whose ``result()`` is the splash-render
    wait. Tests enqueue keys from that wait (or from get_next_key) to model
    a release that arrives while the panel is still painting.
    """
    keys = []

    class _Controller:
        def get_next_key(self, timeout=0.0):
            if keys:
                return keys.pop(0)
            return None

    controller = _Controller()
    display_manager = MagicMock()
    splash_future = Future()
    display_manager.add_widget.return_value = splash_future

    monkeypatch.setattr(board, "controller", controller)
    monkeypatch.setattr(board, "display_manager", display_manager)
    monkeypatch.setattr(board, "beep", lambda *a, **k: None)
    monkeypatch.setattr(board.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        "universalchess.epaper.SplashScreen",
        MagicMock(return_value=MagicMock()),
    )

    return {
        "keys": keys,
        "splash_future": splash_future,
        "display_manager": display_manager,
    }


def test_play_release_during_splash_wait_cancels_countdown(countdown_env):
    """PLAY queued while the splash render blocks must still cancel.

    Why: this is the dgt-64 failure. The drain after ``future.result`` discarded
    the release that arrived during the wait, so the countdown ignored it.

    How a regression manifests: shutdown_countdown returns True even though
    Key.PLAY was queued from result(), and the board shuts down.
    """
    countdown_env["splash_future"].set_result("ok")
    original_result = countdown_env["splash_future"].result

    def result_with_release(timeout=None):
        countdown_env["keys"].append(Key.PLAY)
        return original_result(timeout=timeout)

    countdown_env["splash_future"].result = result_with_release

    assert board.shutdown_countdown(countdown_seconds=1) is False
    countdown_env["display_manager"].remove_widget.assert_called_once()


def test_play_release_during_countdown_loop_cancels(countdown_env):
    """A PLAY key-up after the splash is on screen must cancel.

    Why: the countdown loop is the path for a hold released after the first
    frame. If that loop stops looking for Key.PLAY (or only looks once before
    sleeping through the rest of the second), a late release is ignored.

    How a regression manifests: returns True despite Key.PLAY sitting in the
    queue after the splash wait completes with an empty queue.
    """
    countdown_env["splash_future"].set_result("ok")
    countdown_env["keys"].append(Key.PLAY)

    assert board.shutdown_countdown(countdown_seconds=1) is False
    countdown_env["display_manager"].remove_widget.assert_called_once()


def test_countdown_completes_when_play_stays_held(countdown_env):
    """No PLAY key-up means the hold was kept; shutdown must proceed.

    Why: the cancel path must not fire just because the splash wait finished
    or because some other key (BACK) was queued. A regression that treats any
    drained key as cancel would abort a deliberate shutdown.

    How a regression manifests: returns False with an empty queue, or returns
    False because a BACK in the queue was mistaken for PLAY.
    """
    countdown_env["splash_future"].set_result("ok")
    countdown_env["keys"].append(Key.BACK)

    assert board.shutdown_countdown(countdown_seconds=1) is True
    countdown_env["display_manager"].remove_widget.assert_not_called()
