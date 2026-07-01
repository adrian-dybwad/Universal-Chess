"""Tests for dpkg interrupted-transaction recovery (services.apt_recovery).

Context: a previously killed apt/dpkg run (e.g. a manual install, an engine
dependency install interrupted by a reboot under load) leaves the dpkg database
half-configured. EVERY subsequent apt operation then aborts during "Reading
package lists" with "E: dpkg was interrupted, you must manually run 'dpkg
--configure -a'". That is exactly what blocked the Zahak ``golang`` install.

``dpkg --configure -a`` is the only thing that finishes the interrupted
transaction (``apt-get install -f`` does not). The catch: ``dpkg --configure
-a`` configures EVERY pending package, so if the wedged package is
``universal-chess`` itself, running it re-triggers our postinst, which restarts
``universal-chess-web.service``. That service runs with the default
``KillMode=control-group``, so an in-process ``dpkg`` child would be killed
mid-transaction -- corrupting the database worse. These tests pin the guard:
recover in-process only when our own package is NOT pending configuration, and
otherwise launch the fix out-of-process (so it survives the restart) and report
that a restart is imminent.
"""

from universalchess.services import apt_recovery
from universalchess.services.apt_recovery import RecoveryOutcome


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _status_runner(status_stdout, status_rc=0, configure_rc=0):
    """Build a fake subprocess.run that answers dpkg-query and dpkg --configure.

    ``dpkg-query`` returns ``status_stdout``/``status_rc`` (the universal-chess
    package state); ``dpkg --configure -a`` returns ``configure_rc``. Every
    invocation is recorded so call ordering/inclusion can be asserted.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "dpkg-query" in cmd:
            return _FakeProc(returncode=status_rc, stdout=status_stdout)
        if "dpkg" in cmd and "--configure" in cmd:
            return _FakeProc(returncode=configure_rc)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, calls


def test_no_pending_runs_configure_in_process_and_proceeds():
    """When our package is fully installed, recover in-process and proceed.

    Why this test exists: this is the common Zahak case -- some UNRELATED package
    is wedged. universal-chess is "install ok installed", so configuring is safe
    (it cannot restart us) and finishes the unrelated transaction so the caller's
    apt step works.

    How the regression manifests: if the guard wrongly treated an installed
    package as pending, it would defer to an out-of-process restart and abort a
    perfectly recoverable install; if it skipped the configure entirely, the
    caller's apt would still abort on the interrupted transaction.
    """
    fake_run, calls = _status_runner("install ok installed")
    launched = []

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: launched.append(True) or True
    )

    assert outcome is RecoveryOutcome.PROCEEDED
    # The configure ran in-process; the detached launcher was never used.
    assert any("--configure" in c for c in calls)
    assert launched == []


def test_own_package_pending_defers_to_detached_and_reports_restart():
    """When universal-chess is half-configured, defer the fix out-of-process.

    Why this test exists: configuring our own half-configured package runs our
    postinst, which restarts universal-chess-web (KillMode=control-group). An
    in-process dpkg child would be SIGKILLed mid-transaction, worsening the
    corruption. The fix must instead be launched detached (systemd-run) so it
    survives the restart, and the caller must be told a restart is imminent so it
    can surface a friendly warning and abort.

    How the regression manifests: if it ran the configure in-process here, the
    list of executed commands would include "--configure" and the database could
    be left worse; the assertion that no in-process configure ran would fail.
    """
    fake_run, calls = _status_runner("install ok half-configured")
    launched = []

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: launched.append(True) or True
    )

    assert outcome is RecoveryOutcome.DEFERRED_RESTART
    # The detached launcher was used; no in-process configure was attempted.
    assert launched == [True]
    assert not any(isinstance(c, list) and "--configure" in c for c in calls)


def test_own_package_pending_failed_launch_reports_failed():
    """A failed detached launch reports FAILED, not a false DEFERRED_RESTART.

    Why this test exists: if systemd-run cannot start the repair, no restart is
    coming. Reporting DEFERRED_RESTART would tell the user to wait for a restart
    that never happens. FAILED lets the caller fall through so apt surfaces the
    real interrupted-dpkg error instead.

    How the regression manifests: mapping any own-package-pending state to
    DEFERRED_RESTART regardless of launch success would hang the UX on a promised
    restart that never occurs.
    """
    fake_run, _ = _status_runner("install ok half-configured")

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: False
    )

    assert outcome is RecoveryOutcome.FAILED


def test_in_process_configure_failure_reports_failed():
    """A non-zero in-process configure reports FAILED (caller still proceeds).

    Why this test exists: matching the OTA precedent, a failed recovery must not
    mask the underlying problem -- the caller proceeds and the subsequent apt step
    surfaces the genuine error. FAILED records that the repair did not succeed.

    How the regression manifests: returning PROCEEDED on a failed configure would
    claim a repair happened when it did not, hiding why apt later fails.
    """
    fake_run, _ = _status_runner("install ok installed", configure_rc=1)

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: True
    )

    assert outcome is RecoveryOutcome.FAILED


def test_unknown_package_treated_as_not_pending():
    """If dpkg-query does not know universal-chess, treat it as not pending.

    Why this test exists: in non-packaged environments (dev box, CI) the package
    is absent and dpkg-query exits non-zero. That must not be read as "pending
    configuration" -- there is nothing of ours to restart, so the in-process
    configure path (a harmless no-op when nothing is pending) is correct.

    How the regression manifests: treating a non-zero dpkg-query as pending would
    wrongly defer to a detached restart on a machine that has no such service.
    """
    fake_run, calls = _status_runner("", status_rc=1)
    launched = []

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: launched.append(True) or True
    )

    assert outcome is RecoveryOutcome.PROCEEDED
    assert launched == []
    assert any("--configure" in c for c in calls)


def test_missing_dpkg_binary_is_a_noop_proceed():
    """A system without dpkg at all recovers to PROCEEDED, not an exception.

    Why this test exists: on a developer machine (macOS) dpkg-query does not
    exist and subprocess raises FileNotFoundError. Recovery is meaningless there,
    so it must degrade to "nothing to do, proceed" rather than crashing the
    install flow that calls it.

    How the regression manifests: an unguarded FileNotFoundError would propagate
    out of recovery and abort every engine install on non-Debian dev machines.
    """
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    outcome = apt_recovery.recover_interrupted_dpkg(
        run=fake_run, launch_detached=lambda run: True
    )

    assert outcome is RecoveryOutcome.PROCEEDED


def _fix_broken_runner(status_stdout, status_rc=0, fix_rc=0, fix_missing=False):
    """Build a fake subprocess.run answering dpkg-query and ``apt-get install -f``.

    ``dpkg-query`` returns ``status_stdout``/``status_rc`` (the universal-chess
    package state); ``apt-get install -f -y`` returns ``fix_rc`` (or, when
    ``fix_missing`` is set, raises FileNotFoundError to emulate a box with no
    apt-get). Every invocation is recorded so call inclusion/ordering can be
    asserted.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "dpkg-query" in cmd:
            return _FakeProc(returncode=status_rc, stdout=status_stdout)
        if "apt-get" in cmd and "install" in cmd and "-f" in cmd:
            if fix_missing:
                raise FileNotFoundError("apt-get")
            return _FakeProc(returncode=fix_rc)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, calls


def test_fix_broken_runs_apt_get_f_in_process_and_proceeds():
    """When our package is healthy, run ``apt-get install -f`` in-process and proceed.

    Why this test exists: this is the Zahak "Unmet dependencies. Try 'apt
    --fix-broken install'" case -- the system had pre-existing broken deps that
    blocked installing golang. universal-chess is "install ok installed", so
    correcting broken packages in-process is safe (it cannot restart us) and lets
    the caller retry the dependency install.

    How the regression manifests: if the guard wrongly treated an installed
    package as pending it would defer to a restart instead of fixing in-process;
    if it skipped the apt-get call, the broken deps would remain and the retry
    would fail identically.
    """
    fake_run, calls = _fix_broken_runner("install ok installed")
    launched = []

    outcome = apt_recovery.attempt_fix_broken(
        run=fake_run, launch_detached=lambda run: launched.append(True) or True
    )

    assert outcome is RecoveryOutcome.PROCEEDED
    assert any("apt-get" in c and "-f" in c for c in calls)
    assert launched == []


def test_fix_broken_failure_reports_failed():
    """A non-zero ``apt-get install -f`` reports FAILED, not PROCEEDED.

    Why this test exists: if the repair itself fails, the broken state is not
    corrected. FAILED lets the caller stop claiming a fix happened and surface the
    genuine apt error to the user instead of retrying into the same failure.

    How the regression manifests: returning PROCEEDED on a failed fix would tell
    the caller to retry an install that cannot succeed, hiding why apt failed.
    """
    fake_run, _ = _fix_broken_runner("install ok installed", fix_rc=1)

    outcome = apt_recovery.attempt_fix_broken(
        run=fake_run, launch_detached=lambda run: True
    )

    assert outcome is RecoveryOutcome.FAILED


def test_fix_broken_own_package_pending_defers_to_detached():
    """When universal-chess is half-configured, defer the repair out-of-process.

    Why this test exists: ``apt-get install -f`` configures pending packages, so
    if our own package is half-configured it would run our postinst and restart
    universal-chess-web (KillMode=control-group), SIGKILLing the in-process apt
    child mid-transaction. The same guard as recover_interrupted_dpkg must apply:
    launch the fix detached and report a restart is imminent.

    How the regression manifests: if it ran apt-get in-process here, the executed
    commands would include an apt-get install -f call; the assertion that none ran
    would fail, exposing the reintroduced SIGKILL hazard.
    """
    fake_run, calls = _fix_broken_runner("install ok half-configured")
    launched = []

    outcome = apt_recovery.attempt_fix_broken(
        run=fake_run, launch_detached=lambda run: launched.append(True) or True
    )

    assert outcome is RecoveryOutcome.DEFERRED_RESTART
    assert launched == [True]
    assert not any("apt-get" in c and "-f" in c for c in calls)


def test_fix_broken_missing_apt_get_is_a_noop_proceed():
    """A system without apt-get recovers to PROCEEDED, not an exception.

    Why this test exists: on a dev box dpkg-query may be shimmed but apt-get can
    still be absent; subprocess raises FileNotFoundError. The repair is meaningless
    there, so it must degrade to "nothing to do, proceed" rather than crashing the
    install flow.

    How the regression manifests: an unguarded FileNotFoundError would propagate
    out and abort the caller instead of letting the subsequent step run.
    """
    fake_run, _ = _fix_broken_runner("install ok installed", fix_missing=True)

    outcome = apt_recovery.attempt_fix_broken(
        run=fake_run, launch_detached=lambda run: True
    )

    assert outcome is RecoveryOutcome.PROCEEDED


def test_summarize_apt_error_keeps_cause_not_just_generic_advice():
    """The summary must retain the unmet-dependency cause, not only apt's advice.

    Why this test exists: apt prints generic advice ("Try 'apt --fix-broken
    install'") BEFORE the line naming which package/dependency is unmet. A blind
    prefix slice (the previous [:200]) kept only the advice and discarded the
    cause, making field reports undiagnosable. The summary must surface the
    actionable "Depends:" detail.

    How the regression manifests: reverting to a leading-prefix slice drops the
    "golang-1.19-go" Depends line, so the assertion on the cause fails while the
    advice line still passes.
    """
    stderr = (
        "E: Unmet dependencies. Try 'apt --fix-broken install' with no packages "
        "(or specify a solution).\n"
        "E: The following information from --solver 3.0 may provide additional "
        "context:\n"
        "The following packages have unmet dependencies:\n"
        " golang-go : Depends: golang-1.19-go but it is not installable\n"
    )

    summary = apt_recovery.summarize_apt_error(stderr, "")

    assert "golang-1.19-go" in summary
    assert "Depends:" in summary


def test_summarize_apt_error_falls_back_to_raw_when_no_diagnostic_lines():
    """With no recognizable diagnostic line, the summary returns the raw text.

    Why this test exists: not every apt failure prints an "E:"/"Depends:" line
    (e.g. a lock error). The summary must not return empty in that case -- it must
    fall back to the raw output so the user still sees something actionable.

    How the regression manifests: if extraction returned only matched lines with
    no fallback, an unrecognized error would surface as an empty message.
    """
    stderr = "E: Could not get lock /var/lib/dpkg/lock-frontend"

    summary = apt_recovery.summarize_apt_error(stderr, "")

    assert "Could not get lock" in summary


def test_summarize_apt_error_prefers_stdout_when_stderr_empty():
    """apt writes some unmet-dependency detail to stdout; the summary scans both.

    Why this test exists: depending on apt version the "unmet dependencies" block
    can land on stdout while stderr is empty. Scanning only stderr would lose the
    cause. The summary must consider stdout when stderr carries nothing useful.

    How the regression manifests: scanning stderr only returns empty here, so the
    package cause is lost -- the assertion on the stdout detail fails.
    """
    stdout = (
        "The following packages have unmet dependencies:\n"
        " golang-go : Depends: golang-1.19-go but it is not installable\n"
    )

    summary = apt_recovery.summarize_apt_error("", stdout)

    assert "golang-1.19-go" in summary


def test_summarize_apt_error_respects_char_limit():
    """The summary must never exceed the requested character limit.

    Why this test exists: the message is surfaced in a fixed-size install-error UI
    field; an unbounded apt dump (thousands of lines of "Depends:") would overflow
    it. The cap must be enforced even when many lines match the diagnostic filter.

    How the regression manifests: dropping the cap makes the returned string grow
    with the input, so this length assertion fails on the large synthetic input.
    """
    stderr = "\n".join(f"E: broken package number {i} Depends: dep{i}" for i in range(500))

    summary = apt_recovery.summarize_apt_error(stderr, "", limit=300)

    assert len(summary) <= 300
