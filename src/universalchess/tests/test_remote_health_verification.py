"""Tests for scripts/lib/remote-restart-and-verify.sh -- post-deploy health.

Why these tests exist:
    A deploy to a board reported "Deploy complete", both units active and "no
    errors in recent log", while the web interface was in fact crashing at
    import. The verification it replaced did this:

        systemctl restart <units> && sleep 3 && systemctl is-active <units>
            && journalctl -n 20 | grep -iE 'error|traceback|exception'

    Three independent reasons that could not detect the failure:

    1. ``sleep 3`` is far shorter than startup on the board's single ARMv6 core
       (importing the Flask app takes roughly 70 seconds). The check ran, and
       passed, while the app was still importing -- the crash came at +78s.
    2. ``is-active`` says ``active`` moments after every crash, because the units
       set ``Restart=always``. A crash loop is indistinguishable from health
       unless the automatic-restart count is compared.
    3. ``journalctl -n 20`` was read before the traceback existed, and the
       traceback goes to the unit's log file rather than the journal anyway.

    Verification must therefore wait for a positive readiness signal from the
    application itself, and treat any automatic restart during the wait as a
    failure.

How a regression manifests:
    Reintroducing a fixed short sleep or an ``is-active``-only check makes
    test_crash_after_start_is_reported_for_the_web_unit and
    test_verification_waits_for_readiness_rather_than_probing_once pass a
    crash-looping board, and a deploy of broken code exits 0 again.

The four external commands the script drives -- ``sudo``, ``systemctl``,
``curl`` and ``journalctl`` -- are replaced with fakes on PATH. That is the
boundary between this script and the system it inspects, so the script runs
unmodified and each assertion is about its real behavior. The fakes accept
scripted value sequences (one value consumed per call, the last repeating) so a
unit can report ``active`` and then a bumped restart count, reproducing the
observed incident exactly.
"""

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "lib" / "remote-restart-and-verify.sh"
)

_BOARD_UNIT = "universal-chess"
_WEB_UNIT = "universal-chess-web"

# The port the web unit binds and nginx proxies to (ExecStart --port=5000).
_WEB_PORT = "5000"

# Documented exit codes. Distinct rather than a generic 1 so a caller -- and an
# operator reading a failed deploy -- can tell "never came up" from "came up and
# then crashed", which have different causes and different next steps.
_EXIT_OK = 0
_EXIT_UNIT_NOT_ACTIVE = 2
_EXIT_UNIT_RESTARTED = 3
_EXIT_NEVER_READY = 4

# A timeout of 0 still performs one probe, then fails: the loop probes before
# testing its deadline. Used to make the negative cases instant and independent
# of wall-clock timing.
_SINGLE_PROBE = "0"

# Long enough that the deadline is never reached during a test; those cases end
# on a readiness signal instead, and `sleep` is stubbed so no real time passes.
_AMPLE_TIMEOUT = "600"

# Text the fake journalctl emits. Contains no words the old grep looked for
# ('error', 'traceback', 'exception'), so a test asserting it reaches the
# operator proves the diagnostics are forwarded verbatim rather than matched.
_JOURNAL_LINE = "fake-journal: main process exited, code=exited, status=1/FAILURE"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def verify(tmp_path):
    """Run the real script against fakes for sudo/systemctl/curl/journalctl.

    Returns a callable accepting the simulated system's behavior and yielding the
    completed process plus the recorded invocations of systemctl and curl.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Consume one whitespace-separated value per call, repeating the last one.
    # This is what lets a unit report `active` on one call and a bumped restart
    # count on the next, which is how the real crash presented.
    pop_value = r"""
_pop() {
	local file="${FAKE_STATE_DIR}/$1" fallback="$2" values
	[[ -f $file ]] || { printf '%s' "$fallback"; return; }
	read -r -a values < "$file"
	(( ${#values[@]} )) || { printf '%s' "$fallback"; return; }
	printf '%s' "${values[0]}"
	(( ${#values[@]} > 1 )) && printf '%s\n' "${values[*]:1}" > "$file"
	return 0
}
"""

    _write_executable(bin_dir / "sudo", 'exec "$@"\n')
    _write_executable(
        bin_dir / "systemctl",
        pop_value
        + r"""
printf '%s\n' "$*" >> "${FAKE_STATE_DIR}/systemctl.calls"
case "$1" in
	restart) exit 0 ;;
	is-active)
		state="$(_pop "$2.state" active)"
		printf '%s\n' "$state"
		[[ $state == active ]] && exit 0 || exit 3 ;;
	show)
		unit="${!#}"
		printf '%s\n' "$(_pop "${unit}.restarts" 0)" ;;
	*) exit 64 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        r"""
printf '%s\n' "$*" >> "${FAKE_STATE_DIR}/curl.calls"
attempts=$(( $(wc -l < "${FAKE_STATE_DIR}/curl.calls") ))
ready_after=$(cat "${FAKE_STATE_DIR}/curl.ready_after" 2>/dev/null || echo 1)
[[ $ready_after != never && $attempts -ge $ready_after ]] && exit 0
exit 7
""",
    )
    _write_executable(bin_dir / "journalctl", f"printf '%s\\n' {_JOURNAL_LINE!r}\n")
    # Stubbed so a multi-poll test costs no wall-clock time. The script's own
    # deadline is wall-clock based, so the negative cases pass a 0 timeout
    # instead of relying on this.
    _write_executable(bin_dir / "sleep", "exit 0\n")

    def run(*args, states=None, restarts=None, ready_after=1):
        for unit, sequence in (states or {}).items():
            (state_dir / f"{unit}.state").write_text(sequence)
        for unit, sequence in (restarts or {}).items():
            (state_dir / f"{unit}.restarts").write_text(sequence)
        (state_dir / "curl.ready_after").write_text(str(ready_after))
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["FAKE_STATE_DIR"] = str(state_dir)
        argv = args or (_BOARD_UNIT, _WEB_UNIT, _WEB_PORT, _AMPLE_TIMEOUT)
        proc = subprocess.run(  # noqa: S603 - test invokes the repo's own script
            ["bash", str(_SCRIPT), *argv],  # noqa: S607
            env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60,
        )
        return proc, _read_calls(state_dir / "systemctl.calls"), _read_calls(
            state_dir / "curl.calls"
        )

    return run


def _read_calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


class TestHealthyDeploy:
    """A board that comes up must be reported healthy, promptly."""

    def test_ready_web_interface_exits_zero(self, verify):
        # Guards against the fix over-correcting into always failing. Regression:
        # a healthy deploy starts reporting a failure and blocks all deploys.
        proc, _, _ = verify()
        assert proc.returncode == _EXIT_OK, (proc.stdout, proc.stderr)

    def test_both_units_are_restarted_with_elevation(self, verify):
        # The whole point of the remote step is applying the synced code. The
        # restart must cover both units: web/template changes only take effect
        # when the web unit restarts. Regression: a unit is dropped and the
        # deploy silently keeps running old code in that process.
        _, systemctl_calls, _ = verify()
        restarts = [c for c in systemctl_calls if c.startswith("restart ")]
        assert len(restarts) == 1, systemctl_calls
        assert _BOARD_UNIT in restarts[0] and _WEB_UNIT in restarts[0], restarts

    def test_readiness_is_probed_on_the_local_web_port(self, verify):
        # The app binds 127.0.0.1:5000 behind nginx; probing the public host
        # would test nginx (which answers 502 while the app imports) instead of
        # the app. Regression: the probe targets the wrong port or host and
        # readiness is never observed, or is observed when the app is down.
        _, _, curl_calls = verify()
        assert curl_calls, "readiness was never probed"
        assert f"127.0.0.1:{_WEB_PORT}" in curl_calls[0], curl_calls

    def test_verification_waits_for_readiness_rather_than_probing_once(self, verify):
        # The core defect: a single early check passes while the app is still
        # importing (~70s on ARMv6). Readiness only appears on the third probe
        # here, so a script that probes once and reports success fails this.
        proc, _, curl_calls = verify(ready_after=3)
        assert proc.returncode == _EXIT_OK, (proc.stdout, proc.stderr)
        assert len(curl_calls) == 3, curl_calls


class TestCrashAfterStartIsDetected:
    """``Restart=always`` makes a crash loop look active; count the restarts."""

    def test_crash_after_start_is_reported_for_the_web_unit(self, verify):
        # The observed incident: the unit reports active throughout, but the app
        # exits 1 at import and systemd restarts it, bumping NRestarts 0 -> 1.
        # Regression (an is-active-only check): exit 0 on a crash-looping board,
        # which is the false "Deploy complete" this script exists to prevent.
        proc, _, _ = verify(restarts={_WEB_UNIT: "0 1"})
        assert proc.returncode == _EXIT_UNIT_RESTARTED, (proc.returncode, proc.stdout)
        assert _WEB_UNIT in proc.stdout + proc.stderr

    def test_crash_of_the_board_unit_is_not_masked_by_a_ready_web(self, verify):
        # The readiness probe only covers the web process. A board controller
        # crash-looping behind a perfectly healthy web interface must still fail
        # the deploy. Regression: readiness short-circuits the restart-count
        # check and board-side breakage ships silently.
        proc, _, _ = verify(restarts={_BOARD_UNIT: "0 2"})
        assert proc.returncode == _EXIT_UNIT_RESTARTED, (proc.returncode, proc.stdout)
        assert _BOARD_UNIT in proc.stdout + proc.stderr

    def test_crash_report_includes_log_diagnostics(self, verify):
        # An operator needs the traceback, not just a verdict; the previous check
        # grepped the journal before the traceback existed. Regression: the
        # failure is announced with no supporting log output and every failed
        # deploy needs a manual round trip to the board.
        proc, _, _ = verify(restarts={_WEB_UNIT: "0 1"})
        assert _JOURNAL_LINE in proc.stdout + proc.stderr


class TestUnitFailureIsDetected:
    """A unit that is not running must fail the deploy."""

    def test_inactive_unit_exits_non_zero(self, verify):
        # A unit that gave up entirely (Restart=always exhausted, or start-limit
        # hit) reports failed. Regression: the deploy reports success for a board
        # with no running software at all.
        proc, _, _ = verify(states={_WEB_UNIT: "failed"})
        assert proc.returncode == _EXIT_UNIT_NOT_ACTIVE, (proc.returncode, proc.stdout)
        assert _WEB_UNIT in proc.stdout + proc.stderr

    def test_inactive_board_unit_is_detected_too(self, verify):
        # The old check discarded the first unit's is-active status (only the
        # last iteration's status propagated), so a dead board controller passed.
        # Regression: that asymmetry returns and only the web unit is enforced.
        proc, _, _ = verify(states={_BOARD_UNIT: "failed"})
        assert proc.returncode == _EXIT_UNIT_NOT_ACTIVE, (proc.returncode, proc.stdout)
        assert _BOARD_UNIT in proc.stdout + proc.stderr

    def test_never_ready_within_timeout_exits_non_zero(self, verify):
        # An app that stays active but never serves (hung import, port already
        # bound) must not pass. A 0 timeout still probes once, so this asserts
        # the deadline is enforced rather than merely configured.
        proc, _, curl_calls = verify(
            _BOARD_UNIT, _WEB_UNIT, _WEB_PORT, _SINGLE_PROBE, ready_after="never"
        )
        assert proc.returncode == _EXIT_NEVER_READY, (proc.returncode, proc.stdout)
        assert len(curl_calls) == 1, curl_calls

    def test_timeout_report_includes_log_diagnostics(self, verify):
        # Same reasoning as the crash case: a timeout is useless without the log
        # that explains it. Regression: operator gets a bare "not ready".
        proc, _, _ = verify(
            _BOARD_UNIT, _WEB_UNIT, _WEB_PORT, _SINGLE_PROBE, ready_after="never"
        )
        assert _JOURNAL_LINE in proc.stdout + proc.stderr


class TestUsage:
    """Argument handling, since the script is invoked over ssh with positionals."""

    def test_missing_arguments_fail_without_touching_the_board(self, verify):
        # Invoked as `bash -s -- <args>` over ssh, where a dropped argument is
        # easy to miss. Restarting units with a defaulted-empty name, or probing
        # port "", must not happen. Regression: the script proceeds on partial
        # input and the failure surfaces as something unrelated.
        proc, systemctl_calls, _ = verify(_BOARD_UNIT, _WEB_UNIT)
        assert proc.returncode != _EXIT_OK
        assert systemctl_calls == [], systemctl_calls
