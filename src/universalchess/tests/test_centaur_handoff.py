"""Tests for the original-DGT-Centaur handoff (display release ordering).

Background / why these tests exist
----------------------------------
Launching the original Centaur software (the "Original Centaur" menu/web action)
hands the board hardware to a foreign process. The bug these tests guard:
``board.cleanup()`` released only the *serial* port, so centaur received board
events (LEDs lit, moves registered) but Universal-Chess still owned the e-paper:
the refresh scheduler thread kept running and the SPI fd + gpiozero RST/DC/BUSY
lines stayed claimed. centaur then drove the *same* ``/dev/spidev1.0`` and the
*same* BCM pins, so its refreshes collided -- the panel showed centaur's first
screen but never updated.

The fix has two parts, each pinned here:
1. ``Manager.release_hardware()`` fully releases the panel (scheduler stopped,
   then SPI + GPIO lines closed via ``module_exit(cleanup=True)``), unlike
   ``shutdown()``/``sleep()`` which use ``cleanup=False`` and leave the lines
   claimed.
2. ``perform_centaur_handoff()`` releases the display BEFORE launching centaur,
   and does not release (or launch) at all when the binary is absent.
"""

from unittest.mock import MagicMock, patch

import pytest

from universalchess.epaper.framework.manager import Manager
import signal

from universalchess.services.power import (
    RESTART_UNIVERSAL_CHESS_CMD,
    centaur_direct_mode_enabled,
    classify_centaur_exit,
    perform_centaur_handoff,
    perform_centaur_translate_handoff,
    restart_exit_code,
    return_to_universal_chess,
)


def _make_manager_with_mock_epd():
    """Build a Manager around a mock EPD (no real hardware/threads).

    width/height are concrete ints so the real FrameBuffer can size itself; the
    scheduler is replaced with a mock so release_hardware() can be asserted
    without starting a daemon thread.
    """
    epd = MagicMock()
    epd.width = 128
    epd.height = 296
    manager = Manager(epd=epd, batch_updates=False)
    manager._scheduler = MagicMock()
    return manager, epd


# ---------------------------------------------------------------------------
# Manager.release_hardware()
# ---------------------------------------------------------------------------

def test_release_hardware_closes_spi_and_gpio_with_cleanup_true():
    """release_hardware() must call module_exit(cleanup=True).

    Why: only cleanup=True closes the gpiozero RST/DC/BUSY lines (and the SPI fd).
    Every other path uses cleanup=False, which leaves the lines claimed and
    collides with centaur. Failure manifests as module_exit called with
    cleanup=False (or not at all) -> centaur cannot claim the GPIO/SPI.
    """
    manager, epd = _make_manager_with_mock_epd()

    with patch(
        "universalchess.epaper.framework.manager.epdconfig.module_exit"
    ) as mock_exit:
        manager.release_hardware()

    mock_exit.assert_called_once_with(cleanup=True)
    epd.idle_sleep.assert_called_once()


def test_release_hardware_stops_scheduler_before_closing_hardware():
    """The scheduler must be stopped BEFORE SPI/GPIO are closed.

    Why: a queued refresh running after module_exit() would touch a closed SPI
    device / released GPIO lines (crash or corrupt the handoff). Ordering is the
    invariant. Failure manifests as module_exit appearing before scheduler.stop
    in the recorded call order.
    """
    manager, epd = _make_manager_with_mock_epd()

    order = []
    manager._scheduler.stop.side_effect = lambda: order.append("scheduler_stop")
    epd.idle_sleep.side_effect = lambda: order.append("idle_sleep")

    with patch(
        "universalchess.epaper.framework.manager.epdconfig.module_exit"
    ) as mock_exit:
        mock_exit.side_effect = lambda **kw: order.append(("module_exit", kw))
        manager.release_hardware()

    manager._scheduler.stop.assert_called_once()
    assert order == ["scheduler_stop", "idle_sleep", ("module_exit", {"cleanup": True})]


def test_release_hardware_continues_when_panel_settle_fails():
    """A failing panel settle must NOT abort the GPIO/SPI release.

    Why: idle_sleep() sends SPI commands; if the panel is unresponsive it may
    raise, but the whole point of the handoff is to free the hardware -- so the
    release (module_exit) must still run. Failure manifests as module_exit not
    being called when idle_sleep raises (board left holding the panel).
    """
    manager, epd = _make_manager_with_mock_epd()
    epd.idle_sleep.side_effect = RuntimeError("panel unresponsive")

    with patch(
        "universalchess.epaper.framework.manager.epdconfig.module_exit"
    ) as mock_exit:
        manager.release_hardware()

    mock_exit.assert_called_once_with(cleanup=True)


# ---------------------------------------------------------------------------
# perform_centaur_handoff()
# ---------------------------------------------------------------------------

def test_handoff_releases_display_before_launching_centaur():
    """The display must be released BEFORE centaur is launched.

    Why: this is the core regression. If centaur starts while UC still owns the
    panel, the two processes contend for SPI/GPIO and the display freezes after
    the first frame. Asserts the exact order release -> launch -> on-exit. The
    on-exit hook must run only AFTER launch returns (centaur has exited), so it
    restores Universal Chess at the right moment. Failure manifests as 'launch'
    preceding 'release', or the exit hook running before 'launch', in the order.
    """
    order = []
    display_manager = MagicMock()
    display_manager.release_hardware.side_effect = lambda: order.append("release")
    launch_fn = MagicMock(side_effect=lambda path: order.append("launch"))
    on_centaur_exit_fn = MagicMock(side_effect=lambda: order.append("on_exit"))

    result = perform_centaur_handoff(
        display_manager=display_manager,
        software_path="/home/pi/centaur/centaur",
        launch_fn=launch_fn,
        on_centaur_exit_fn=on_centaur_exit_fn,
        path_exists_fn=lambda p: True,
    )

    assert result is True
    assert order == ["release", "launch", "on_exit"]
    display_manager.release_hardware.assert_called_once()
    launch_fn.assert_called_once_with("/home/pi/centaur/centaur")


def test_handoff_does_not_release_or_launch_when_binary_absent():
    """When the centaur binary is missing, do nothing destructive.

    Why: a missing binary means UC must keep running and keep owning the panel.
    Releasing the display (or launching) would blank/relinquish the board for no
    reason. Asserts neither release_hardware nor launch run, and the result is
    False so the caller stays in Universal Chess.
    """
    display_manager = MagicMock()
    launch_fn = MagicMock()
    on_centaur_exit_fn = MagicMock()

    result = perform_centaur_handoff(
        display_manager=display_manager,
        software_path="/home/pi/centaur/centaur",
        launch_fn=launch_fn,
        on_centaur_exit_fn=on_centaur_exit_fn,
        path_exists_fn=lambda p: False,
    )

    assert result is False
    display_manager.release_hardware.assert_not_called()
    launch_fn.assert_not_called()
    on_centaur_exit_fn.assert_not_called()


# ---------------------------------------------------------------------------
# perform_centaur_translate_handoff()
# ---------------------------------------------------------------------------

def test_translate_handoff_starts_gateway_before_launch_and_stops_after():
    """Translate mode must start the gateway, launch, then stop the gateway.

    Why: the gateway must be listening before centaur emits its first frame
    (else the opening screen is lost), and must be torn down after centaur exits.
    Asserts the exact order start -> launch -> stop. Failure manifests as 'launch'
    preceding 'start' (dropped first frame) or 'stop' missing (leaked server).
    """
    order = []
    start_gateway_fn = MagicMock(side_effect=lambda: order.append("start"))
    launch_fn = MagicMock(side_effect=lambda path: order.append("launch"))
    stop_gateway_fn = MagicMock(side_effect=lambda: order.append("stop"))

    result = perform_centaur_translate_handoff(
        software_path="/home/pi/centaur/centaur",
        start_gateway_fn=start_gateway_fn,
        launch_fn=launch_fn,
        stop_gateway_fn=stop_gateway_fn,
        path_exists_fn=lambda p: True,
    )

    assert result is True
    assert order == ["start", "launch", "stop"]
    launch_fn.assert_called_once_with("/home/pi/centaur/centaur")


def test_translate_handoff_stops_gateway_even_if_launch_raises():
    """The gateway must be stopped even if launching centaur raises.

    Why: a leaked gateway server would hold the socket and a thread after a
    failed launch. Asserts stop_gateway_fn runs despite launch_fn raising.
    """
    stop_gateway_fn = MagicMock()

    def _boom(path):
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError):
        perform_centaur_translate_handoff(
            software_path="/home/pi/centaur/centaur",
            start_gateway_fn=MagicMock(),
            launch_fn=_boom,
            stop_gateway_fn=stop_gateway_fn,
            path_exists_fn=lambda p: True,
        )

    stop_gateway_fn.assert_called_once()


def test_translate_handoff_starts_serial_and_gateway_before_launch_stops_both_after():
    """The serial tap starts first and stops last, wrapping the gateway + launch.

    Why this test exists: the tap owns the physical port, so it must be swapped in
    before centaur opens the board and restored only after centaur exits and the
    gateway is down. Asserts the exact nesting order. Regression manifests as the
    port being restored before the gateway stops, or the tap starting after
    launch (centaur would open the un-swapped port and the tap would see nothing).
    """
    order = []
    result = perform_centaur_translate_handoff(
        software_path="/home/pi/centaur/centaur",
        start_gateway_fn=lambda: order.append("start_gateway"),
        launch_fn=lambda path: order.append("launch"),
        stop_gateway_fn=lambda: order.append("stop_gateway"),
        start_serial_fn=lambda: order.append("start_serial"),
        stop_serial_fn=lambda: order.append("stop_serial"),
        path_exists_fn=lambda p: True,
    )

    assert result is True
    assert order == ["start_serial", "start_gateway", "launch", "stop_gateway", "stop_serial"]


def test_translate_handoff_stops_serial_and_gateway_even_if_launch_raises():
    """Both the gateway and the serial tap are torn down if launch raises.

    Why this test exists: a leaked serial tap leaves the port swapped (board
    unusable); a leaked gateway holds the socket. Asserts both stops run, in the
    right order, despite launch throwing. Regression manifests as the port never
    being restored after a failed launch.
    """
    order = []

    def _boom(path):
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError):
        perform_centaur_translate_handoff(
            software_path="/home/pi/centaur/centaur",
            start_gateway_fn=lambda: order.append("start_gateway"),
            launch_fn=_boom,
            stop_gateway_fn=lambda: order.append("stop_gateway"),
            start_serial_fn=lambda: order.append("start_serial"),
            stop_serial_fn=lambda: order.append("stop_serial"),
            path_exists_fn=lambda p: True,
        )

    assert order == ["start_serial", "start_gateway", "stop_gateway", "stop_serial"]


def test_translate_handoff_does_nothing_when_binary_absent():
    """Missing binary: do not start the gateway or launch.

    Asserts the result is False and neither side effect runs, so UC stays as-is.
    """
    start_gateway_fn = MagicMock()
    launch_fn = MagicMock()
    stop_gateway_fn = MagicMock()

    result = perform_centaur_translate_handoff(
        software_path="/home/pi/centaur/centaur",
        start_gateway_fn=start_gateway_fn,
        launch_fn=launch_fn,
        stop_gateway_fn=stop_gateway_fn,
        path_exists_fn=lambda p: False,
    )

    assert result is False
    start_gateway_fn.assert_not_called()
    launch_fn.assert_not_called()
    stop_gateway_fn.assert_not_called()


# ---------------------------------------------------------------------------
# centaur_direct_mode_enabled()
# ---------------------------------------------------------------------------

def test_direct_mode_defaults_to_false_translate_mode():
    """An unset [centaur] direct_mode must mean translate mode (False).

    Why: translate mode is the shipped default so centaur renders on any panel.
    The reader passes 'False' as the default to Settings.read, so an absent key
    must come back False. A regression (wrong default, inverted logic) would make
    a fresh board launch centaur in direct mode and blank a mismatched panel.

    The fake read_setting_fn returns the default it is given, emulating a missing
    key; the test asserts that default ('False') parses to False.
    """
    def read_missing(section, key, default):
        assert (section, key) == ("centaur", "direct_mode")
        return default

    assert centaur_direct_mode_enabled(read_missing) is False


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("True", True), ("true", True), ("1", True), ("on", True),
        ("yes", True), ("  TRUE  ", True),
        ("False", False), ("false", False), ("0", False), ("off", False),
        ("no", False), ("", False), ("garbage", False),
    ],
)
def test_direct_mode_parses_stored_value_leniently(stored, expected):
    """Stored direct_mode strings must parse to the right boolean.

    Why: the value is hand-editable in centaur.ini and written by the UI, so the
    parser must accept the common truthy spellings and treat everything else as
    off -- matching the other boolean flags (debug_serial, display flags). A
    regression manifests as a truthy spelling reading False (toggle appears to do
    nothing) or a non-truthy string reading True (unexpected direct mode).
    """
    assert centaur_direct_mode_enabled(lambda s, k, d: stored) is expected


# ---------------------------------------------------------------------------
# classify_centaur_exit()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "returncode,expected_level",
    [
        # Clean exit and expected terminations (return/exit chord, shutdown) are
        # info -- they are how a user normally leaves centaur, not failures.
        (0, "info"),
        (-signal.SIGTERM, "info"),                 # translate: killed directly
        (128 + int(signal.SIGTERM), "info"),        # direct: killed via sudo child
        (-signal.SIGINT, "info"),
        (-signal.SIGHUP, "info"),
        (-signal.SIGKILL, "info"),
        # Crash signals and any other non-zero exit are errors worth surfacing.
        (-signal.SIGSEGV, "error"),
        (128 + int(signal.SIGSEGV), "error"),       # sudo-wrapped crash
        (1, "error"),
        (2, "error"),                               # exit code 2 != killed by SIGINT
    ],
)
def test_classify_centaur_exit_levels(returncode, expected_level):
    """Exit codes must classify as info (expected) vs error (crash/failure).

    Why this test exists: the launch now logs/emits an Event Log entry based on
    this classification. The return/exit chord pkills centaur, which surfaces as
    a negative signal (translate) or 128+signal (direct via sudo); both must read
    as an expected 'info' termination, not a scary error. Conversely a crash
    (SIGSEGV, either encoding) or a non-zero exit must read as 'error' so a
    handed-over centaur that dies is visible instead of silently swallowed.

    A regression manifests as an expected termination logged as an error (noise
    every time the user returns from centaur) or a real crash logged as info
    (the failure the user reported staying invisible).
    """
    level, message = classify_centaur_exit(returncode)
    assert level == expected_level
    # The message must mention Centaur so the Event Log line is self-describing
    # without the numeric code.
    assert "Centaur" in message


# ---------------------------------------------------------------------------
# return_to_universal_chess()
# ---------------------------------------------------------------------------

def test_return_to_universal_chess_restarts_not_stops():
    """Returning from centaur must RESTART the service, never `stop` it.

    Why this test exists: this is the board-goes-dark regression. The board used
    `systemctl stop`, but an explicit stop is terminal -- systemd leaves the unit
    inactive regardless of Restart=on-failure -- so Universal Chess never came
    back. This pins the command to `restart` (via the shared constant) and pins
    the order settle -> restart -> exit.

    How a regression manifests: the command reverts to `stop` (board stays dead),
    the restart is skipped, or the settle/exit ordering changes so the restart
    fires before centaur has released the board.
    """
    order = []
    return_to_universal_chess(
        run_fn=lambda cmd: order.append(("run", cmd)),
        exit_fn=lambda code: order.append(("exit", code)),
        sleep_fn=lambda secs: order.append(("sleep", secs)),
        settle_seconds=3.0,
    )

    assert order == [
        ("sleep", 3.0),
        ("run", RESTART_UNIVERSAL_CHESS_CMD),
        ("exit", 1),
    ]
    # The command must be a restart, not a stop -- the exact bug being guarded.
    assert RESTART_UNIVERSAL_CHESS_CMD == [
        "sudo", "systemctl", "restart", "universal-chess.service",
    ]


def test_return_to_universal_chess_exits_nonzero_as_restart_fallback():
    """If the restart returns (did not replace us), exit non-zero as a fallback.

    Why this test exists: the restart normally kills this process during its stop
    phase, so the code after it never runs. But a failed/denied restart returns
    instead -- and then a plain exit(0) would leave the board dead. The fallback
    exit MUST be non-zero so Restart=on-failure still recovers the board.

    How a regression manifests: exit_fn is called with 0 (or not at all), so a
    failed restart leaves Universal Chess down with no recovery.
    """
    exit_codes = []
    return_to_universal_chess(
        run_fn=lambda cmd: None,  # simulates a restart that returned (did not kill us)
        exit_fn=exit_codes.append,
        sleep_fn=lambda secs: None,
    )

    assert exit_codes == [1]


# ---------------------------------------------------------------------------
# restart_exit_code()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pending,expected",
    [
        (SystemExit(1), 1),      # the return-from-Centaur fallback code
        (SystemExit(2), 2),      # any non-zero code is preserved verbatim
        (SystemExit(0), 0),      # explicit clean exit stays clean
        (SystemExit(None), 0),   # bare sys.exit() -> code None -> clean
        (SystemExit("boom"), 0), # string code (exit 1 + msg) is not our contract
        (None, 0),               # no exception in flight (normal loop end)
        (RuntimeError("x"), 0),  # a non-SystemExit that was already handled
        (KeyboardInterrupt(), 0),
    ],
)
def test_restart_exit_code_maps_pending_exception(pending, expected):
    """cleanup must exit non-zero iff a non-zero SystemExit is propagating.

    Why this test exists: this is the stock-board return-from-Centaur regression.
    return_to_universal_chess raises SystemExit(1) as a privilege-free fallback so
    systemd's Restart=on-failure brings Universal Chess back when `sudo systemctl
    restart` is denied (no passwordless-sudo grant on a stock board). That
    SystemExit unwinds into the main loop's `finally`, which runs cleanup_and_exit
    -- and cleanup ends the process itself. cleanup therefore must ADOPT the
    propagating SystemExit's code (via this function) instead of forcing 0, or the
    fallback is swallowed and the board stays dead. A clean/absent/handled exit
    must map to 0 so an ordinary shutdown still exits cleanly (systemd must not
    restart-loop a deliberate stop). Only an explicit integer code is honored,
    because the restart contract uses integer codes.

    How a regression manifests: SystemExit(1) mapping back to 0 reintroduces the
    board-goes-dark bug; a normal shutdown (None) mapping to non-zero would make
    systemd treat every clean exit as a failure.
    """
    assert restart_exit_code(pending) == expected


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
