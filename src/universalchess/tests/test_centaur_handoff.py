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
from universalchess.services.power import (
    centaur_direct_mode_enabled,
    perform_centaur_handoff,
    perform_centaur_translate_handoff,
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
    the first frame. Asserts the exact order release -> launch -> stop-service.
    Failure manifests as 'launch' preceding 'release' in the recorded order.
    """
    order = []
    display_manager = MagicMock()
    display_manager.release_hardware.side_effect = lambda: order.append("release")
    launch_fn = MagicMock(side_effect=lambda path: order.append("launch"))
    stop_service_fn = MagicMock(side_effect=lambda: order.append("stop"))

    result = perform_centaur_handoff(
        display_manager=display_manager,
        software_path="/home/pi/centaur/centaur",
        launch_fn=launch_fn,
        stop_service_fn=stop_service_fn,
        path_exists_fn=lambda p: True,
    )

    assert result is True
    assert order == ["release", "launch", "stop"]
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
    stop_service_fn = MagicMock()

    result = perform_centaur_handoff(
        display_manager=display_manager,
        software_path="/home/pi/centaur/centaur",
        launch_fn=launch_fn,
        stop_service_fn=stop_service_fn,
        path_exists_fn=lambda p: False,
    )

    assert result is False
    display_manager.release_hardware.assert_not_called()
    launch_fn.assert_not_called()
    stop_service_fn.assert_not_called()


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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
