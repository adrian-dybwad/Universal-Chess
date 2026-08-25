"""Leaving the original Centaur says so on the panel before the service restarts.

Returning from Centaur is not a quick swap: ``return_to_universal_chess`` settles
for three seconds and then restarts the unit, and Universal Chess needs roughly
another fifteen to import and paint its own startup splash. Across that whole gap
the panel held whatever was last drawn -- Centaur's final frame -- with nothing
to say the exit had registered. Observed on hardware, that reads as the board
having crashed and powered itself off, which is exactly how it was reported.

Translate mode never gives the panel away, so UC can draw on it the moment
Centaur exits. Direct mode does give it away, so it must take the hardware back
first -- the difference the two sets of tests below pin. e-ink holds an image
with no power, so the splash survives the restart either way and stays up until
UC's own startup splash replaces it.

The splash belongs only on the path that actually restarts. When the binary is
missing nothing was handed over and UC simply stays running, so announcing a
return would be a lie about what just happened.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.app import board_app


RETURNING_KEY = "splash.returning"
SPLASH = "splash"
RESTART = "restart"
REACQUIRE = "reacquire"


@pytest.fixture
def translate_handoff(monkeypatch):
    """Run ``_run_centaur_translate`` with the panel, tap and gateway faked.

    Returns ``(calls, set_launched)``. ``calls`` is the ordered log of the two
    steps under test; ``set_launched`` chooses whether the handoff reports that
    Centaur was launched, which is what distinguishes a real return from a
    missing binary.
    """
    from universalchess.services import power
    from universalchess.services import centaur_display, centaur_serial
    from universalchess.services.centaur_display import shim_builder

    calls = []
    state = {"launched": True}

    monkeypatch.setattr(shim_builder, "ensure_display_shim", lambda: None)

    # The panel manager: a MagicMock satisfies clear_widgets/add_widget and the
    # promise the splash code waits on, without touching a real driver.
    fake_board = MagicMock()
    monkeypatch.setattr(board_app, "board", fake_board)
    monkeypatch.setattr(board_app, "SplashScreen", MagicMock())
    monkeypatch.setattr(board_app.time, "sleep", lambda _s: None)

    monkeypatch.setattr(centaur_display, "CentaurDisplayGateway", MagicMock())
    monkeypatch.setattr(centaur_display, "ThreadedGatewayServer", MagicMock())
    monkeypatch.setattr(centaur_display, "render_and_signal", MagicMock())
    monkeypatch.setattr(centaur_serial, "SerialTap", MagicMock())
    monkeypatch.setattr(centaur_serial, "ThreadedSerialTap", MagicMock())
    monkeypatch.setattr(centaur_serial, "resolve_tap_device", lambda: "/dev/ttyS0")

    def _splash(manager, message, **kwargs):
        calls.append((SPLASH, message))
        return True

    monkeypatch.setattr(board_app, "show_fullscreen_splash", _splash)
    monkeypatch.setattr(board_app, "t", lambda key, **kw: key)

    def _handoff(**kwargs):
        return state["launched"]

    def _return(**kwargs):
        calls.append((RESTART, None))

    monkeypatch.setattr(power, "perform_centaur_translate_handoff", _handoff)
    monkeypatch.setattr(power, "return_to_universal_chess", _return)

    def _set_launched(launched: bool):
        state["launched"] = launched

    return calls, _set_launched


def test_returning_splash_is_painted_before_the_service_restarts(translate_handoff):
    """The panel must announce the return, and do so before the restart.

    Why the ordering is asserted rather than just the call: the restart kills
    this process, so a splash issued after it would never reach the panel. The
    whole point is to fill the gap the restart creates, which means it has to be
    on screen before the restart is asked for.

    How the regression manifests: ``calls`` holds only the restart entry (the
    panel stays on Centaur's last frame for ~18s and still looks like a crash),
    or holds the two entries in the wrong order (the splash never renders).
    """
    calls, _ = translate_handoff

    board_app._run_centaur_translate()

    assert calls == [(SPLASH, RETURNING_KEY), (RESTART, None)]


def test_no_returning_splash_when_the_centaur_binary_is_missing(translate_handoff):
    """A handoff that never happened must not claim to be returning from one.

    Why this test exists: when the binary is absent the handoff returns False,
    UC stays running with its menu, and nothing restarts. Painting "Returning..."
    there would replace a live menu with a message about an event that did not
    occur, and nothing would come along to clear it.

    How the regression manifests: the splash is painted unconditionally, so a
    board with no Centaur installed blanks its menu to a false status the moment
    the action is chosen.
    """
    calls, set_launched = translate_handoff
    set_launched(False)

    result = board_app._run_centaur_translate()

    assert result is False
    assert calls == []


@pytest.fixture
def direct_handoff(monkeypatch):
    """Run ``_run_centaur`` with the panel faked and centaur never executed.

    The handoff stand-in keeps the real contract -- release the panel, then run
    the exit hook -- because the splash lives inside that hook, so mocking the
    handoff away entirely would test nothing. ``launch_fn`` is deliberately not
    called: starting centaur is not what these tests are about.

    Returns the ordered call log.
    """
    from universalchess.services import power

    calls = []

    fake_board = MagicMock()
    fake_board.display_manager.reacquire_hardware.side_effect = (
        lambda: calls.append((REACQUIRE, None))
    )
    monkeypatch.setattr(board_app, "board", fake_board)
    monkeypatch.setattr(board_app, "SplashScreen", MagicMock())
    monkeypatch.setattr(board_app.time, "sleep", lambda _s: None)
    monkeypatch.setattr(board_app, "t", lambda key, **kw: key)

    def _splash(manager, message, **kwargs):
        calls.append((SPLASH, message))
        return True

    monkeypatch.setattr(board_app, "show_fullscreen_splash", _splash)

    def _handoff(display_manager, software_path, launch_fn, on_centaur_exit_fn, **kw):
        display_manager.release_hardware()
        on_centaur_exit_fn()
        return True

    monkeypatch.setattr(power, "perform_centaur_handoff", _handoff)
    monkeypatch.setattr(power, "return_to_universal_chess",
                        lambda **kw: calls.append((RESTART, None)))

    return calls, fake_board


def test_direct_mode_takes_the_panel_back_before_painting_the_splash(direct_handoff):
    """Direct mode must re-acquire the hardware, then splash, then restart.

    Why this test exists: direct mode releases the panel so centaur can drive it
    natively -- SPI closed, GPIO lines freed, scheduler stopped. Drawing without
    taking that back first writes to closed hardware and the splash never
    appears, so the ordering is the whole behaviour. The restart must come last
    for the same reason as translate mode: it kills the process that would draw.

    How the regression manifests: the re-acquire is missing (nothing renders, on
    the mode where the panel was genuinely given away), or the splash is ordered
    after the restart and never reaches the panel.
    """
    calls, _ = direct_handoff

    with pytest.raises(SystemExit):
        board_app._run_centaur()

    assert calls == [
        (REACQUIRE, None),
        (SPLASH, RETURNING_KEY),
        (RESTART, None),
    ]


def test_a_failed_panel_reacquire_still_restarts_the_service(direct_handoff):
    """Losing the panel must not cost the user their board.

    Why this test exists: the splash is cosmetic, the restart is not. Taking the
    panel back re-opens SPI and the GPIO lines, which can fail on hardware that
    centaur left in an odd state. If that exception escaped the exit hook,
    ``return_to_universal_chess`` would never run and the unit would sit stopped
    with a dead board -- the precise outcome that function exists to avoid, and a
    far worse trade than a missing message. The same reasoning as the
    best-effort panel settle in ``release_hardware``.

    How the regression manifests: the restart is absent from ``calls`` because
    the error propagated, so Universal Chess never comes back after Centaur.
    """
    calls, fake_board = direct_handoff
    fake_board.display_manager.reacquire_hardware.side_effect = RuntimeError(
        "SPI busy"
    )

    with pytest.raises(SystemExit):
        board_app._run_centaur()

    assert (RESTART, None) in calls
