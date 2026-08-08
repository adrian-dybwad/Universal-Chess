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
