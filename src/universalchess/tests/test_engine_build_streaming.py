"""Tests for streamed build-command execution in the source installer.

A source build on a constrained board runs for minutes; previously its output was
captured all-at-once, so the UI could not tell a slow build from a hung one and a
build that hit a timeout left no progress trail. ``_run_monitored_command`` streams
combined stdout+stderr line-by-line to a callback, retains a trailing tail for the
failure message, and ends a command that has stopped making progress even when it
is silent. These tests pin that behavior at the method boundary using real
short-lived shell commands (the boundary it actually integrates with).

The command is bounded by a stall window rather than a fixed duration: what should
end a build is the absence of progress, not the passage of time. ``sleep`` is used
below as the silent, CPU-idle, byte-idle process that a stalled build looks like.
"""

import subprocess
import time

import pytest

from universalchess.managers.engine_manager import EngineManager


@pytest.fixture
def manager(tmp_path):
    """An EngineManager whose dirs point at a temp location (no real installs)."""
    return EngineManager(engines_dir=str(tmp_path))


def test_streams_each_line_and_returns_zero(manager, tmp_path):
    """Every output line is delivered to on_line and success returns rc 0.

    Why this test exists: the live-progress feature depends on each build-output
    line reaching the callback in order; if streaming regressed to a single
    end-of-build dump, the banner would show nothing until the build finished.

    How the regression manifests: if the reader stopped forwarding lines (or
    buffered them), ``seen`` would miss interior lines; if returncode handling
    broke, the success assertion would fail.
    """
    seen = []
    # stdout and stderr both produced, to prove they are merged in order-ish.
    rc, tail = manager._run_monitored_command(
        "echo one; echo two 1>&2; echo three",
        tmp_path,
        on_line=seen.append,
    )

    assert rc == 0
    assert seen == ["one", "two", "three"]
    # The tail mirrors what was streamed (used for the failure message).
    assert tail.splitlines() == ["one", "two", "three"]


def test_nonzero_exit_is_reported_with_tail(manager, tmp_path):
    """A failing command returns its code and a tail that includes stderr.

    Why this test exists: the install-error message is built from the tail, and
    the real cause (e.g. a compiler error on stderr) must be present. stdout and
    stderr are merged precisely so the error is captured regardless of stream.

    How the regression manifests: if stderr were not merged into the stream, the
    tail would omit the error line and the failure message would be uninformative;
    if the exit code were swallowed, the build would be treated as successful.
    """
    rc, tail = manager._run_monitored_command(
        "echo building; echo boom-error 1>&2; exit 7",
        tmp_path,
        on_line=lambda _line: None,
    )

    assert rc == 7
    assert "boom-error" in tail


def test_tail_is_bounded(manager, tmp_path, monkeypatch):
    """Only the last N lines are retained in the tail.

    Why this test exists: build output can be large; the tail must be bounded so a
    chatty build does not balloon memory or the persisted error message. The bound
    is the dominant lines (the end), which is where build failures report.

    How the regression manifests: removing the maxlen bound would retain the whole
    output; this asserts an early line is dropped while late lines are kept.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager._BUILD_TAIL_LINES", 5
    )
    rc, tail = manager._run_monitored_command(
        "for i in $(seq 1 20); do echo line$i; done",
        tmp_path,
        on_line=lambda _line: None,
    )
    lines = tail.splitlines()

    assert rc == 0
    # Bounded to the last 5 lines: the early ones are gone, the final ones remain.
    assert lines == ["line16", "line17", "line18", "line19", "line20"]


def test_stalled_silent_process_is_killed(manager, tmp_path):
    """A silent process consuming nothing is killed (does not hang forever).

    Why this test exists: the reader-thread design exists so a command can be ended
    even when it emits no newline to unblock a read. ``sleep`` produces no output,
    burns no CPU and moves no bytes, so it presents every signal of a wedged build
    and must be stopped once the stall window passes.

    How the regression manifests: if enforcement relied on readline returning, a
    silent process would hang past the window and this test would itself time out
    (or TimeoutExpired would never be raised).
    """
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        manager._run_monitored_command(
            "sleep 30",
            tmp_path,
            on_line=lambda _line: None,
            stall_seconds=2,
            ceiling_seconds=300,
        )
    # Killed shortly after the 2s stall window, not after the full 30s sleep.
    assert time.monotonic() - start < 10


def test_timeout_carries_the_output_tail(manager, tmp_path):
    """A timed-out build reports the output it had produced before the kill.

    Why this test exists: a Pi Zero W install failed with a bare "Build timed out
    after 600s", which cannot distinguish a build that was still compiling from
    one that was wedged -- the tail was collected and then thrown away when the
    deadline fired. Attaching it to the raised TimeoutExpired is what lets the
    installer name the last compile step in the failure message.

    How the regression manifests: raising TimeoutExpired without ``output`` leaves
    ``.output`` None here, and the install error degrades back to a bare number.
    """
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        manager._run_monitored_command(
            "echo compiling-eval; sleep 30",
            tmp_path,
            on_line=lambda _line: None,
            stall_seconds=2,
            ceiling_seconds=300,
        )

    assert "compiling-eval" in (excinfo.value.output or "")


def test_progress_updater_throttles_and_prefixes(monkeypatch, manager):
    """The on_line updater throttles updates and labels them with the engine.

    Why this test exists: each progress update persists install state to the
    SD-card-backed store, so a chatty build must not trigger an update per line;
    the throttle bounds write frequency. The message must also identify the engine
    and stage so the banner row is meaningful.

    How the regression manifests: removing the throttle would emit one update per
    line (one assertion below would see >1 call within the window); dropping the
    label/stage would break the banner's engine-install row.
    """
    from universalchess.services.engine_install_state import InstallStage

    calls = []

    def fake_update(msg, stage=None, fraction=None):
        calls.append((msg, stage))

    # Freeze the clock so both lines fall inside one throttle window.
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.time.monotonic", lambda: 1000.0
    )
    on_line = manager._make_output_progress_updater(
        "Zahak", fake_update, download_stage=InstallStage.FETCHING_NETS,
    )
    on_line("compiling pkg a")
    on_line("compiling pkg b")  # same window -> throttled away
    on_line("   ")  # blank -> ignored

    assert len(calls) == 1
    msg, stage = calls[0]
    assert msg == "Building Zahak: compiling pkg a"
    assert stage == InstallStage.BUILDING
