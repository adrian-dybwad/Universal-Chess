"""Tests for the build_memory context manager (services/build_memory.py).

This wraps the uc-build-memory root helper around engine source builds. The
contract that matters: acquire/release are issued as the exact pinned sudo
command, release happens iff acquire succeeded, and a helper failure degrades to
"build without extra swap" rather than blocking the install. Each test pins one
of those guarantees.
"""

import subprocess

import pytest

from universalchess.services.build_memory import HELPER_PATH, build_memory


class _Recorder:
    """Records (argv, timeout) calls and returns scripted CompletedProcess results."""

    def __init__(self, results):
        # results: list of CompletedProcess (or exceptions) returned in order.
        self._results = list(results)
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(code=1, stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)


def test_acquire_then_release_issue_pinned_sudo_commands():
    # The helper is a NOPASSWD-pinned path invoked via `sudo -n`; the argv must
    # match exactly or the grant fails. Pin both acquire and release argv, and
    # that the owner-pid is threaded through (the helper's liveness anchor).
    runner = _Recorder([_ok(), _ok()])
    with build_memory(owner_pid=4321, run=runner) as acquired:
        assert acquired is True
    assert runner.calls[0][0] == ["sudo", "-n", HELPER_PATH, "acquire", "--owner-pid", "4321"]
    assert runner.calls[1][0] == ["sudo", "-n", HELPER_PATH, "release", "--owner-pid", "4321"]


def test_release_runs_even_when_block_raises():
    # Swap must be released even if the build raises, or it would leak until the
    # next acquire/reboot. The finally-release is what guards this.
    runner = _Recorder([_ok(), _ok()])
    with pytest.raises(RuntimeError), build_memory(owner_pid=7, run=runner):
        raise RuntimeError("build blew up")
    assert [c[0][3] for c in runner.calls] == ["acquire", "release"]


def test_failed_acquire_yields_false_and_skips_release():
    # If acquisition fails (no sudo grant / helper error), the build must still be
    # attempted (yield False, do not raise) and NO release is issued -- releasing
    # something never acquired could tear down another holder's swap.
    runner = _Recorder([_fail(stderr="a password is required")])
    with build_memory(owner_pid=9, run=runner) as acquired:
        assert acquired is False
    assert len(runner.calls) == 1
    assert runner.calls[0][0][3] == "acquire"


def test_acquire_that_cannot_run_is_non_fatal():
    # A missing helper binary (FileNotFoundError) must not break installs: the
    # context manager swallows it, yields False, and issues no release.
    runner = _Recorder([FileNotFoundError("no such helper")])
    with build_memory(owner_pid=9, run=runner) as acquired:
        assert acquired is False
    assert len(runner.calls) == 1


def test_release_failure_does_not_raise():
    # A failed release is logged, not raised (the helper self-heals a leaked
    # holder on the next acquire); the with-block must exit cleanly.
    runner = _Recorder([_ok(), _fail()])
    with build_memory(owner_pid=9, run=runner) as acquired:
        assert acquired is True
    # Both attempted; no exception escaped.
    assert [c[0][3] for c in runner.calls] == ["acquire", "release"]


def test_release_that_cannot_run_does_not_raise():
    # Even if the release invocation itself raises (OSError), exiting the block
    # must not propagate it.
    runner = _Recorder([_ok(), OSError("sudo vanished")])
    with build_memory(owner_pid=9, run=runner):
        pass
    assert len(runner.calls) == 2
