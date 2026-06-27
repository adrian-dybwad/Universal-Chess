"""Temporarily expand swap headroom around heavy engine builds.

An engine source build can exceed a RAM-constrained board's memory and be
OOM-killed (e.g. Zahak's NNUE-embedding compile on a 512 MB Pi Zero 2 W). This
module wraps the pinned ``uc-build-memory`` root helper, which brings up a zram
tier plus a temporary SD-card swap backstop for the duration of a build and tears
it down afterwards, so normal play keeps running on RAM alone with no permanent
swap.

The web service runs unprivileged, so the helper is invoked via ``sudo -n``
against the single pinned path the package postinst grants (mirroring bt-admin).
The BlueZ self-heal rebuild calls the same helper directly from its own root
script; the reference counting in the helper makes overlapping holders safe.

Acquisition is best-effort: if the helper is missing, the sudo grant is absent,
or it errors, the build proceeds *without* the extra headroom rather than being
blocked. The worst case is the prior behavior (a possible OOM), never a failed
install because of a memory-management problem.
"""

import logging
import os
import subprocess
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Sequence

log = logging.getLogger(__name__)

HELPER_PATH = "/opt/universalchess/scripts/uc-build-memory"

# Setup uses fallocate (near-instant); the generous bound only matters if the
# helper falls back to dd'ing a swapfile on a slow SD card.
_ACQUIRE_TIMEOUT_SECONDS = 300
_RELEASE_TIMEOUT_SECONDS = 120

# A command runner takes (argv, timeout_seconds) and returns a CompletedProcess.
# Injected in tests so the sudo invocation can be asserted without root.
CommandRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


def _default_runner(args: Sequence[str], timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


@contextmanager
def build_memory(
    owner_pid: Optional[int] = None,
    helper_path: str = HELPER_PATH,
    run: CommandRunner = _default_runner,
) -> Iterator[bool]:
    """Hold extra swap headroom for the duration of the ``with`` block.

    Yields True if the headroom was acquired (and will be released on exit), or
    False if acquisition was skipped/failed (nothing to release). Never raises
    for a helper failure -- callers must still attempt the build.

    The owner PID (defaulting to this process) keys the helper's reference count;
    it is the liveness anchor that lets the helper reclaim swap if this process
    dies mid-build without releasing.
    """
    pid = str(owner_pid if owner_pid is not None else os.getpid())
    acquired = _acquire(run, helper_path, pid)
    try:
        yield acquired
    finally:
        if acquired:
            _release(run, helper_path, pid)


def _acquire(run: CommandRunner, helper_path: str, pid: str) -> bool:
    args = ["sudo", "-n", helper_path, "acquire", "--owner-pid", pid]
    try:
        proc = run(args, _ACQUIRE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "build_memory: could not run acquire (%s); building without extra swap", exc
        )
        return False
    if proc.returncode != 0:
        log.warning(
            "build_memory: acquire exited %s; building without extra swap. stderr=%s",
            proc.returncode, (proc.stderr or "").strip(),
        )
        return False
    log.info("build_memory: acquired extra swap (owner pid %s)", pid)
    return True


def _release(run: CommandRunner, helper_path: str, pid: str) -> None:
    args = ["sudo", "-n", helper_path, "release", "--owner-pid", pid]
    try:
        proc = run(args, _RELEASE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        # A failed release is logged but not raised: the helper prunes a dead
        # owner on the next acquire and tmpfs state clears on reboot, so a leak
        # is bounded rather than permanent.
        log.warning(
            "build_memory: could not run release (%s); swap may persist until the "
            "next acquire or reboot", exc,
        )
        return
    if proc.returncode != 0:
        log.warning(
            "build_memory: release exited %s. stderr=%s",
            proc.returncode, (proc.stderr or "").strip(),
        )
    else:
        log.info("build_memory: released extra swap (owner pid %s)", pid)
