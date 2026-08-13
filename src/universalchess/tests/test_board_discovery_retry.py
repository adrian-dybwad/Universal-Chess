"""Tests that board discovery keeps re-probing a silent board until it answers.

Why these tests exist:
    ``_discover_board_address(packet=None)`` sent the wake pair (0x4d, 0x4e) and
    one ``DGT_BUS_SEND_87`` exactly once per controller. Every other re-probe
    path in that state machine is reached only from a packet the board sent, so a
    board that is asleep at startup -- or that is woken by its power button after
    the app is already up -- was never probed again. ``self.ready`` stayed False
    for the process lifetime: ``init_board`` gave up after three attempts, and
    the app then polled address 0x00/0x00 forever ("Request timeout for
    DGT_SEND_BATTERY_INFO" once per five seconds) while the board sat there
    awake. The only recovery was restarting the service, after which discovery
    completed in well under a second.

    The fix adds a retry worker that re-sends the probe on a fixed interval
    while the controller is not ready, so a board that wakes late is picked up
    without operator intervention.

How a regression manifests:
    Losing the worker (or the ``self.ready`` guard on its loop) is caught
    directly: ``test_discovery_reprobes_while_board_stays_silent`` records a
    single probe instead of the expected three, and
    ``test_discovery_retry_stops_once_board_is_ready`` records a second probe
    against a live board -- which would reset ``addr1``/``addr2`` mid-session and
    break an already-working board. ``test_discovery_retry_stops_when_signalled``
    failing means cleanup can no longer stop the worker, leaving a thread writing
    to a closed port.
"""

import threading

import pytest

pytest.importorskip("serial")

from universalchess.board.sync_centaur import SyncCentaur

# The board answers a probe in well under a second when awake, so the interval
# only bounds how long a late wake goes unnoticed. Tests drive a tiny interval
# instead of this value to stay fast and deterministic.
TEST_RETRY_SECONDS = 0.01

# Discovery probe, in write order: the two wake bytes then the bus-address
# request. Kept here so a test failure names the protocol, not magic numbers.
WAKE_BYTES = (b"\x4d", b"\x4e")
BUS_ADDRESS_REQUEST = 0x87

# A half-discovered address pair: the board answered the first 0x87 and then went
# silent before confirming. Matches the real addresses this board reports
# (addr1=0x6, addr2=0x50) so the reset assertion cannot pass by coincidence.
PARTIAL_ADDR1 = 0x06
PARTIAL_ADDR2 = 0x50


def _make_controller() -> SyncCentaur:
    """Build a controller without touching hardware.

    auto_init=False skips the background thread that opens the board port,
    leaving a bare instance whose discovery retry worker can be driven
    synchronously against a fake port.
    """
    controller = SyncCentaur(developer_mode=False, auto_init=False)
    controller.serial_debug = False
    controller.discovery_retry_seconds = TEST_RETRY_SECONDS
    return controller


class _SleepingBoardSerial:
    """Fake port that records writes and never answers, like a sleeping board.

    ``on_probe`` runs after each completed probe (the 0x87 write) so a test can
    end the otherwise unbounded retry loop, or flip the controller ready, at a
    known point in the sequence.
    """

    def __init__(self, on_probe=None):
        self.is_open = True
        self.writes = []
        self.probe_count = 0
        self._on_probe = on_probe

    @property
    def in_waiting(self):
        return 0

    def write(self, data):
        payload = bytes(data)
        self.writes.append(payload)
        if payload and payload[0] == BUS_ADDRESS_REQUEST:
            self.probe_count += 1
            if self._on_probe is not None:
                self._on_probe()
        return len(payload)

    def read(self, size):
        return b""

    def close(self):
        self.is_open = False

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


def _split_into_probes(writes):
    """Group recorded writes into whole discovery probes.

    Returns one list per probe. Grouping (rather than counting 0x87 writes)
    proves the wake bytes are re-sent too: a retry that skipped them would leave
    a sleeping board unwoken, so the probe would be as useless as no probe.
    """
    probes = []
    for index in range(0, len(writes), 3):
        probes.append(writes[index:index + 3])
    return probes


def test_discovery_reprobes_while_board_stays_silent():
    """A board that never answers must be probed again, not probed once.

    Failure mode this guards: the pre-fix one-shot probe records exactly one
    probe here, so a board woken after startup is never discovered.
    """
    controller = _make_controller()
    expected_probes = 3
    port = _SleepingBoardSerial(
        on_probe=lambda: (
            controller._discovery_stop.set()
            if port.probe_count >= expected_probes
            else None
        )
    )
    controller.ser = port

    controller._discovery_retry_worker()

    probes = _split_into_probes(port.writes)
    # Count proves re-probing happened; the per-probe shape proves each retry is
    # a complete, usable probe (wake bytes + bus-address request) rather than a
    # bare 0x87 that leaves a sleeping board asleep.
    assert len(probes) == expected_probes
    assert port.probe_count == expected_probes
    for probe in probes:
        assert len(probe) == 3
        assert (probe[0], probe[1]) == WAKE_BYTES
        assert probe[2][0] == BUS_ADDRESS_REQUEST
    # No trailing partial probe: every recorded write belongs to a whole probe.
    assert len(port.writes) == expected_probes * 3


def test_discovery_retry_stops_once_board_is_ready():
    """Once discovery succeeds the worker must stop probing immediately.

    Why: a probe against a ready controller resets ``addr1``/``addr2`` and
    re-runs the handshake on a board that is already working. Failure shows up
    as a second probe recorded after ``ready`` was set.
    """
    controller = _make_controller()
    port = _SleepingBoardSerial()
    controller.ser = port
    # Answering the first probe is what discovery success looks like to the
    # worker; set it from the write hook so the flip happens mid-loop.
    port._on_probe = lambda: setattr(controller, "ready", True)

    controller._discovery_retry_worker()

    assert port.probe_count == 1
    assert controller.ready is True


def test_discovery_retry_stops_when_signalled():
    """A signalled stop (cleanup) must end the loop without probing.

    Why: cleanup closes the serial port, so a worker that keeps probing writes
    to a closed port. Failure manifests as a probe recorded after the stop.
    """
    controller = _make_controller()
    port = _SleepingBoardSerial()
    controller.ser = port
    controller._discovery_stop.set()

    controller._discovery_retry_worker()

    assert port.writes == []
    assert port.probe_count == 0


def test_cleanup_signals_the_discovery_retry_worker():
    """cleanup() must set the stop event so the worker cannot outlive the port.

    Failure means the retry thread survives cleanup and writes to a closed
    serial port after shutdown.
    """
    controller = _make_controller()
    controller.ser = _SleepingBoardSerial()

    controller.cleanup(leds_off=False)

    assert controller._discovery_stop.is_set()


def test_discovery_retry_clears_half_discovered_address():
    """A retry must restart from a clean address pair.

    Why: non-zero ``addr1``/``addr2`` make the state machine treat the next 0x87
    as a confirmation and compare it against the stored pair, taking the
    ``Discovery: ERROR`` branch when it differs. Clearing them first makes the
    retry idempotent no matter how far the previous attempt got. Failure shows
    up as the stale 0x6/0x50 pair surviving the probe.
    """
    controller = _make_controller()
    port = _SleepingBoardSerial()
    controller.ser = port
    controller.addr1 = PARTIAL_ADDR1
    controller.addr2 = PARTIAL_ADDR2
    port._on_probe = controller._discovery_stop.set

    controller._discovery_retry_worker()

    assert port.probe_count == 1
    assert controller.addr1 == 0x00
    assert controller.addr2 == 0x00


def test_run_background_starts_the_retry_worker(monkeypatch):
    """Startup must arm the retry worker, not just send the first probe.

    Why: the worker is what turns a one-shot probe into a durable one. Failure
    means nothing re-probes in production even though the worker exists.
    """
    controller = _make_controller()
    controller.ser = _SleepingBoardSerial()
    started = []

    monkeypatch.setattr(controller, "_initialize", lambda: None)
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *args, **kwargs: _RecordingThread(started, *args, **kwargs),
    )

    controller.run_background()

    assert controller._discovery_retry_worker in started


class _RecordingThread:
    """Records the target of every thread start instead of running it."""

    def __init__(self, started, target=None, name=None, daemon=None, **kwargs):
        self._started = started
        self._target = target
        self.daemon = daemon
        self.name = name

    def start(self):
        self._started.append(self._target)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass
