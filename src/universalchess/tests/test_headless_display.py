"""A board whose e-paper panel never initializes keeps running.

Why these tests exist:
    Display bring-up used to return no Manager when neither controller could
    drive the panel, and the application then retried on its own with the
    default UC8151D driver. That retry raised the same initialization error
    with nothing to catch it, so ``board_app.main`` exited before the game loop
    started and systemd restarted the service in a loop. Startup now hands the
    application the Manager whose panel did not initialize: it keeps the widget
    stack every screen builds and renders each screen for the web mirror
    without driving the panel, so the remote view is the board's screen. Two
    callers must still tell that Manager apart from a live panel -- the
    Bluetooth pairing prompt, which would otherwise accept invisible key
    presses, and live display tuning, which would otherwise adopt a driver and
    report a panel that nothing paints.

How a regression manifests:
    - A dead panel's Manager touches the driver after the failed init, or
      reports itself initialized, so the board drives hardware that is not there.
    - A dead panel's Manager mirrors nothing, so the remote view shows no screen
      (or the previous boot's) on the one board where it is the only screen.
    - A pairing request on a board with no panel raises a prompt (and a web
      mirror of it) that the board keys can answer without the user seeing it.
    - A display-tuning change on a board with no panel calls into the Manager.
"""

import threading
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from universalchess.app import board_app, display_boot
from universalchess.app.pending_work import PendingWork
from universalchess.board import board
from universalchess.board.display_selection import DisplayAttempt
from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.waveshare.panel_models import PanelDriver
from universalchess.epaper.framework.widget import Widget
from universalchess.services import chromecast, event_log

PANEL_WIDTH = 128
PANEL_HEIGHT = 296
INIT_OK = 0
INIT_FAILED = -1
FUTURE_TIMEOUT_SECONDS = 2.0
# Bounds the prompt's wait if the no-panel refusal regresses, so the test fails
# on its assertions instead of blocking for the production 30 s.
PAIRING_TIMEOUT_SECONDS = 0.05
MIRRORED = "mirrored"
NOT_INITIALIZED = "not-initialized"
# Long enough for the coalesced flush's 1 ms timer and the live scheduler's
# batching delay; a regression waits this long once and then fails.
MIRROR_WAIT_SECONDS = 3.0
# Inked in the top-left corner only, so a frame mirrored with the wrong
# rotation differs from the live panel's instead of matching by symmetry.
INK_BOX = (0, 0, 40, 60)
BLACK = 0


class _Panel(PanelDriver):
    """EPD stand-in that records every call that would reach the hardware."""

    width = PANEL_WIDTH
    height = PANEL_HEIGHT
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
    """Full-screen widget inking one corner, standing in for a screen's content."""

    def render(self, sprite):
        ImageDraw.Draw(sprite).rectangle(INK_BOX, fill=BLACK)


class _MirrorRecorder:
    """``on_refresh`` stand-in that records each mirrored frame."""

    def __init__(self):
        self.frames = []
        self._first = threading.Event()

    def __call__(self, image, red_image=None):
        self.frames.append((image, red_image))
        self._first.set()

    def wait_for_first(self) -> bool:
        return self._first.wait(MIRROR_WAIT_SECONDS)


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


def _screen_with_widget(manager: Manager) -> Widget:
    """Compose a one-widget screen without painting it, as a screen transition does."""
    manager.clear_widgets(addStatusBar=False)
    widget = _StubWidget(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager._attach_widget(widget)
    return widget


def _external_frame() -> Image.Image:
    frame = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 255)
    ImageDraw.Draw(frame).rectangle(INK_BOX, fill=BLACK)
    return frame


# Every way a frame is produced: a screen change, a direct priority refresh, a
# routine widget change (coalesced onto a timer), the clock's tick, and a frame
# from the original-Centaur display gateway, which bypasses the widget stack.
_FRAME_SOURCES = {
    "add_widget": lambda m: m.add_widget(
        _StubWidget(0, 0, PANEL_WIDTH, PANEL_HEIGHT, m.update)),
    "priority_update": lambda m: m.update(full=True, immediate=True),
    "routine_update": lambda m: m.update(priority=False),
    "clock_tick": lambda m: m.flush_now(),
    "external_frame": lambda m: m.display_frame(_external_frame()),
}


@pytest.mark.parametrize("source", sorted(_FRAME_SOURCES))
def test_a_manager_whose_panel_did_not_initialize_mirrors_each_frame_without_the_panel(source):
    """Each frame a dead panel's Manager produces reaches the web mirror, once.

    Why: on a board with no panel the remote view is the only screen there is,
    and it is fed solely by ``on_refresh``. The Manager used to return before
    rendering, so nothing was mirrored and the view showed a broken image or
    the previous boot's frame. How a regression manifests: ``frames`` is empty
    (the early return is back), has two entries (a frame mirrored twice), the
    future never resolves (the frame went to the scheduler queue no thread
    drains), or ``calls`` gains a driver call after "init".
    """
    recorder = _MirrorRecorder()
    manager = _manager_after_init(INIT_FAILED, on_refresh=recorder)
    _screen_with_widget(manager)

    try:
        result = _FRAME_SOURCES[source](manager)
        if result is not None:
            assert result.result(timeout=FUTURE_TIMEOUT_SECONDS) in (MIRRORED, "deferred")
        assert recorder.wait_for_first()

        assert len(recorder.frames) == 1
        image, red_image = recorder.frames[0]
        assert image.size == (PANEL_WIDTH, PANEL_HEIGHT)
        assert red_image is None
        assert manager.initialized is False
        assert manager.epd.calls == ["init"]
    finally:
        manager.shutdown()


def test_the_headless_mirror_shows_the_frame_a_live_panel_would_mirror():
    """Without a panel the mirrored frame is pixel-identical to a live panel's.

    Why: the mirror applies the same rotation to every frame it is given
    (``write_epaper_jpg`` turns it 180 degrees), so a headless frame must be
    the panel-oriented frame the scheduler would have shown, not the logical
    canvas. How a regression manifests: the byte comparison fails because the
    headless frame skipped (or doubled) the panel rotation and the remote view
    is upside down; the corner-only ink keeps a rotated frame from matching by
    symmetry.
    """
    live_recorder, headless_recorder = _MirrorRecorder(), _MirrorRecorder()
    live = _manager_after_init(INIT_OK, on_refresh=live_recorder)
    headless = _manager_after_init(INIT_FAILED, on_refresh=headless_recorder)

    try:
        for manager, recorder in ((live, live_recorder), (headless, headless_recorder)):
            _screen_with_widget(manager)
            manager.update(full=False, immediate=True)
            assert recorder.wait_for_first()

        live_image, _ = live_recorder.frames[0]
        headless_image, _ = headless_recorder.frames[0]
        assert headless_image.mode == live_image.mode
        assert headless_image.size == live_image.size
        assert headless_image.tobytes() == live_image.tobytes()
    finally:
        live.shutdown()
        headless.shutdown()


def test_a_dead_panels_manager_mirrors_nothing_once_it_is_shutting_down():
    """Shutdown still drops every frame on a Manager without a panel.

    Why: ``_shutting_down`` is also raised while the original Centaur software
    owns the board, and the mirror must not keep publishing this application's
    screens over that. How a regression manifests: ``frames`` is not empty, or
    the result is "mirrored", because the headless path ignored the gate.
    """
    recorder = _MirrorRecorder()
    manager = _manager_after_init(INIT_FAILED, on_refresh=recorder)
    _screen_with_widget(manager)
    manager.shutdown()

    result = manager.update(full=False, immediate=True).result(timeout=FUTURE_TIMEOUT_SECONDS)

    assert result == NOT_INITIALIZED
    assert manager.flush_now() is None
    assert recorder.wait_for_first() is False
    assert recorder.frames == []


def _probe_finds_no_panel(monkeypatch, tmp_path):
    """Make init_display's controller probe fail without touching hardware."""
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(event_log, "_loggers", {})
    monkeypatch.setattr(display_boot, "read_waveform_profile_key", lambda: "")
    monkeypatch.setattr(display_boot, "read_flag", lambda *_a, **_k: False)
    monkeypatch.setattr(display_boot, "build_epd",
                        lambda *_a, **_k: (MagicMock(), MagicMock(key="x")))
    monkeypatch.setattr(
        display_boot, "attempt_display_init",
        lambda *_a, **_k: (MagicMock(), DisplayAttempt(ok=False, busy_timeout=False,
                                                       error="no panel")),
    )
    monkeypatch.setattr("universalchess.board.hardware_info.read_display_status", lambda: None)
    monkeypatch.setattr("universalchess.board.hardware_info.write_display_status",
                        lambda **_k: None)


@pytest.mark.parametrize("left_by_previous_boot", [True, False])
def test_boot_discards_the_previous_boots_mirror_frame(monkeypatch, tmp_path,
                                                       left_by_previous_boot):
    """Display bring-up removes epaper.jpg before the first frame of this boot.

    Why: the file lives on disk and survives a reboot, and ``/screen.jpg`` serves
    whatever is there, so the remote view presented the last frame of the
    previous boot as the current screen until this boot drew one. The absent
    case checks that a fresh install, with nothing to remove, still boots. How
    a regression manifests: the file still exists after init_display, or the
    absent case raises FileNotFoundError and display bring-up fails.
    """
    snapshot = tmp_path / "epaper.jpg"
    if left_by_previous_boot:
        snapshot.write_bytes(b"\xff\xd8previous boot\xff\xd9")
    monkeypatch.setattr(chromecast, "EPAPER_STATIC_JPG", str(snapshot))
    _probe_finds_no_panel(monkeypatch, tmp_path)

    _manager, splash = display_boot.init_display()

    assert splash is None
    assert not snapshot.exists()


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
    monkeypatch.setattr(board_app.display_settings, "read_waveform_profile_key", lambda: "")
    monkeypatch.setattr(board_app.display_settings, "read_flag", lambda *_a, **_k: False)

    board_app._process_pending_display_profile()

    assert manager.method_calls == []
    assert pending.display_profile.requested() is False
