"""A board whose e-paper panel never initializes keeps running.

Why these tests exist:
    Display bring-up used to return no Manager when neither controller could
    drive the panel, and the application then retried on its own with the
    default UC8151D driver. That retry raised the same initialization error
    with nothing to catch it, so ``board_app.main`` exited before the game loop
    started and systemd restarted the service in a loop. Startup now hands the
    application the Manager whose panel did not initialize: it keeps the widget
    stack every screen builds and paints nothing. Two callers must still tell
    that Manager apart from a live panel -- the Bluetooth pairing prompt, which
    would otherwise accept invisible key presses, and live display tuning, which
    would otherwise adopt a driver and report a panel that nothing paints.

How a regression manifests:
    - A dead panel's Manager touches the driver after the failed init, or
      reports itself initialized, so the board drives hardware that is not there.
    - A pairing request on a board with no panel raises a prompt (and a web
      mirror of it) that the board keys can answer without the user seeing it.
    - A display-tuning change on a board with no panel calls into the Manager.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.app import board_app
from universalchess.app.pending_work import PendingWork
from universalchess.board import board
from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.widget import Widget

PANEL_WIDTH = 128
PANEL_HEIGHT = 296
INIT_OK = 0
INIT_FAILED = -1
FUTURE_TIMEOUT_SECONDS = 2.0
# Bounds the prompt's wait if the no-panel refusal regresses, so the test fails
# on its assertions instead of blocking for the production 30 s.
PAIRING_TIMEOUT_SECONDS = 0.05
NOT_INITIALIZED = "not-initialized"


class _Panel:
    """EPD stand-in that records every call that would reach the hardware."""

    width = PANEL_WIDTH
    height = PANEL_HEIGHT
    gray_levels = 1
    three_color = False

    def __init__(self, init_result: int):
        self._init_result = init_result
        self.calls = []

    def init(self):
        self.calls.append("init")
        return self._init_result

    def getbuffer(self, _image):
        self.calls.append("getbuffer")
        return b""

    def display(self, *_args, **_kwargs):
        self.calls.append("display")

    def DisplayPartial(self, *_args, **_kwargs):  # noqa: N802 - Waveshare driver API
        self.calls.append("DisplayPartial")

    def Clear(self, *_args, **_kwargs):  # noqa: N802 - Waveshare driver API
        self.calls.append("Clear")

    def sleep(self):
        self.calls.append("sleep")

    def idle_sleep(self):
        self.calls.append("idle_sleep")


class _StubWidget(Widget):
    """Bare full-screen widget, standing in for a screen's content."""

    def render(self, _image):
        return None


def _manager_after_init(init_result: int, on_refresh=None) -> Manager:
    """A Manager over ``_Panel`` after one initialize() attempt."""
    manager = Manager(on_refresh=on_refresh, epd=_Panel(init_result))
    if init_result == INIT_OK:
        manager.initialize()
    else:
        with pytest.raises(RuntimeError):
            manager.initialize()
    return manager


@pytest.mark.parametrize(("init_result", "expected"), [(INIT_OK, True), (INIT_FAILED, False)])
def test_initialized_reports_whether_the_panel_driver_came_up(init_result, expected):
    """``Manager.initialized`` is the driver's init() outcome.

    Why: startup now hands the application a Manager whether or not the panel
    answered, so callers that must not act on a dead panel ask this instead of
    testing for None. How a regression manifests: the property is missing or
    True for a dead panel, and pairing or display tuning acts on it.
    """
    manager = _manager_after_init(init_result)
    try:
        assert manager.initialized is expected
    finally:
        manager.shutdown()


def test_a_manager_whose_panel_did_not_initialize_paints_nothing():
    """Every drawing call on a dead panel's Manager is accepted and paints nothing.

    Why: the application builds screens through ``board.display_manager``
    without checking for a panel, so headless running depends on this Manager
    taking widgets and refresh requests without driving the hardware. How a
    regression manifests: ``calls`` gains a refresh after "init", the web
    mirror callback fires, or one of these calls raises and takes the board
    down the way the late retry did.
    """
    refreshed = []
    manager = _manager_after_init(INIT_FAILED, on_refresh=lambda *frames: refreshed.append(frames))

    manager.clear_widgets(addStatusBar=False)
    widget = _StubWidget(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    painted = [
        manager.add_widget(widget).result(timeout=FUTURE_TIMEOUT_SECONDS),
        manager.update(full=True, immediate=True).result(timeout=FUTURE_TIMEOUT_SECONDS),
        manager.remove_widget(widget).result(timeout=FUTURE_TIMEOUT_SECONDS),
    ]
    tick = manager.flush_now()

    assert manager.initialized is False
    assert painted == [NOT_INITIALIZED, NOT_INITIALIZED, NOT_INITIALIZED]
    assert tick is None
    assert manager.epd.calls == ["init"]
    assert refreshed == []


def test_pairing_is_refused_without_a_prompt_when_the_panel_did_not_initialize(monkeypatch):
    """No panel means no on-board Pair/Reject prompt and an immediate refusal.

    Why: the prompt routes the board keys to itself. On a dead panel the user
    cannot see it, so a key pressed for another reason could accept a pairing.
    This refusal used to come from ``board.display_manager is None``, which no
    longer holds on a board without a panel. How a regression manifests:
    ``broadcasts`` contains "bt_pair_request" because the prompt was raised and
    mirrored to the web before timing out.
    """
    broadcasts = []
    monkeypatch.setattr(board, "display_manager", _manager_after_init(INIT_FAILED), raising=False)
    monkeypatch.setattr(board, "beep", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(board_app, "PAIRING_CONFIRM_TIMEOUT_SECONDS", PAIRING_TIMEOUT_SECONDS)
    monkeypatch.setattr(board_app, "_broadcast_bt_event",
                        lambda event_type, _data=None: broadcasts.append(event_type))

    accepted = board_app._confirm_pairing_on_board("123456")

    assert accepted is False
    assert broadcasts == []


def test_display_tuning_is_left_for_the_next_boot_when_the_panel_did_not_initialize(monkeypatch):
    """A tuning change on a board with no panel consumes the request and touches nothing.

    Why: the settings are already saved, and the next boot's probe reads them.
    Acting on the dead panel's Manager would adopt drivers for hardware that
    did not answer at boot, and a panel swap would publish an initialized
    panel to the System card while nothing paints. How a regression manifests:
    ``manager.method_calls`` is not empty, or the request stays pending and
    the main loop re-runs it every pass.
    """
    pending = PendingWork()
    pending.display_profile.request({})
    manager = MagicMock(spec=Manager)
    manager.initialized = False
    monkeypatch.setattr(board_app, "_pending", pending)
    monkeypatch.setattr(board, "display_manager", manager, raising=False)
    monkeypatch.setattr(board_app.display_settings, "read_selection", lambda: ("", False))
    monkeypatch.setattr(board_app.display_settings, "read_flag", lambda *_a, **_k: False)

    board_app._process_pending_display_profile()

    assert manager.method_calls == []
    assert pending.display_profile.requested() is False
