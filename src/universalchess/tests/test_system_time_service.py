"""Tests for services/system_time_service.py.

The service owns the device's wall clock: reading whether network time sync is
enabled and actually synchronised, turning it on or off, and stepping the clock
to a caller-supplied epoch. Everything privileged goes through the pinned
uc-clock-admin sudo helper, so the command runner is injected and the argv it
receives is asserted -- that argv is the security-relevant output of this module.

The read path deliberately reports "unknown" rather than a plausible-looking
default when timedatectl cannot be consulted (a dev box, a non-systemd host).
Reporting False for "NTP enabled" on a board where it is actually on would make
the Settings toggle lie and would let the UI offer a manual clock set that
timedatectl is going to refuse.
"""

import subprocess

import pytest

from universalchess.services import system_time_service as sts

_HELPER = "/opt/universalchess/scripts/uc-clock-admin"
_EPOCH_IN_RANGE = 1800000000  # 2027-01-15T08:00:00Z


class _Result:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(result):
    """A runner that records every argv it is handed and returns ``result``."""
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        return result

    return run, calls


def _raising_runner(exc):
    def run(args, timeout):
        raise exc

    return run


def _recording_raising_runner(exc):
    """A runner that records each attempt before raising, to count failed reads."""
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        raise exc

    return run, calls


@pytest.fixture(autouse=True)
def clear_cached_status():
    """Drop the memoised sync flags around every test.

    The module caches the timedatectl read behind a short window, so without a
    reset the flags one test primed would be served to the next and the runner
    it injected would never be called -- tests would pass or fail depending on
    the order they ran in.
    """
    sts.invalidate_status_cache()
    yield
    sts.invalidate_status_cache()


def _frozen_clock(start=0.0):
    """A settable monotonic stand-in: returns `clock.value`, which tests advance."""

    class Clock:
        value = start

        def __call__(self):
            return self.value

    return Clock()


def test_status_parses_timedatectl_properties_into_booleans():
    """`NTP=yes` / `NTPSynchronized=no` become enabled=True, synchronised=False.

    These are two different facts and the UI shows them differently: enabled is
    the toggle's position, synchronised is whether the board has actually reached
    a time server. Collapsing them would tell a user with no internet that their
    clock is fine. Manifests as the wrong pair here.
    """
    run, _ = _recording_runner(_Result(stdout="NTP=yes\nNTPSynchronized=no\n"))
    status = sts.get_status(run=run)
    assert status.ntp_enabled is True
    assert status.ntp_synchronised is False


def test_status_reads_properties_by_name_not_by_output_order():
    """Properties are matched by key, so timedatectl may emit them in any order.

    `timedatectl show` makes no ordering guarantee across versions. Positional
    parsing would silently swap the two flags on a version that reorders them --
    the failure would look like a UI bug, not a parsing bug.
    """
    run, calls = _recording_runner(_Result(stdout="NTPSynchronized=yes\nNTP=no\n"))
    status = sts.get_status(run=run)
    assert status.ntp_enabled is False
    assert status.ntp_synchronised is True
    # Reading state needs no privileges, so it must not go through sudo.
    assert calls[0][0] != "sudo"


@pytest.mark.parametrize("scenario,runner", [
    ("timedatectl missing", _raising_runner(FileNotFoundError("timedatectl"))),
    ("timedatectl timed out", _raising_runner(subprocess.TimeoutExpired("timedatectl", 5))),
])
def test_status_reports_unknown_when_timedatectl_cannot_be_consulted(scenario, runner):
    """An unreadable clock state is None, never a fabricated False.

    Defaulting to False would render the Settings toggle in the "off" position
    on a board where sync is on, and would invite a manual clock set that
    timedatectl then refuses. None lets the UI say it does not know.
    """
    status = sts.get_status(run=runner)
    assert status.ntp_enabled is None
    assert status.ntp_synchronised is None


@pytest.mark.parametrize("stdout", ["", "NTP=maybe\n", "garbage without an equals sign\n"])
def test_status_reports_unknown_for_unparseable_output(stdout):
    """Output that is empty or not yes/no yields None for the affected flag.

    Same reasoning as an absent timedatectl: guessing is worse than admitting
    ignorance. Manifests as a bogus True/False derived from junk.
    """
    run, _ = _recording_runner(_Result(stdout=stdout))
    status = sts.get_status(run=run)
    assert status.ntp_enabled is None


def test_status_reports_a_nonzero_exit_as_unknown():
    """A failed timedatectl invocation is unknown, not "disabled".

    Guards against reading stdout regardless of the exit status, which on a
    partial failure could parse a stale or truncated property block.
    """
    run, _ = _recording_runner(_Result(returncode=1, stdout="NTP=yes\n"))
    status = sts.get_status(run=run)
    assert status.ntp_enabled is None


def test_status_reports_the_device_epoch():
    """The status carries the device's own wall clock reading.

    This is what surfaces a wrong board clock in the UI at all -- the reported
    symptom went unnoticed precisely because nothing displayed it. Manifests as
    a missing or zero epoch.
    """
    run, _ = _recording_runner(_Result(stdout="NTP=yes\nNTPSynchronized=yes\n"))
    status = sts.get_status(run=run, now=lambda: 1800000000.25)
    assert status.epoch_seconds == 1800000000.25


# -- caching the sync flags ---------------------------------------------------
#
# The flags are read by GET /api/settings (unauthenticated, on every Settings
# page load) and by the board on every rebuild of its System menu. Each read used
# to fork a timedatectl subprocess, which on a Pi Zero is the most expensive
# thing either path does and, on the unauthenticated endpoint, is a fork any LAN
# client could ask for at will. These tests pin the window that collapses them.


def test_repeated_reads_consult_timedatectl_once_within_the_cache_window():
    """A second read inside the window is served from the first, not re-run.

    Why: this is the entire point of the cache -- an unauthenticated endpoint
    must not fork a subprocess per request. How a regression manifests: the
    runner records two invocations, meaning every caller pays for a fork again.
    """
    run, calls = _recording_runner(_Result(stdout="NTP=yes\nNTPSynchronized=no\n"))
    clock = _frozen_clock()

    first = sts.get_status(run=run, monotonic=clock)
    second = sts.get_status(run=run, monotonic=clock)

    assert len(calls) == 1
    # The cached answer must be the same answer, not merely "an" answer.
    assert (second.ntp_enabled, second.ntp_synchronised) == (True, False)
    assert (first.ntp_enabled, first.ntp_synchronised) == (True, False)


def test_the_clock_reading_is_taken_live_even_when_the_flags_are_cached():
    """Only the flags are cached; the epoch is read on every call.

    Why: the epoch is the number the Device Clock card exists to display, and
    the offset it derives from it. Caching it would freeze the board's reported
    time for the length of the window, so a card refreshed twice would show the
    same instant twice and the offset would be wrong by the gap. How a
    regression manifests: both reads report an identical epoch below.
    """
    run, _ = _recording_runner(_Result(stdout="NTP=yes\n"))
    clock = _frozen_clock()
    ticks = iter([1800000000.0, 1800000001.5])

    first = sts.get_status(run=run, now=lambda: next(ticks), monotonic=clock)
    second = sts.get_status(run=run, now=lambda: next(ticks), monotonic=clock)

    assert first.epoch_seconds == 1800000000.0
    assert second.epoch_seconds == 1800000001.5


def test_the_state_is_read_again_once_the_window_has_elapsed():
    """The cache expires, so a state changed outside the UI is picked up.

    Why: sync can be switched on elsewhere, and systemd flips NTPSynchronized on
    its own once it reaches a server -- the transition the card is there to show.
    A cache that never expired would report the old answer indefinitely. How a
    regression manifests: the second read is served from the cache and reports
    the stale flags.
    """
    stale, calls = _recording_runner(_Result(stdout="NTP=yes\nNTPSynchronized=no\n"))
    clock = _frozen_clock()
    sts.get_status(run=stale, monotonic=clock)

    clock.value += sts.STATUS_CACHE_TTL_SECONDS
    fresh, _ = _recording_runner(_Result(stdout="NTP=yes\nNTPSynchronized=yes\n"))
    reread = sts.get_status(run=fresh, monotonic=clock)

    assert len(calls) == 1  # the first runner was not consulted a second time
    assert reread.ntp_synchronised is True


def test_the_window_is_measured_on_the_monotonic_clock():
    """Stepping the wall clock neither expires nor extends the window.

    Why: this module steps the wall clock itself. Timing the cache on time.time
    would mean a set_clock that jumped the device forward instantly expired it,
    and one that jumped it back froze the cached flags until the wall clock
    caught up -- which for a board set from 1970 is decades. How a regression
    manifests: the read below is repeated because a wall-clock jump was mistaken
    for the window elapsing.
    """
    run, calls = _recording_runner(_Result(stdout="NTP=yes\n"))
    clock = _frozen_clock()
    a_year = 365 * 24 * 60 * 60

    sts.get_status(run=run, now=lambda: 1800000000.0, monotonic=clock)
    sts.get_status(run=run, now=lambda: 1800000000.0 + a_year, monotonic=clock)
    sts.get_status(run=run, now=lambda: 1800000000.0 - a_year, monotonic=clock)

    assert len(calls) == 1


def test_an_unreadable_state_is_cached_like_any_other():
    """A failed read is remembered for the window rather than retried per call.

    Why: on a host without timedatectl every read fails, and that is exactly the
    case where retrying per request costs a doomed fork each time -- the worst
    version of the cost the cache exists to remove. How a regression manifests:
    two attempts recorded below.
    """
    run, calls = _recording_raising_runner(FileNotFoundError("timedatectl"))
    clock = _frozen_clock()

    first = sts.get_status(run=run, monotonic=clock)
    second = sts.get_status(run=run, monotonic=clock)

    assert len(calls) == 1
    assert first.ntp_enabled is None
    assert second.ntp_enabled is None


def test_a_caller_can_demand_a_fresh_read_and_that_read_refills_the_cache():
    """use_cache=False re-runs timedatectl and stores what it found.

    Why: stepping the clock is refused unless sync is known to be off, and that
    decision must not rest on a reading up to a window old -- sync could have
    been switched on in between. Refilling matters too: a fresh read that left
    the old value in place would serve the stale answer to the next caller. How
    a regression manifests: no second invocation, or the third read reporting
    the superseded flags.
    """
    stale, _ = _recording_runner(_Result(stdout="NTP=no\n"))
    clock = _frozen_clock()
    sts.get_status(run=stale, monotonic=clock)

    fresh, fresh_calls = _recording_runner(_Result(stdout="NTP=yes\n"))
    demanded = sts.get_status(run=fresh, monotonic=clock, use_cache=False)
    assert len(fresh_calls) == 1
    assert demanded.ntp_enabled is True

    # Served from the refilled cache: same answer, still only one invocation.
    subsequent = sts.get_status(run=fresh, monotonic=clock)
    assert len(fresh_calls) == 1
    assert subsequent.ntp_enabled is True


@pytest.mark.parametrize("enabled", [True, False])
def test_turning_sync_on_or_off_drops_the_cached_state(enabled):
    """A write invalidates, so the next read reflects what was just applied.

    Why: both surfaces re-read immediately after the toggle moves -- the web on
    the settings_changed refresh, the board on its next menu build -- and both
    would otherwise be served the pre-toggle value and snap the switch back to
    where it was. How a regression manifests: the read after the write reports
    the old flag despite the apply succeeding.
    """
    before, _ = _recording_runner(_Result(stdout=f"NTP={'no' if enabled else 'yes'}\n"))
    clock = _frozen_clock()
    sts.get_status(run=before, monotonic=clock)

    applier, _ = _recording_runner(_Result(returncode=0))
    sts.set_ntp_enabled(enabled, helper_path=_HELPER, run=applier)

    after, after_calls = _recording_runner(_Result(stdout=f"NTP={'yes' if enabled else 'no'}\n"))
    assert sts.get_status(run=after, monotonic=clock).ntp_enabled is enabled
    assert len(after_calls) == 1


def test_a_failed_apply_also_drops_the_cached_state():
    """Invalidation does not depend on the helper reporting success.

    Why: an apply that returns non-zero may still have changed the state (the
    helper can fail after timedatectl succeeded), so trusting the failure and
    keeping the cache would show a state the device no longer has. Re-reading
    costs one subprocess on a path the user just triggered by hand. How a
    regression manifests: no invocation recorded on the read after the failure.
    """
    seed, _ = _recording_runner(_Result(stdout="NTP=no\n"))
    clock = _frozen_clock()
    sts.get_status(run=seed, monotonic=clock)

    failing, _ = _recording_runner(_Result(returncode=1, stderr="denied"))
    assert sts.set_ntp_enabled(True, helper_path=_HELPER, run=failing) is False

    after, after_calls = _recording_runner(_Result(stdout="NTP=yes\n"))
    assert sts.get_status(run=after, monotonic=clock).ntp_enabled is True
    assert len(after_calls) == 1


@pytest.mark.parametrize("enabled,expected_state", [(True, "on"), (False, "off")])
def test_set_ntp_enabled_invokes_the_pinned_helper_through_sudo(enabled, expected_state):
    """Enabling/disabling runs `sudo -n <helper> ntp on|off` and reports applied.

    The exact argv is the whole security surface of the sudo grant, so it is
    asserted in full. `-n` matters: without it a missing grant hangs on a
    password prompt instead of failing fast.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    applied = sts.set_ntp_enabled(enabled, helper_path=_HELPER, run=run)
    assert applied is True
    assert calls == [["sudo", "-n", _HELPER, "ntp", expected_state]]


@pytest.mark.parametrize("scenario,result_or_exc", [
    ("helper exits nonzero", _Result(returncode=1, stderr="denied")),
    ("sudo grant missing", FileNotFoundError("sudo")),
])
def test_set_ntp_enabled_reports_not_applied_rather_than_raising(scenario, result_or_exc):
    """A failed apply returns False so the caller can say "not applied".

    Mirrors timezone_service: a missing sudo grant on a hand-installed board is
    an expected condition, not a 500. Manifests as an exception escaping to the
    request handler.
    """
    if isinstance(result_or_exc, Exception):
        run = _raising_runner(result_or_exc)
    else:
        run, _ = _recording_runner(result_or_exc)
    assert sts.set_ntp_enabled(True, helper_path=_HELPER, run=run) is False


def test_set_clock_invokes_the_pinned_helper_with_whole_seconds():
    """A valid epoch runs `sudo -n <helper> set-epoch <seconds>`.

    Guards the argv and the integer conversion: the helper refuses anything that
    is not a plain integer, so passing a float through would fail on the board
    while passing here.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    applied = sts.set_clock(
        float(_EPOCH_IN_RANGE), helper_path=_HELPER, run=run, ntp_enabled=False
    )
    assert applied is True
    assert calls == [["sudo", "-n", _HELPER, "set-epoch", str(_EPOCH_IN_RANGE)]]


def test_set_clock_rounds_to_the_nearest_second():
    """A fractional epoch is rounded, not truncated.

    The browser reports milliseconds, so the value arriving here is almost always
    fractional. Truncating would bias the board's clock up to a second slow on
    every set; the helper would reject the raw float outright.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    sts.set_clock(_EPOCH_IN_RANGE + 0.75, helper_path=_HELPER, run=run, ntp_enabled=False)
    assert calls == [["sudo", "-n", _HELPER, "set-epoch", str(_EPOCH_IN_RANGE + 1)]]


def test_set_clock_refuses_while_network_time_sync_is_enabled():
    """With NTP on, the call raises and makes no privileged invocation.

    timedatectl refuses to step a clock it is synchronising, so attempting it
    would surface as an opaque helper failure. Raising a distinct error lets the
    endpoint tell the user to turn sync off first. Manifests as a helper call
    being made, or as a generic failure the UI cannot explain.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    with pytest.raises(sts.NetworkTimeSyncEnabledError):
        sts.set_clock(float(_EPOCH_IN_RANGE), helper_path=_HELPER, run=run, ntp_enabled=True)
    assert calls == []


def test_set_clock_refuses_when_the_sync_state_is_unknown():
    """An unknown NTP state is treated as "may be on" and refuses the step.

    The safe reading of "I could not determine the state" is not "it is off".
    Proceeding would produce a confusing helper failure on a board where sync is
    actually running.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    with pytest.raises(sts.NetworkTimeSyncEnabledError):
        sts.set_clock(float(_EPOCH_IN_RANGE), helper_path=_HELPER, run=run, ntp_enabled=None)
    assert calls == []


@pytest.mark.parametrize("bad_epoch", [
    sts.EPOCH_MIN_SECONDS - 1,
    sts.EPOCH_MAX_SECONDS + 1,
    0,
    -1,
    float("nan"),
    float("inf"),
])
def test_set_clock_rejects_epochs_outside_the_supported_range(bad_epoch):
    """Out-of-range or non-finite epochs raise ValueError before any call.

    The endpoint turns this into a 400. The range exists because the device
    issues its own TLS certificates and orders its event log by wall time; NaN
    and infinity are included because they survive JSON parsing and would
    otherwise reach int() and raise something the route cannot classify.
    """
    run, calls = _recording_runner(_Result(returncode=0))
    with pytest.raises(ValueError):
        sts.set_clock(bad_epoch, helper_path=_HELPER, run=run, ntp_enabled=False)
    assert calls == []


def test_python_and_helper_agree_on_the_accepted_epoch_range():
    """The service's bounds match the ones the uc-clock-admin helper enforces.

    Both sides validate independently (defence in depth), which means they can
    drift: a widened Python bound would start producing requests the helper
    rejects with an opaque exit 3, and a narrowed one would reject values the
    board would have accepted. This pins them together so a change to either is
    a deliberate change to both.
    """
    from pathlib import Path

    helper = Path(sts.__file__).resolve().parents[1] / "scripts" / "uc-clock-admin"
    text = helper.read_text(encoding="utf-8")
    assert f"EPOCH_MIN={sts.EPOCH_MIN_SECONDS}" in text
    assert f"EPOCH_MAX={sts.EPOCH_MAX_SECONDS}" in text
