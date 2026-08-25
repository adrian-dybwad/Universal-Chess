"""Leaving the original Centaur says so on the panel before the service restarts.

Returning from Centaur is not a quick swap: ``return_to_universal_chess`` settles
for three seconds and then restarts the unit, and Universal Chess needs roughly
another fifteen to import and paint its own startup splash. Across that whole gap
the panel held whatever was last drawn -- Centaur's final frame -- with nothing
to say the exit had registered. Observed on hardware, that reads as the board
having crashed and powered itself off, which is exactly how it was reported.

Translate mode never gives the panel away (that is the difference from direct
mode), so UC can still draw on it the moment Centaur exits. e-ink holds an image
with no power, so the splash painted here survives the restart and stays up until
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
