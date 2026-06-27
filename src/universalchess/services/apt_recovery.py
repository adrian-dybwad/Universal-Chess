"""Recover an interrupted dpkg transaction before running apt.

A killed or aborted apt/dpkg run -- a manual install, or (observed here) an
engine build-dependency install interrupted by a reboot under load -- leaves the
dpkg database half-configured. Every subsequent apt operation then aborts during
"Reading package lists" with::

    E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to
    correct the problem.

Only ``dpkg --configure -a`` finishes the interrupted transaction;
``apt-get install -f`` does not. The same recovery already runs in the OTA
installer (``scripts/install-update``); this module makes it reusable from
Python so every apt mutation path (engine installs) can self-heal in one place.

The hazard that shapes the design: ``dpkg --configure -a`` configures EVERY
pending package. If the wedged package is ``universal-chess`` itself,
configuring it re-runs our postinst, which restarts ``universal-chess-web``
(default ``KillMode=control-group``). A ``dpkg`` child started in-process would
then be SIGKILLed mid-transaction -- corrupting the database worse than before.

So recovery is guarded:

* Our own package NOT pending configuration (the common case -- some unrelated
  package is wedged): run ``dpkg --configure -a`` in-process. It is a no-op when
  nothing is pending and cannot restart us, so it is safe and synchronous.
* Our own package pending configuration: do NOT run it in-process. Launch it
  out-of-process via ``systemd-run`` (a transient unit owned by PID 1, like the
  OTA reboot) so it survives the service restart it triggers, and report
  :data:`RecoveryOutcome.DEFERRED_RESTART` so the caller can show a friendly
  "fixing incomplete install, please retry after restart" warning and abort.

Read-only ``dpkg-query`` callers (package-presence checks) do not need this --
an interrupted transaction does not block queries, only mutations.
"""

import subprocess
from enum import Enum
from typing import Callable, Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging

    log = logging.getLogger(__name__)

# The Debian package name for this application (matches the dpkg-query in
# update_service and the deb control file). Configuring this package re-runs our
# postinst, which restarts universal-chess-web -- the reason for the guard below.
OWN_PACKAGE = "universal-chess"

# dpkg state words (third field of ${Status}) that ``dpkg --configure -a`` would
# act on by running the package's postinst. "installed" is the healthy state;
# "config-files" means removed-with-config (configure will not reinstall it).
_PENDING_CONFIGURE_STATES = frozenset({"half-configured", "half-installed", "unpacked"})

# Transient unit name for the detached repair. --collect garbage-collects it
# after exit. The short sleep lets the caller persist its user-facing warning and
# return before the configure's postinst restarts the web service.
_REPAIR_UNIT = "universal-chess-dpkg-repair"
_REPAIR_PRE_DELAY_SECONDS = 5

# Bound the in-process configure: a genuine half-configured package can take a
# while to configure, but this must not hang an install thread indefinitely.
_CONFIGURE_TIMEOUT_SECONDS = 600


class RecoveryOutcome(Enum):
    """Result of an interrupted-dpkg recovery attempt.

    PROCEEDED: safe to continue with the caller's apt step (nothing was pending,
        or an unrelated pending transaction was finished in-process).
    DEFERRED_RESTART: our own package was pending configuration; the fix was
        launched out-of-process and will restart the service shortly. The caller
        must surface a warning and abort -- the current operation cannot complete.
    FAILED: a recovery was needed but did not succeed. The caller should proceed
        anyway so the subsequent apt step surfaces the genuine error rather than
        masking it.
    """

    PROCEEDED = "proceeded"
    DEFERRED_RESTART = "deferred_restart"
    FAILED = "failed"


def _own_package_state(run: Callable[..., subprocess.CompletedProcess]) -> Optional[str]:
    """Return the dpkg state word for :data:`OWN_PACKAGE`, or None if unknown.

    Reads the third field of dpkg's ``${Status}`` (e.g. "installed",
    "half-configured"). Returns None when the package is unknown (dpkg-query
    exits non-zero) or dpkg is absent (non-Debian dev/CI machine) -- both mean
    "nothing of ours could be restarted by a configure".
    """
    try:
        result = run(
            ["dpkg-query", "-W", "-f=${Status}", OWN_PACKAGE],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # No dpkg on this system (e.g. macOS dev box): nothing to recover.
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().split()[-1]


def _launch_detached_configure(
    run: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Launch ``dpkg --configure -a`` in a transient systemd unit (PID 1 owned).

    Used only when configuring would restart us: a transient unit is in its own
    cgroup, so it survives the universal-chess-web restart that the configure's
    postinst triggers. Mirrors the OTA reboot pattern in the postinst. A short
    pre-delay gives the caller time to persist its user-facing warning before the
    restart lands. Returns True if the unit launched.
    """
    cmd = (
        f"sleep {_REPAIR_PRE_DELAY_SECONDS}; "
        "dpkg --configure -a"
    )
    try:
        result = run(
            [
                "sudo",
                "systemd-run",
                "--collect",
                f"--unit={_REPAIR_UNIT}",
                "/bin/sh",
                "-c",
                cmd,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.error("[apt_recovery] systemd-run not available; cannot launch detached dpkg repair")
        return False
    if result.returncode != 0:
        log.error(
            f"[apt_recovery] detached dpkg repair failed to launch (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
        return False
    log.warning("[apt_recovery] launched detached 'dpkg --configure -a'; service restart imminent")
    return True


def recover_interrupted_dpkg(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    launch_detached: Optional[Callable[..., bool]] = None,
) -> RecoveryOutcome:
    """Finish any interrupted dpkg transaction before the caller runs apt.

    Args:
        run: subprocess.run-compatible callable (injected for tests).
        launch_detached: callable that launches the out-of-process repair and
            returns success (injected for tests). Defaults to the systemd-run
            launcher.

    Returns:
        A :class:`RecoveryOutcome`. See its members for how each must be handled.
        Only :data:`RecoveryOutcome.DEFERRED_RESTART` requires the caller to
        abort (and warn); PROCEEDED and FAILED both mean "continue with apt".
    """
    if launch_detached is None:
        launch_detached = _launch_detached_configure

    state = _own_package_state(run)
    if state in _PENDING_CONFIGURE_STATES:
        # Configuring our own package would restart the web service and kill an
        # in-process dpkg child mid-transaction. Hand the repair to PID 1 instead.
        log.warning(
            f"[apt_recovery] {OWN_PACKAGE} is '{state}'; deferring 'dpkg --configure -a' "
            "to a detached unit to avoid killing it via the service restart"
        )
        if launch_detached(run):
            return RecoveryOutcome.DEFERRED_RESTART
        return RecoveryOutcome.FAILED

    # Safe to run in-process: our package is healthy/absent, so the configure
    # cannot restart us. It is a no-op when nothing is pending, and otherwise
    # finishes whatever unrelated transaction was wedging apt.
    try:
        result = run(
            ["sudo", "dpkg", "--configure", "-a"],
            capture_output=True,
            text=True,
            timeout=_CONFIGURE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        # No dpkg on this system: nothing to recover, let the caller proceed.
        return RecoveryOutcome.PROCEEDED
    if result.returncode != 0:
        log.warning(
            f"[apt_recovery] 'dpkg --configure -a' returned {result.returncode}: "
            f"{result.stderr.strip()}"
        )
        return RecoveryOutcome.FAILED
    return RecoveryOutcome.PROCEEDED
