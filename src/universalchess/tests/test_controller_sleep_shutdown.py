"""Tests for the shutdown-time controller sleep path.

The DGT Centaur controller keeps its own battery, so a Pi that powers off
without sleeping the controller leaves it drawing current until the battery is
flat. Two processes can perform that sleep: the main service during its own
cleanup, and ``universalchess.board.shutdown`` -- the systemd fallback hook that
exists for the case where the main service is not running (crashed or stopped).

The fallback hook ran 26 times on a development board and failed all 26: it runs
in its own process, which never calls ``init_board``, so the module-level
``controller`` was ``None`` and ``controller.sleep()`` raised ``AttributeError``.
These tests pin both halves of the fix -- the hook can initialise a controller
for itself, and it skips the work when the main service already slept the board
this boot (recorded by a stamp under ``/run/universalchess``, which systemd
empties on every boot so a stale stamp cannot silently disable the hook).
"""

from __future__ import annotations

import pytest

from universalchess.board import board
from universalchess.board import shutdown as shutdown_hook
from universalchess.services import event_log

# The category the viewer already knows how to label; see EVENT_CATEGORY_LABEL_KEYS
# in Settings.tsx, where an unmapped category renders as its raw token.
EVENT_CATEGORY = "system"


class _FakeController:
    """SyncCentaur stand-in recording only the calls the sleep path makes.

    The real controller opens a serial port and starts listener threads, so the
    boundary is mocked here while the tests still enter through the public
    ``sleep_controller`` / hook entry points.
    """

    def __init__(self, *, ready: bool = True, sleep_result: bool = True) -> None:
        self._ready = ready
        self._sleep_result = sleep_result
        self.wait_ready_timeouts: list[float] = []
        self.sleep_calls = 0
        self.sleep_retries: list[tuple[int, float]] = []

    def wait_ready(self, timeout: float) -> bool:
        self.wait_ready_timeouts.append(timeout)
        return self._ready

    def sleep(self, retries: int = 3, retry_delay: float = 0.5) -> bool:
        self.sleep_calls += 1
        self.sleep_retries.append((retries, retry_delay))
        return self._sleep_result


@pytest.fixture
def stamp(tmp_path, monkeypatch):
    """Redirect the slept-stamp away from the real /run path."""
    path = tmp_path / "controller-slept"
    monkeypatch.setattr(board, "CONTROLLER_SLEPT_STAMP", path)
    return path


@pytest.fixture
def recorded_events(tmp_path, monkeypatch):
    """Redirect the event log to a temporary file and read it back.

    Returns a callable so each test reads the events written by the call it just
    made, rather than a snapshot taken before the hook ran.
    """
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(path))
    # The module caches one logger per resolved path; a stale handler from an
    # earlier test's tmp_path would send this test's records to a deleted file.
    monkeypatch.setattr(event_log, "_loggers", {})
    return lambda: event_log.read_events(path=path)


@pytest.fixture(autouse=True)
def _no_controller_in_this_process(monkeypatch):
    """Start every test from the hook's situation: no controller initialised."""
    monkeypatch.setattr(board, "controller", None)


@pytest.fixture
def created_controllers(monkeypatch):
    """Record every controller the sleep path constructs."""
    created: list[_FakeController] = []

    def _factory_for(fake: _FakeController):
        def _create():
            created.append(fake)
            return fake

        return _create

    monkeypatch.setattr(board, "_create_controller", lambda: pytest.fail(
        "sleep_controller constructed a controller without the test arming one"
    ))
    return created, _factory_for


def test_sleep_controller_initialises_when_the_process_has_no_controller(
    monkeypatch, stamp, created_controllers
):
    """The fallback hook's own process must be able to sleep the controller.

    Why: ``controller`` is only assigned by ``init_board``, which runs in the
    main service. The hook never calls it, so before this fix the global was
    ``None`` and every shutdown logged "battery may drain" while the controller
    stayed powered. How a regression manifests: ``sleep`` is never called and
    ``sleep_controller`` returns False because ``None.sleep`` raises.
    """
    created, factory_for = created_controllers
    fake = _FakeController()
    monkeypatch.setattr(board, "_create_controller", factory_for(fake))

    assert board.sleep_controller() is True
    assert fake.sleep_calls == 1
    assert len(created) == 1
    # Retries are what make the sleep confirmed rather than fire-and-forget; a
    # single unretried attempt would silently accept a dropped command.
    assert fake.sleep_retries == [(3, 0.5)]
    # Bounded wait: the unit has no start timeout, so an unbounded wait here
    # would stall shutdown indefinitely rather than give up.
    assert fake.wait_ready_timeouts == [board.SHUTDOWN_INIT_TIMEOUT_SECONDS]
    assert stamp.exists()


def test_sleep_controller_reuses_a_controller_this_process_already_has(
    monkeypatch, stamp, created_controllers
):
    """The main service's own cleanup must not get a second controller.

    Why: constructing one reopens the serial port the live controller holds.
    How a regression manifests: ``_create_controller`` is called (the fixture
    fails the test) or ``wait_ready`` is waited on despite a ready controller.
    """
    created, _ = created_controllers
    fake = _FakeController()
    monkeypatch.setattr(board, "controller", fake)

    assert board.sleep_controller() is True
    assert fake.sleep_calls == 1
    assert created == []
    assert fake.wait_ready_timeouts == []
    assert stamp.exists()


def test_sleep_controller_gives_up_when_no_board_answers(
    monkeypatch, stamp, created_controllers
):
    """An absent or already-sleeping board must not be reported as slept.

    Why: the stamp means "the controller is asleep". Claiming success when
    discovery never completed would suppress a later attempt for a board that is
    still powered. How a regression manifests: the stamp appears, or ``sleep``
    is called against a controller that never became ready.
    """
    _created, factory_for = created_controllers
    fake = _FakeController(ready=False)
    monkeypatch.setattr(board, "_create_controller", factory_for(fake))

    assert board.sleep_controller() is False
    assert fake.sleep_calls == 0
    assert fake.wait_ready_timeouts == [board.SHUTDOWN_INIT_TIMEOUT_SECONDS]
    assert not stamp.exists()


def test_sleep_controller_records_nothing_when_the_controller_refuses(
    monkeypatch, stamp
):
    """A controller that never acknowledges must leave no stamp.

    Why: the stamp is what stops the fallback hook from retrying, so writing it
    after an unacknowledged sleep would leave a powered controller with nothing
    left to try. How a regression manifests: stamp exists after a False sleep.
    """
    fake = _FakeController(sleep_result=False)
    monkeypatch.setattr(board, "controller", fake)

    assert board.sleep_controller() is False
    assert fake.sleep_calls == 1
    assert not stamp.exists()


def test_sleep_still_succeeds_when_the_stamp_cannot_be_written(
    monkeypatch, tmp_path
):
    """A stamp the process cannot write must not fail the sleep.

    Why: the stamp is only an optimisation for the second sleeper. Failing the
    sleep because /run is unwritable would turn a cosmetic problem into a
    drained battery. How a regression manifests: sleep_controller returns False
    (or raises OSError) even though the controller acknowledged.
    """
    monkeypatch.setattr(
        board, "CONTROLLER_SLEPT_STAMP", tmp_path / "missing-dir" / "controller-slept"
    )
    fake = _FakeController()
    monkeypatch.setattr(board, "controller", fake)

    assert board.sleep_controller() is True
    assert fake.sleep_calls == 1


def test_hook_skips_when_the_controller_was_already_slept_this_boot(
    monkeypatch, stamp
):
    """The hook must do nothing once the main service has slept the board.

    Why: the hook is pulled into every power-off (stopping an inactive oneshot
    cannot prevent it), so after a clean app shutdown it used to probe an
    already-sleeping board and log a false "battery may drain". How a regression
    manifests: sleep_controller is invoked despite the stamp.
    """
    stamp.touch()
    calls = {"n": 0}

    def _should_not_run() -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(board, "sleep_controller", _should_not_run)

    assert shutdown_hook.main() == 0
    assert calls["n"] == 0


@pytest.mark.usefixtures("stamp")
def test_hook_defers_while_the_main_service_still_owns_the_controller(monkeypatch):
    """The hook must not sleep a controller another process is still using.

    Why: the hook exists for a main service that is *not* running. If it runs
    while the service is up (a manual start, or an unordered shutdown
    transaction) initialising its own controller would open a second connection
    to the serial port the live controller holds. Standing down does not mean
    the controller gets slept -- the service does that only for a shutdown it
    initiated itself -- which is why the unit is ordered after that service has
    stopped. How a regression manifests: sleep_controller is called while the
    service is reported active.
    """
    calls = {"n": 0}

    def _should_not_run() -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(board, "sleep_controller", _should_not_run)
    monkeypatch.setattr(shutdown_hook, "_main_service_is_active", lambda: True)

    assert shutdown_hook.main() == 0
    assert calls["n"] == 0


@pytest.mark.parametrize(
    ("slept", "expected_exit"),
    [(True, 0), (False, 1)],
)
def test_hook_sleeps_the_controller_when_no_stamp_exists(
    monkeypatch, stamp, slept, expected_exit
):
    """With no stamp the hook is the only thing that can sleep the board.

    Why: this is the crash/stopped-service case the hook exists for. The exit
    code must distinguish an acknowledged sleep from a failed one so the journal
    records which shutdowns left the controller powered. How a regression
    manifests: the hook exits 0 on failure (hiding a draining battery) or skips
    the sleep entirely.
    """
    assert not stamp.exists()
    calls = {"n": 0}

    def _sleep() -> bool:
        calls["n"] += 1
        return slept

    monkeypatch.setattr(board, "sleep_controller", _sleep)
    monkeypatch.setattr(shutdown_hook, "_main_service_is_active", lambda: False)

    assert shutdown_hook.main() == expected_exit
    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("slept", "expected_level", "expected_phrase"),
    [
        (True, "info", "asleep"),
        (False, "error", "battery"),
    ],
)
@pytest.mark.usefixtures("stamp")
def test_hook_reports_what_it_did_to_the_event_log(
    monkeypatch, recorded_events, slept, expected_level, expected_phrase
):
    """The outcome must survive into the persistent, user-visible log.

    Why: the hook's own log lines reach only the journal, and the board ships
    ``Storage=volatile``, so on the next boot there is no record that a
    controller was left powered -- which is how the hook's total failure went
    unnoticed for 26 shutdowns. The event log is written to /var/lib and shown
    in Settings, so a user whose controller battery drains can see the reason.

    How a regression manifests: no record appears, or a failed sleep is filed at
    a severity the viewer does not surface as a problem.
    """
    monkeypatch.setattr(board, "sleep_controller", lambda: slept)
    monkeypatch.setattr(shutdown_hook, "_main_service_is_active", lambda: False)

    shutdown_hook.main()

    events = recorded_events()
    assert len(events) == 1
    assert events[0]["category"] == EVENT_CATEGORY
    assert events[0]["level"] == expected_level
    assert expected_phrase in events[0]["message"].lower()
    assert "controller" in events[0]["message"].lower()


def test_hook_reports_standing_down_as_a_warning(monkeypatch, stamp, recorded_events):
    """Standing down leaves the controller powered, so it must be recorded.

    Why: the service the hook defers to sleeps the controller only for a
    shutdown it initiated itself. Filing this as a bland success would describe
    a drained battery as a normal power-off.

    How a regression manifests: nothing is recorded, or the record is filed at
    ``info`` so it reads as the hook having handled the shutdown.
    """
    assert not stamp.exists()
    monkeypatch.setattr(board, "sleep_controller", lambda: True)
    monkeypatch.setattr(shutdown_hook, "_main_service_is_active", lambda: True)

    shutdown_hook.main()

    events = recorded_events()
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert events[0]["category"] == EVENT_CATEGORY
    assert "controller" in events[0]["message"].lower()


def test_hook_records_nothing_when_another_process_already_slept_the_board(
    monkeypatch, stamp, recorded_events
):
    """The ordinary shutdown must not add a line to the event log.

    Why: the menu and web shutdowns sleep the controller themselves and leave
    the stamp, so the hook skips. That is every normal power-off; a record each
    time would bury the events the log exists to show.

    How a regression manifests: the log gains one entry per power-off and the
    viewer fills with routine noise.
    """
    stamp.touch()
    monkeypatch.setattr(board, "sleep_controller", lambda: pytest.fail(
        "the hook slept a controller the stamp says is already asleep"
    ))

    assert shutdown_hook.main() == 0
    assert recorded_events() == []


def test_hook_records_an_outright_failure(monkeypatch, stamp, recorded_events):
    """A hook that fails before reaching the controller must say so.

    Why: an exception here means nobody slept the controller, which is
    indistinguishable to the user from a flat battery days later unless it is
    recorded.

    How a regression manifests: the exception is swallowed into the volatile
    journal alone and the event log shows a power-off that looks clean.
    """
    assert not stamp.exists()

    def _explode() -> bool:
        message = "serial port gone"
        raise OSError(message)

    monkeypatch.setattr(board, "controller_slept_this_boot", _explode)

    assert shutdown_hook.main() == 1

    events = recorded_events()
    assert len(events) == 1
    assert events[0]["level"] == "error"
    assert events[0]["category"] == EVENT_CATEGORY
    assert "serial port gone" in events[0]["message"]


@pytest.mark.usefixtures("stamp")
def test_unknown_main_service_state_still_sleeps_the_controller(monkeypatch):
    """When the service state cannot be read, the hook must still do the work.

    Why: the guard exists to avoid a second serial connection, not to become a
    new reason to skip. If systemctl is missing or fails, refusing would leave
    the controller powered -- the exact failure this hook prevents -- so the
    unknown case resolves to "not active". How a regression manifests: a
    systemctl failure propagates or the hook returns without sleeping.
    """
    def _explode(*_a, **_k):
        message = "systemctl"
        raise FileNotFoundError(message)

    monkeypatch.setattr(shutdown_hook.subprocess, "run", _explode)
    calls = {"n": 0}

    def _sleep() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(board, "sleep_controller", _sleep)

    assert shutdown_hook.main() == 0
    assert calls["n"] == 1
