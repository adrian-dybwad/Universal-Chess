"""Tests for the shared board power helpers (services.power).

The on-board Power menu actions and the web Power control both call
``perform_shutdown``/``perform_reboot``, so a shutdown or reboot must behave
identically regardless of where it is triggered. These tests pin the shutdown
reason/flag and the reboot LED sweep so the two surfaces cannot diverge. They
were moved here when the bespoke ``system_menu`` builder was deleted in favor of
the data-driven System/Power menus that invoke these helpers as actions.
"""

import universalchess.services.power as power
from universalchess.services.power import perform_shutdown, perform_reboot


class _RecordingBoard:
    """Board stub recording LED calls for the reboot sweep."""

    def __init__(self):
        self.led_calls = []

    def led(self, index, **kwargs):
        self.led_calls.append(index)


def test_perform_shutdown_calls_shutdown_fn_with_shutdown_args():
    """perform_shutdown must invoke shutdown_fn('Shutdown', False).

    Why this test exists: the e-paper Power menu and the web Power control share
    this one function, so the shutdown reason/label (which the board logs and
    splashes via _shutdown) must stay fixed. A drift here would make the two
    surfaces shut down differently.

    How a regression manifests: the recorded args change (wrong label, or reboot
    flag True), so a "Shutdown" would reboot or log the wrong reason.
    """
    calls = []
    perform_shutdown(lambda message, reboot: calls.append((message, reboot)))

    assert calls == [("Shutdown", False)]


def test_perform_reboot_runs_led_sweep_then_reboots(monkeypatch):
    """perform_reboot must sweep all 8 LEDs, then call shutdown_fn('Rebooting', True).

    Why this test exists: the LED sweep is part of the reboot's user-visible
    behavior on the board; the web reboot must not drop it. Pinning the
    sweep-then-shutdown order guarantees the web reboot matches the on-board one.

    How a regression manifests: fewer/more than 8 LEDs sweep, the sweep runs
    after the shutdown call, or the args are wrong (so the web reboot diverges
    from the board reboot).
    """
    # Avoid the real 0.2s-per-LED delay; the timing is not under test.
    monkeypatch.setattr(power.time, "sleep", lambda *_: None)
    board = _RecordingBoard()
    calls = []

    perform_reboot(board, lambda message, reboot: calls.append((message, reboot)))

    assert board.led_calls == [0, 1, 2, 3, 4, 5, 6, 7]
    assert calls == [("Rebooting", True)]


def test_perform_reboot_reboots_even_if_led_sweep_fails(monkeypatch):
    """A failing LED sweep must not block the reboot.

    Why this test exists: on a detached/!ready board the LED call can raise; the
    reboot must still proceed (matching the menu's best-effort sweep).

    How a regression manifests: the exception propagates and shutdown_fn is never
    called, so the board never reboots.
    """
    monkeypatch.setattr(power.time, "sleep", lambda *_: None)

    class _BrokenBoard:
        def led(self, index, **kwargs):
            raise RuntimeError("no board")

    calls = []
    perform_reboot(_BrokenBoard(), lambda message, reboot: calls.append((message, reboot)))

    assert calls == [("Rebooting", True)]
