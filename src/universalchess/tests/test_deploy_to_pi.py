"""Tests for scripts/deploy-to-pi.sh -- the failure reporting and elevation rules.

Why these tests exist:
    A deploy of this script transferred zero Python files to a board and still
    printed "Deploy complete", reported both units active, and exited 0. The
    stale code then ran for a full debugging session before the discrepancy was
    noticed via a benchmark. Two independent defects produced that:

    1. The install tree is root-owned. ``postinst`` runs
       ``chown -R root:root ${DGTCM_PATH}`` deliberately -- the sudoers grants
       are path literals under ``/opt/universalchess/scripts/``, so a service
       user able to rewrite those files could trivially escalate to root. The
       script's rsync ran unelevated and every write was denied.
    2. The transfer's exit status was discarded. ``rsync ... | grep -vE ... ||
       true`` forced a zero status and merged stderr into a filter that removed
       the diagnostics, so a total failure was indistinguishable from success.

    A third defect is latent and was actually triggered by the manual
    ``sudo rsync -a`` workaround used to recover: ``-a`` implies ``-o -g``, and
    a root receiver honours them, so it stamped the *sender's* numeric ownership
    (uid 501 / gid ``staff`` from macOS) onto the whole tree, destroying the
    root:root ownership that the sudoers path grants depend on.

How a regression manifests:
    - Restoring ``|| true`` on the transfer: the failure tests below see exit 0
      and "Deploy complete" in stdout.
    - Dropping ``--rsync-path``: test_transfer_elevates_on_the_receiver finds no
      elevation in the recorded argv, and real deploys silently stop landing.
    - Reintroducing ``-a``: test_transfer_does_not_stamp_sender_ownership finds
      ``-a`` (or a bare ``-o``/``-g``) and real deploys corrupt tree ownership.

The two external processes the script drives -- ``rsync`` and ``ssh`` -- are
replaced with recording shims on PATH. That is the boundary between this script
and what it does not control, so the script itself runs unmodified and every
assertion is about its real behavior.

Post-restart health verification lives in
``scripts/lib/remote-restart-and-verify.sh`` and is covered by
``test_remote_health_verification``; the tests here only pin that the deploy
delivers it with the right arguments and treats its failure as fatal.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-to-pi.sh"
_HEALTH_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "lib" / "remote-restart-and-verify.sh"
)

# A host that is never contacted: every ssh/rsync invocation is intercepted by
# the shims, so the value only has to be syntactically valid.
_HOST = "pa@test-board.invalid"

# rsync's "partial transfer due to error" status -- what a permission-denied
# write into the root-owned tree actually returns. Chosen over a generic 1
# because it is the exact code the real silent failure produced.
_RSYNC_PARTIAL_TRANSFER = 23

# Text the shim writes to stderr, standing in for rsync's permission diagnostic.
# Deliberately contains no '/' at end-of-line and does not start with '.d', so a
# test asserting it survives proves stderr bypasses the readability filter
# rather than merely failing to match it by luck.
_RSYNC_STDERR = "rsync: mkstemp failed: Permission denied (13)"


def _write_shim(
    bin_dir: Path,
    name: str,
    exit_code: int = 0,
    stderr: str = "",
    fail_pattern: str = "",
) -> tuple[Path, Path]:
    """Install a recording shim for ``name``; return its argv and stdin logs.

    Each invocation appends one NUL-free line per argument plus a separator line,
    so a test can distinguish "called once with these args" from "called twice".
    Stdin is captured too, because the deploy delivers the health-verification
    script to the board on ssh's stdin.

    ``fail_pattern`` fails only the invocations whose joined argv contains it.
    The deploy makes several ssh calls; a blanket failure would always stop at
    the first one, making the later steps' failure handling untestable.
    """
    log = bin_dir / f"{name}.calls"
    stdin_log = bin_dir / f"{name}.stdin"
    shim = bin_dir / name
    stderr_line = f'printf "%s\\n" {shlex.quote(stderr)} >&2' if stderr else ":"
    selective_failure = (
        f'if [[ "$*" == *{shlex.quote(fail_pattern)}* ]]; then exit {exit_code}; fi\nexit 0'
        if fail_pattern
        else f"exit {exit_code}"
    )
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'for a in "$@"; do printf "%s\\n" "$a" >> {shlex.quote(str(log))}; done\n'
        f'printf -- "--END--\\n" >> {shlex.quote(str(log))}\n'
        f"cat >> {shlex.quote(str(stdin_log))}\n"
        f"{stderr_line}\n"
        f"{selective_failure}\n"
    )
    shim.chmod(0o755)
    return log, stdin_log


def _calls(log: Path) -> list[list[str]]:
    """Parse a shim log into one argv list per invocation."""
    if not log.exists():
        return []
    calls, current = [], []
    for line in log.read_text().splitlines():
        if line == "--END--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


@pytest.fixture
def deploy(tmp_path):
    """Run the real script with recording rsync/ssh shims on PATH.

    Returns a callable taking the shims' exit codes and the script's arguments,
    yielding the completed process plus the recorded invocations of each shim.
    ``ssh_fail_pattern`` selects which ssh invocation fails; see ``_write_shim``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def run(*args, rsync_exit=0, ssh_exit=0, rsync_stderr="", ssh_fail_pattern=""):
        rsync_log, _ = _write_shim(bin_dir, "rsync", rsync_exit, rsync_stderr)
        ssh_log, _ = _write_shim(
            bin_dir, "ssh", ssh_exit, fail_pattern=ssh_fail_pattern
        )
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        proc = subprocess.run(  # noqa: S603 - test invokes the repo's own script
            ["bash", str(_SCRIPT), "--host", _HOST, *args],  # noqa: S607
            env=env, capture_output=True, text=True, cwd=str(tmp_path),
            # DEVNULL so the shims' stdin capture cannot block on an inherited
            # stdin, which would hang the suite rather than fail it.
            stdin=subprocess.DEVNULL,
        )
        return proc, _calls(rsync_log), _calls(ssh_log)

    return run


@pytest.fixture
def ssh_stdin(tmp_path):
    """Return a callable yielding everything the deploy piped to ssh's stdin.

    Shares ``tmp_path`` with the ``deploy`` fixture, so a test that needs the
    delivered script requests both. Kept separate rather than widening
    ``deploy``'s return arity, which every existing test unpacks.
    """
    def read():
        captured = tmp_path / "bin" / "ssh.stdin"
        return captured.read_text() if captured.exists() else ""

    return read


class TestTransferFailureIsReported:
    """A failed transfer must abort loudly instead of reporting success."""

    def test_failed_transfer_exits_non_zero(self, deploy):
        # The original defect: rsync fails, the script exits 0. Regression here
        # shows up as returncode == 0.
        proc, _, _ = deploy(rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                            rsync_stderr=_RSYNC_STDERR)
        assert proc.returncode != 0

    def test_failed_transfer_does_not_claim_completion(self, deploy):
        # "Deploy complete" on a zero-file transfer is what made the failure
        # invisible. Regression: the string reappears in stdout.
        proc, _, _ = deploy(rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                            rsync_stderr=_RSYNC_STDERR)
        assert "Deploy complete" not in proc.stdout

    def test_failed_transfer_does_not_restart_the_service(self, deploy):
        # Restarting after a failed sync is what made the board look healthy
        # while running stale code. No ssh invocation may occur at all.
        # Regression: ssh_calls is non-empty.
        _, _, ssh_calls = deploy(rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                                 rsync_stderr=_RSYNC_STDERR)
        assert ssh_calls == []

    def test_transfer_diagnostics_reach_the_operator(self, deploy):
        # The permission errors existed but were merged into stdout and eaten by
        # the readability filter. They must survive on stderr. Regression: the
        # diagnostic is absent from the combined output.
        proc, _, _ = deploy(rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                            rsync_stderr=_RSYNC_STDERR)
        assert _RSYNC_STDERR in proc.stderr

    def test_reported_exit_status_is_rsync_own(self, deploy):
        # Preserving rsync's status keeps CI and callers able to tell a partial
        # transfer (23) from e.g. a protocol error. Regression: a generic 1.
        proc, _, _ = deploy(rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                            rsync_stderr=_RSYNC_STDERR)
        assert proc.returncode == _RSYNC_PARTIAL_TRANSFER


class TestElevationAndOwnership:
    """The transfer must be able to write the tree without corrupting it."""

    def test_transfer_elevates_on_the_receiver(self, deploy):
        # Without this the deploy cannot write the root-owned install tree at
        # all -- the original silent failure. Regression: no --rsync-path.
        _, rsync_calls, _ = deploy()
        assert rsync_calls, "rsync was never invoked"
        argv = rsync_calls[0]
        assert any(a.startswith("--rsync-path=") and "sudo" in a for a in argv), argv

    def test_transfer_does_not_stamp_sender_ownership(self, deploy):
        # -a implies -o -g; a root receiver then applies the sender's numeric
        # uid/gid (501:staff from macOS), destroying the root:root ownership the
        # sudoers path grants rely on. This is not hypothetical -- the recovery
        # workaround did exactly that to a live board. Regression: -a returns,
        # or -o/-g are passed explicitly.
        _, rsync_calls, _ = deploy()
        argv = rsync_calls[0]
        assert "-a" not in argv, argv
        assert "-o" not in argv and "-g" not in argv, argv

    def test_transfer_still_preserves_permissions_and_times(self, deploy):
        # Dropping -a must not silently drop recursion, symlinks, modes or
        # mtimes: without -t rsync re-sends every file each run, and without -p
        # the scripts/ helpers lose their exec bit. Regression: -rlptD absent.
        _, rsync_calls, _ = deploy()
        assert "-rlptD" in rsync_calls[0], rsync_calls[0]

    def test_elevation_can_be_disabled_for_unowned_trees(self, deploy):
        # Not every target is root-owned (a dev checkout run from a user dir).
        # The escape hatch must actually suppress elevation rather than being
        # accepted and ignored. Regression: --rsync-path present despite the
        # flag.
        _, rsync_calls, _ = deploy("--no-elevate")
        argv = rsync_calls[0]
        assert not any(a.startswith("--rsync-path=") for a in argv), argv


class TestRuntimeOwnershipIsRestored:
    """Elevating must not leave root-owned files where the service writes.

    Introduced by the fix rather than pre-existing: an elevated rsync creates
    *new* files as root, and two of the directories it ships into (``db`` and
    ``web/static``) are ones ``postinst`` grants to the service user because the
    running product writes there. Without a regrant, a deploy that adds a file
    to either leaves it unwritable by the service -- a failure that would only
    surface later, at runtime, far from the deploy that caused it.

    ``postinst``'s RUNTIME_WRITABLE_DIRS is the source of truth for the list.
    """

    # The subset of postinst's RUNTIME_WRITABLE_DIRS that the sync can actually
    # create files in; the others do not exist in the source tree.
    _SHIPPED_WRITABLE_DIRS = ("db", "web/static")

    def test_runtime_dirs_are_regranted_after_an_elevated_sync(self, deploy):
        # Regression: a newly shipped file under db/ or web/static/ stays
        # root-owned and the service cannot rewrite it.
        _, _, ssh_calls = deploy()
        remote = " ".join(a for c in ssh_calls for a in c)
        assert "chown" in remote, remote
        for directory in self._SHIPPED_WRITABLE_DIRS:
            assert directory in remote, (directory, remote)

    def test_regrant_targets_only_the_runtime_dirs(self, deploy):
        # The regrant must never widen to the whole tree. postinst keeps the
        # install root:root precisely because the passwordless sudo grants are
        # path literals under scripts/ -- a service user able to rewrite those
        # could escalate to root. Regression: a blanket `chown -R` on the
        # install root silently recreates that escalation on every deploy, which
        # is strictly worse than the bug being fixed.
        _, _, ssh_calls = deploy()
        remote = " ".join(a for c in ssh_calls for a in c)
        assert "scripts" not in remote, remote
        for chown_cmd in [s for s in remote.split("&&") if "chown" in s]:
            assert "/opt/universalchess " not in chown_cmd, chown_cmd
            assert not chown_cmd.rstrip().endswith("/opt/universalchess"), chown_cmd

    def test_no_regrant_without_elevation(self, deploy):
        # Unelevated transfers write as the SSH user, so nothing became
        # root-owned and there is nothing to repair. Issuing the chown anyway
        # would need sudo on a target deliberately chosen for not needing it.
        _, _, ssh_calls = deploy("--no-elevate", "--no-restart")
        remote = " ".join(a for c in ssh_calls for a in c)
        assert "chown" not in remote, remote

    def test_no_regrant_when_nothing_was_transferred(self, deploy):
        # A dry run changes no ownership, so it must not mutate the board.
        # Regression: --dry-run stops being read-only.
        _, _, ssh_calls = deploy("--dry-run")
        assert ssh_calls == []


class TestSuccessfulDeploy:
    """The happy path and the read-only modes must be unaffected by the fix."""

    def test_successful_deploy_restarts_and_reports_completion(self, deploy, ssh_stdin):
        # Guards against the fix over-correcting into always failing. Asserts the
        # restart actually happened rather than counting ssh invocations, which
        # would pin the number of remote steps instead of the behavior. The
        # restart moved into the delivered verification script, so its presence
        # on ssh's stdin is what proves the board is being restarted.
        proc, rsync_calls, _ = deploy()
        assert proc.returncode == 0, proc.stderr
        assert "Deploy complete" in proc.stdout
        assert len(rsync_calls) == 1
        assert "systemctl restart" in ssh_stdin()

    def test_successful_deploy_targets_the_requested_host(self, deploy):
        # A transfer that lands somewhere other than the named host is the same
        # class of bug as one that lands nowhere. Regression: the destination
        # argument no longer carries the host.
        _, rsync_calls, ssh_calls = deploy()
        assert any(a.startswith(f"{_HOST}:") for a in rsync_calls[0]), rsync_calls[0]
        assert _HOST in ssh_calls[0], ssh_calls[0]

    def test_dry_run_transfers_nothing_and_does_not_restart(self, deploy):
        # Pre-existing contract. Regression: ssh is invoked, or -n is missing
        # from the rsync argv so a "preview" actually writes to the board.
        proc, rsync_calls, ssh_calls = deploy("--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "-n" in rsync_calls[0], rsync_calls[0]
        assert ssh_calls == []

    def test_no_restart_flag_syncs_without_restarting(self, deploy, ssh_stdin):
        # Pre-existing contract, re-pinned because the failure path now also
        # skips the restart -- the two must not become indistinguishable. The
        # ownership regrant still runs (the transfer happened), so this asserts
        # no *restart* rather than no ssh at all.
        proc, rsync_calls, _ = deploy("--no-restart")
        assert proc.returncode == 0, proc.stderr
        assert len(rsync_calls) == 1
        assert "systemctl restart" not in ssh_stdin()

    def test_check_mode_reports_failure_of_its_own_probe(self, deploy):
        # --check is read-only, but a probe that cannot even reach the board
        # must not print "All content in sync" -- that reads as a positive
        # confirmation and is exactly the false reassurance being removed.
        proc, _, _ = deploy("--check", rsync_exit=_RSYNC_PARTIAL_TRANSFER,
                            rsync_stderr=_RSYNC_STDERR)
        assert proc.returncode != 0
        assert "All content in sync" not in proc.stdout

    def test_check_mode_does_not_elevate(self, deploy):
        # --check only reads a world-readable tree; requesting sudo there would
        # make a diagnostic command fail on boards without a NOPASSWD grant.
        _, rsync_calls, ssh_calls = deploy("--check")
        assert not any(a.startswith("--rsync-path=") for a in rsync_calls[0])
        assert ssh_calls == []


class TestPostDeployVerification:
    """The deploy must delegate health verification and honour its verdict.

    The previous inline check (``sleep 3 && systemctl is-active``) reported a
    healthy board while the web app was crash-looping on the code just shipped;
    see test_remote_health_verification for why each part of it could not detect
    that. These tests cover only the hand-off: that the real script is delivered
    with the right arguments, and that its failure fails the deploy.
    """

    # ssh argv fragment identifying the verification invocation, so a selective
    # shim failure hits that step rather than the earlier ownership regrant.
    _VERIFY_INVOCATION = "bash"

    # An exit code the verification script defines (a unit crash-looped). Chosen
    # over a generic 1 to prove the specific status propagates.
    _EXIT_UNIT_RESTARTED = 3

    def test_verification_script_is_delivered_to_the_board(self, deploy, ssh_stdin):
        # The script is piped on stdin so it always matches the checkout being
        # deployed, with no install step. Regression: nothing is delivered and
        # the board runs whatever stale copy it has, or none.
        deploy()
        assert ssh_stdin() == _HEALTH_SCRIPT.read_text()

    def test_verification_receives_both_units_and_the_web_port(self, deploy):
        # Positional arguments over ssh are easy to drop or reorder. The port
        # must be the loopback port the web unit binds: probing anything else
        # measures nginx, which answers 502 for the whole ~70s import. The
        # timeout must exceed that import time or the check times out on every
        # healthy deploy. Regression: an argument is lost and verification either
        # fails on a healthy board or checks nothing.
        _, _, ssh_calls = deploy()
        verify = [c for c in ssh_calls if self._VERIFY_INVOCATION in c]
        assert len(verify) == 1, ssh_calls
        argv = verify[0]
        assert "universal-chess" in argv and "universal-chess-web" in argv, argv
        assert "5000" in argv, argv
        timeout = int(argv[-1])
        assert timeout > 70, argv

    def test_failed_verification_exits_with_the_verification_status(self, deploy):
        # Distinct codes let a caller tell "crashed after starting" (3) from
        # "never started" (4). Regression: the status is flattened to 1 and that
        # distinction is lost to CI and to any wrapper.
        proc, _, _ = deploy(
            ssh_exit=self._EXIT_UNIT_RESTARTED,
            ssh_fail_pattern=self._VERIFY_INVOCATION,
        )
        assert proc.returncode == self._EXIT_UNIT_RESTARTED, proc.stdout

    def test_failed_verification_does_not_claim_completion(self, deploy):
        # The exact false reassurance being removed: a board whose web app is
        # crash-looping on the new code reported "Deploy complete" and exit 0.
        # Regression: the string reappears despite a failed verification.
        proc, _, _ = deploy(
            ssh_exit=self._EXIT_UNIT_RESTARTED,
            ssh_fail_pattern=self._VERIFY_INVOCATION,
        )
        assert "Deploy complete" not in proc.stdout
