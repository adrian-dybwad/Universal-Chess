"""Tests for the uc-clock-admin root helper (scripts/uc-clock-admin).

This pinned passwordless-sudo helper is the only way the unprivileged service
can touch the device clock: enabling/disabling NTP and stepping the clock to a
caller-supplied epoch. Its security value is entirely in the verb `case` and the
argument validation gating the privileged `timedatectl` calls, so the tests
exercise that boundary in DRY_RUN mode, which records the intended invocation
instead of running it.

The epoch bounds matter beyond input hygiene: the device issues its own TLS
certificates and orders its event log by wall time, so a clock stepped to 1970
or 2999 breaks both. The helper refuses those rather than trusting the caller.

Each test states the regression it guards and how it would surface.
"""

import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-clock-admin"

# Mirror of the helper's own bounds. Kept here as explicit instants so a change
# to either side is a visible, deliberate edit rather than a silent drift.
_EPOCH_2024_01_01 = 1704067200
_EPOCH_2100_01_01 = 4102444800
_EPOCH_IN_RANGE = 1800000000  # 2027-01-15T08:00:00Z


def _run(args, action_log, *, dry_run="1"):
    env = dict(os.environ)
    env["UC_CLOCK_ADMIN_DRY_RUN"] = dry_run
    env["UC_CLOCK_ADMIN_ACTION_LOG"] = str(action_log)
    argv = ["bash", str(_HELPER), *args]
    # Fixed argv (no shell) running the repo's own helper under bash; test-only.
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    lines = action_log.read_text().splitlines() if action_log.exists() else []
    return proc, lines


@pytest.mark.parametrize("state,expected", [("on", "true"), ("off", "false")])
def test_ntp_verb_invokes_timedatectl_set_ntp(tmp_path, state, expected):
    """`ntp on|off` maps to `timedatectl set-ntp true|false` and exits 0.

    Guards the happy path and the on/off mapping specifically: inverting it would
    silently disable time sync when the user asked to enable it, which looks like
    the toggle "not sticking" rather than an error. Manifests as the wrong
    boolean in the action log.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["ntp", state], log)
    assert proc.returncode == 0
    assert lines == [f"timedatectl set-ntp {expected}"]


@pytest.mark.parametrize("bad", ["true", "1", "yes", "", "on off", "on; rm -rf /"])
def test_ntp_verb_rejects_anything_but_on_or_off(tmp_path, bad):
    """Only the exact tokens `on` and `off` are accepted (exit 2), no call.

    This is half the injection boundary for the sudo grant: accepting a
    free-form string would pass caller-controlled text into the privileged
    argv. Manifests as a non-empty action log or a zero exit for a bad token.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["ntp", bad], log)
    assert proc.returncode == 2
    assert lines == []


def test_set_epoch_invokes_timedatectl_with_an_at_prefixed_timestamp(tmp_path):
    """A valid epoch runs `timedatectl set-time @<epoch>` and exits 0.

    The `@` prefix is what makes systemd read the argument as seconds since the
    epoch rather than a local-time string; dropping it would make the applied
    time depend on the device timezone. Manifests as a missing `@` in the log.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["set-epoch", str(_EPOCH_IN_RANGE)], log)
    assert proc.returncode == 0
    assert lines == [f"timedatectl set-time @{_EPOCH_IN_RANGE}"]


@pytest.mark.parametrize("boundary,expected_exit", [
    (_EPOCH_2024_01_01, 0),          # inclusive lower bound
    (_EPOCH_2024_01_01 - 1, 3),      # one second below
    (_EPOCH_2100_01_01, 0),          # inclusive upper bound
    (_EPOCH_2100_01_01 + 1, 3),      # one second above
])
def test_set_epoch_bounds_are_inclusive_and_reject_one_second_outside(
    tmp_path, boundary, expected_exit
):
    """The accepted range is closed at both ends, to the second.

    Chosen to land exactly on and one second past each bound, so an off-by-one
    in the comparison (`-lt` vs `-le`) is caught rather than hidden by a value
    far from the edge. A rejected value must make no privileged call at all;
    manifests as an action log entry for an out-of-range epoch.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["set-epoch", str(boundary)], log)
    assert proc.returncode == expected_exit
    assert lines == ([f"timedatectl set-time @{boundary}"] if expected_exit == 0 else [])


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "-1",                     # negative
    "17000000.5",             # fractional (milliseconds passed through by mistake)
    "1700000000; rm -rf /",   # shell metacharacters
    "1700000000 x",           # trailing token
    "notanumber",
    "0x65545440",             # hex
    "1" * 40,                 # absurd length, guards against integer overflow
])
def test_set_epoch_rejects_non_integer_input_before_any_call(tmp_path, bad):
    """Non-integer epochs are rejected (exit 2) before touching timedatectl.

    The other half of the injection boundary. A fractional value is included
    because the browser reports milliseconds, and a caller dividing by 1000
    without rounding is the most likely way a malformed value arrives here.
    Manifests as a non-empty action log or a zero exit.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(["set-epoch", bad], log)
    assert proc.returncode == 2
    assert lines == []


@pytest.mark.parametrize("args", [
    [],                            # no verb
    ["ntp"],                       # verb without its argument
    ["set-epoch"],                 # verb without its argument
    ["reboot"],                    # unknown verb
    ["ntp", "on", "extra"],        # extra argument
    ["set-time", "now"],           # near-miss on the real verb name
])
def test_unknown_or_malformed_invocations_are_usage_errors(tmp_path, args):
    """Anything outside the two supported verbs is a usage error (exit 2).

    The verb `case` is the security boundary for the sudo grant: a passthrough
    or catch-all branch would turn the grant into arbitrary root command
    execution. Manifests as a zero exit or an action log for an unknown verb.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(args, log)
    assert proc.returncode == 2
    assert lines == []


def test_postinst_installs_a_sudo_grant_for_this_helper():
    """The package must grant passwordless sudo to exactly this helper path.

    Without the grant nothing about the feature errors: every privileged call
    returns "not applied" and the Settings toggle and clock-set action look like
    they simply do not stick, which is far harder to diagnose than a failure.
    The path is asserted whole because a grant for a different or misspelled path
    is equivalent to no grant at all.
    """
    postinst = (
        Path(__file__).resolve().parents[3]
        / "packaging" / "deb-root" / "DEBIAN" / "postinst"
    )
    text = postinst.read_text(encoding="utf-8")
    assert 'CLOCK_ADMIN_HELPER="${DGTCM_PATH}/scripts/uc-clock-admin"' in text
    assert 'NOPASSWD: $CLOCK_ADMIN_HELPER' in text
