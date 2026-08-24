"""UC must stop polling the board before the original Centaur takes the UART.

Measured on an Orange Pi Zero 2W: after the ``Exec format error`` was fixed and
centaur could finally execute, every launch still died the same way. centaur's
startup handshake failed four times::

    File "/home/pa/centaur/dgt_serial.py", line 927, in doPing
    Exception: Initial PING: Command failed

whereupon centaur powered itself off and exited 1, which the app reports as
"Original Centaur exited with code 1" and the user sees as an immediate bounce
back to the menu.

The cause was not centaur, the display shim, or the serial tap. It was UC:
:class:`SystemPollingService` polls ``DGT_SEND_BATTERY_INFO`` through
``board.controller`` every five seconds, and the handoff never stopped it. UC and
centaur were two masters on one board link, each consuming the replies the other
was waiting for -- UC logging ``Timeout for DGT_SEND_BATTERY_INFO`` at 08:50:55
and 08:51:07 while centaur's PING starved in between. Stopping the UC service by
hand and running centaur against the same UART produced no PING failure at all,
which is what isolated the poller as the second master.

The stop must happen *before* centaur is launched: stopping it afterwards leaves
the whole overlapping window unprotected, which is the bug.
"""

import pytest

from universalchess.app import board_app


LAUNCH_TRANSLATE = "translate"
LAUNCH_DIRECT = "direct"


class _RecordingService:
    """Stands in for the polling singleton, recording that it was stopped.

    Only ``stop`` is exercised: the handoff's contract with the poller is that it
    is no longer touching the board, and ``stop`` is idempotent and safe to call
    when polling was never started.
    """

    def __init__(self, calls):
        self._calls = calls

    def stop(self):
        self._calls.append("polling-stopped")


@pytest.fixture
def handoff(monkeypatch):
    """Drive ``_launch_original_centaur`` with the board and both modes faked.

    Returns the ordered call log. Everything that would touch hardware, settings
    or the filesystem is replaced, so the only thing under test is the order of
    "stop polling" against "launch centaur".
    """
    from universalchess.board import settings as settings_module
    from universalchess.services import centaur_import, power

    calls = []

    monkeypatch.setattr(centaur_import, "ensure_factory_marker", lambda: None)
    monkeypatch.setattr(settings_module.Settings, "read", staticmethod(lambda: {}))

    def _translate():
        calls.append(LAUNCH_TRANSLATE)
        return True

    def _direct():
        calls.append(LAUNCH_DIRECT)
        return True

    monkeypatch.setattr(board_app, "_run_centaur_translate", _translate)
    monkeypatch.setattr(board_app, "_run_centaur", _direct)

    import universalchess.services as services

    monkeypatch.setattr(
        services, "get_system_service", lambda: _RecordingService(calls), raising=False
    )

    def _set_mode(direct: bool):
        monkeypatch.setattr(
            power, "centaur_direct_mode_enabled", lambda _read: direct
        )

    return calls, _set_mode


@pytest.mark.parametrize(
    "direct_mode, expected_launch",
    [(False, LAUNCH_TRANSLATE), (True, LAUNCH_DIRECT)],
)
def test_board_polling_stops_before_centaur_launches(handoff, direct_mode, expected_launch):
    """Both handoff modes must quiesce UC's board polling first.

    Why both: direct mode and translate mode each give centaur the same UART, so
    a poller left running starves centaur's PING identically. Only the display
    handling differs between them, which is irrelevant to the board link.

    How the regression manifests: ``calls`` contains only the launch entry,
    because nothing stopped the poller -- exactly the state that produced the
    repeated ``Initial PING: Command failed`` on hardware. If a future change
    stops the poller but does so after the launch, the order assertion catches
    it, since the damaging window is precisely while centaur is running.
    """
    calls, set_mode = handoff
    set_mode(direct_mode)

    board_app._launch_original_centaur()

    assert calls == ["polling-stopped", expected_launch]


def test_polling_stops_even_when_the_factory_marker_cannot_be_written(handoff, monkeypatch):
    """A failed factory marker must not skip the quiesce.

    Why this test exists: the marker write is best-effort and its ``OSError`` is
    swallowed so the handoff continues. If the quiesce were ordered after it
    inside the same try, or short-circuited by it, centaur would still launch --
    now against a live poller, reintroducing the two-master failure on exactly
    the path where the board is already in an unusual state.

    Failure: ``calls`` lacks "polling-stopped" while still containing the launch.
    """
    from universalchess.services import centaur_import

    calls, set_mode = handoff
    set_mode(False)

    def _boom():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(centaur_import, "ensure_factory_marker", _boom)

    board_app._launch_original_centaur()

    assert calls == ["polling-stopped", LAUNCH_TRANSLATE]
