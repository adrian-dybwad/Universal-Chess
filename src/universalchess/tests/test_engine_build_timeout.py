"""Tests for when a source build is allowed to keep running, and when it is killed.

Why this module exists: installing Rodent IV on a Raspberry Pi Zero W failed with
"Build timed out after 600s" at 95%. The build was not broken -- measured on that
board it compiles successfully in 13m51s (831s). It was killed at 72% of the way
through healthy work by a hand-written per-engine budget.

The budget could not be repaired by picking bigger numbers. On one board the
catalog's estimates are wrong in both directions: Rodent IV took 1.73x its
8-minute estimate while Claudia took 0.46x its 3-minute estimate. Any fixed
figure is therefore either too small for some engine or too generous to detect a
real hang.

So a build is killed for lack of *progress*, not for taking time. Liveness comes
from the build's own process tree (see ``services.build_progress``), with an
absolute ceiling retained only as a backstop against a build that spins forever
while consuming CPU -- the one pathology progress cannot distinguish from work.

The boundary faked here is the process-table reader. The build itself is a real
subprocess, so these tests exercise the real streaming, killing and error paths.
"""

import subprocess
import time

import pytest

from universalchess.managers.engine_manager import (
    BUILD_CEILING_HEADROOM,
    BUILD_STALL_SECONDS,
    ENGINES,
    MIN_BUILD_CEILING_SECONDS,
    EngineDefinition,
    EngineManager,
    build_ceiling_seconds,
)
from universalchess.services.build_progress import ProcessInfo, ReportedBuildProgress

# The budget Rodent IV had when the Pi Zero W install was killed mid-compile.
_FIELD_FAILURE_TIMEOUT_SECONDS = 600

# Measured on the Pi Zero W: successful Rodent IV compile, and its clone.
_RODENT_MEASURED_BUILD_SECONDS = 831
_RODENT_ESTIMATE_SECONDS = 8 * 60

# Engines the installer actually compiles; bundled/system-package entries have no
# build step, so no build budget applies to them.
_SOURCE_BUILT_ENGINES = sorted(
    name for name, engine in ENGINES.items() if engine.build_commands
)

def _live_process_table(root_pid: int, cpu_ticks: int) -> dict:
    """A one-process tree, parented to ``root_pid``, that has consumed CPU.

    The reader boundary is asked for the tree of a given pid, which is what lets a
    fake describe a descendant of a subprocess whose pid it cannot know in advance.
    """
    child_pid = root_pid + 1
    return {
        child_pid: ProcessInfo(
            pid=child_pid, ppid=root_pid, comm="cc1plus", cpu_ticks=cpu_ticks,
            args=("/usr/libexec/gcc/cc1plus", "-quiet", "src/search.cpp"),
        )
    }


def _engine(*, estimated_install_minutes: int) -> EngineDefinition:
    """A throwaway source-built engine carrying only the estimate under test."""
    return EngineDefinition(
        name="dummy",
        display_name="Dummy",
        summary="",
        description="",
        repo_url=None,
        build_commands=["true"],
        binary_path="dummy",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=[],
        estimated_install_minutes=estimated_install_minutes,
    )


def _manager(tmp_path) -> EngineManager:
    manager = EngineManager(engines_dir=str(tmp_path / "engines"))
    manager.build_tmp = tmp_path / "build"
    return manager


class TestCeiling:
    """The absolute backstop, derived from the catalog estimate."""

    @pytest.mark.parametrize("engine_name", _SOURCE_BUILT_ENGINES)
    def test_every_source_built_engine_gets_slow_board_headroom(self, engine_name):
        """Each engine's ceiling is at least the headroom multiple of its estimate.

        Why this test exists: the ceiling exists to catch a build that spins
        forever, not to cap a slow one. A ceiling close to an engine's own
        estimate kills it on any board slower than the one the estimate was
        written on -- which is what happened to Rodent IV. Deriving the ceiling
        from the estimate makes the headroom uniform across the catalog instead of
        thirteen independent hand-written numbers.

        How a regression manifests: reintroducing a per-engine budget that
        undercuts the headroom (Zahak's old 600s against a 600s estimate was the
        worst case, at 1.00x) fails here for that engine, before it fails on a
        board.
        """
        engine = ENGINES[engine_name]
        estimate_seconds = engine.estimated_install_minutes * 60

        assert build_ceiling_seconds(engine) >= estimate_seconds * BUILD_CEILING_HEADROOM

    def test_rodent_iv_ceiling_covers_the_measured_build(self):
        """Rodent IV's ceiling exceeds the 831s the board actually needs.

        Why this test exists: this is the reported bug, pinned to the measurement
        rather than to a ratio. The board compiles Rodent IV in 831s and the old
        budget was 600s. Named separately from the catalog-wide check so a future
        headroom change that still satisfies the generic invariant cannot silently
        reopen this exact failure.

        How a regression manifests: pinning Rodent IV back to 600s makes the
        ceiling fall below the measured build time, so the install fails on
        hardware that is working correctly.
        """
        ceiling = build_ceiling_seconds(ENGINES["rodentIV"])

        assert ceiling > _FIELD_FAILURE_TIMEOUT_SECONDS
        assert ceiling > _RODENT_MEASURED_BUILD_SECONDS

    @pytest.mark.parametrize("estimated_install_minutes", [0, 1])
    def test_tiny_estimate_still_gets_the_floor_ceiling(self, estimated_install_minutes):
        """A near-zero estimate does not produce a near-zero ceiling.

        Why: the ceiling is a multiple of a hand-written estimate, so a catalog
        entry that omits or understates the estimate would otherwise get a ceiling
        of zero (an install that fails instantly) or a minute or two (an install
        that cannot finish on a slow board). The floor makes an inaccurate
        estimate cost extra patience, never a failed install.

        How a regression manifests: dropping the floor makes the 0-minute case
        return 0, killing the build before it emits a single line.
        """
        engine = _engine(estimated_install_minutes=estimated_install_minutes)

        assert build_ceiling_seconds(engine) >= MIN_BUILD_CEILING_SECONDS

    def test_ceiling_scales_with_the_estimate(self):
        """A longer-estimated build gets a proportionally longer ceiling.

        Why: the floor must not flatten the whole catalog to one value -- Maia's
        hour-long lc0 compile needs far more than Claudia's 83 seconds. This pins
        that the derivation stays proportional above the floor.

        How a regression manifests: replacing the derivation with a flat constant
        still satisfies the floor and headroom checks above, but fails here
        because both estimates would map to the same ceiling.
        """
        floor_minutes = MIN_BUILD_CEILING_SECONDS // 60
        short = build_ceiling_seconds(_engine(estimated_install_minutes=floor_minutes))
        long = build_ceiling_seconds(_engine(estimated_install_minutes=floor_minutes * 6))

        assert long == 6 * short

    def test_engine_definitions_no_longer_carry_a_hand_set_budget(self):
        """No engine declares its own build timeout.

        Why this test exists: thirteen independently maintained budget numbers are
        what produced a 600s cap on an 831s build, and a per-engine override would
        quietly reinstate that failure mode for one engine while every other test
        here still passes.

        How a regression manifests: adding a ``build_timeout`` field back to any
        catalog entry fails here, naming the engine.
        """
        offenders = [
            name for name in _SOURCE_BUILT_ENGINES
            if getattr(ENGINES[name], "build_timeout", None) is not None
        ]

        assert offenders == []


class TestStallDetection:
    """Killing a build for lack of progress rather than for elapsed time."""

    def test_a_build_making_progress_outlives_the_old_fixed_budget(self, tmp_path):
        """A slow but working build is not killed when its stall window expires.

        Why this test exists: this is the field failure, expressed as behaviour
        instead of arithmetic. The build here runs longer than its stall window
        and reports no output at all -- exactly Rodent IV's shape, which is silent
        for 13m51s -- while its process tree keeps consuming CPU. It must survive.

        How a regression manifests: reverting to a wall-clock deadline (or
        requiring output as the liveness signal) kills this command and the test
        fails with TimeoutExpired, the same way the Pi Zero W install did.
        """
        manager = _manager(tmp_path)
        ticks = {"value": 0}

        def read_processes(root_pid):
            ticks["value"] += 100
            return _live_process_table(root_pid, ticks["value"])

        started = time.monotonic()
        returncode, _tail = manager._run_monitored_command(
            "sleep 3", tmp_path, on_line=lambda _line: None,
            stall_seconds=1, ceiling_seconds=60, read_processes=read_processes,
        )

        assert returncode == 0
        # The command really did outlive its stall window rather than exiting early.
        assert time.monotonic() - started >= 3

    def test_a_build_consuming_nothing_is_killed_once_the_stall_window_passes(self, tmp_path):
        """A wedged build fails promptly instead of running to the ceiling.

        Why: replacing the wall-clock cap must not mean waiting hours on a hang.
        A tree that burns no CPU and prints nothing is not working, and the stall
        window is what turns that into a prompt, accurate failure.

        How a regression manifests: dropping stall detection lets this sleep run
        to the ceiling, so the test fails by not raising.
        """
        manager = _manager(tmp_path)

        with pytest.raises(subprocess.TimeoutExpired):
            manager._run_monitored_command(
                "sleep 60", tmp_path, on_line=lambda _line: None,
                stall_seconds=1, ceiling_seconds=300,
                read_processes=lambda root_pid: _live_process_table(root_pid, cpu_ticks=7),
            )

    def test_ceiling_stops_a_build_that_spins_forever(self, tmp_path):
        """Endless CPU burn is still bounded.

        Why: consumed CPU proves activity, not usefulness. A compiler stuck in an
        infinite loop refreshes liveness forever, so the ceiling is the only thing
        that ends it. This is why the backstop is retained rather than removed
        along with the old fixed budget.

        How a regression manifests: removing the ceiling makes this run until the
        test suite is killed, since the injected reader reports progress forever.
        """
        manager = _manager(tmp_path)
        ticks = {"value": 0}

        def read_processes(root_pid):
            ticks["value"] += 1000
            return _live_process_table(root_pid, ticks["value"])

        with pytest.raises(subprocess.TimeoutExpired):
            manager._run_monitored_command(
                "sleep 60", tmp_path, on_line=lambda _line: None,
                stall_seconds=30, ceiling_seconds=1, read_processes=read_processes,
            )

    def test_stall_window_clears_the_worst_measured_quiet_stretch(self):
        """The window is a wide multiple of the longest silence ever measured.

        Why this test exists: the window must be justified by what a healthy build
        actually does, and a long translation unit is not a silent one -- during
        CT800's four-minute bookdata.c compile the process is pinned to the core,
        so CPU time keeps climbing. Measured on the Pi Zero W across 21 minutes of
        compiling, the longest stretch with no signal of any kind was 7.0s (CT800;
        Rodent IV's 13m31s build, which prints one line in total, peaked at 3.1s).
        The window is set well above that, not above a unit's duration.

        How a regression manifests: tightening the window toward the measured
        silence removes the margin that absorbs a slow poll or a scheduling
        hiccup, and healthy builds start being killed again.
        """
        measured_worst_quiet_stretch_seconds = 7
        required_margin = 10

        assert measured_worst_quiet_stretch_seconds * required_margin <= BUILD_STALL_SECONDS


class TestAdaptiveCeiling:
    """The backstop as the runner applies it, against a real subprocess.

    Why this class exists: Maia's Pi Zero W build was killed at exactly its 14400s
    ceiling while healthy, at module 177 of 259 with hours of work left. The stall
    window had correctly stayed quiet the whole time. The ceiling was still a
    multiple of a catalog estimate, so on a board six times slower than the one
    that estimate came from it killed working builds -- the original bug, moved
    from ten minutes to four hours.
    """

    def test_a_build_that_keeps_finishing_work_outlives_its_base_ceiling(self, tmp_path):
        """Reported progress buys the time the base ceiling did not allow.

        Why this test exists: this is the field failure end to end. The base
        ceiling is one second and the command runs for three, so it survives only
        if completing units extends the deadline.

        How a regression manifests: a fixed deadline raises TimeoutExpired here
        after one second, which is what the board did after four hours.
        """
        manager = _manager(tmp_path)
        units = {"done": 0}

        def read_reported_progress():
            units["done"] += 1
            return ReportedBuildProgress(
                units_finished=units["done"], units_total=100,
                fraction=units["done"] / 100, eta_seconds=30,
            )

        started = time.monotonic()
        returncode, _tail = manager._run_monitored_command(
            "sleep 3", tmp_path, on_line=lambda _line: None,
            stall_seconds=30, ceiling_seconds=1,
            read_processes=lambda root_pid: _live_process_table(root_pid, cpu_ticks=1),
            read_reported_progress=read_reported_progress,
        )

        assert returncode == 0
        assert time.monotonic() - started >= 3

    def test_a_build_that_stops_finishing_work_is_still_killed(self, tmp_path):
        """Extending the ceiling must not disable it.

        Why this test exists: the ceiling's whole purpose is the one pathology no
        liveness signal can detect -- a build consuming CPU forever without ever
        completing. Here the process tree keeps burning CPU (so the stall window
        never fires) and the reported fraction never moves, so no further time is
        granted and the existing deadline must arrive.

        How a regression manifests: granting time per sample rather than per unit
        of finished work lets this ``sleep 60`` run to completion, and the test
        fails by not raising.
        """
        manager = _manager(tmp_path)
        ticks = {"value": 0}

        def read_processes(root_pid):
            ticks["value"] += 1000
            return _live_process_table(root_pid, ticks["value"])

        frozen = ReportedBuildProgress(
            units_finished=50, units_total=100, fraction=0.5, eta_seconds=1,
        )

        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            manager._run_monitored_command(
                "sleep 60", tmp_path, on_line=lambda _line: None,
                stall_seconds=30, ceiling_seconds=1, read_processes=read_processes,
                read_reported_progress=lambda: frozen,
            )

        # Bounded by the single grant its one reported fraction earned, not by the
        # 60s the command would otherwise run for.
        assert time.monotonic() - started < 30


class TestFailureMessage:
    """What the user is told when a build really is killed."""

    def test_timed_out_build_reports_what_it_was_building(self, tmp_path, monkeypatch):
        """The failure message carries the last build output, not just a number.

        Why this test exists: "Build timed out after 600s" said nothing about
        whether the compiler was working or wedged, so the field report could not
        be diagnosed from the message alone. ``_run_monitored_command`` already
        retains an output tail; on timeout it was discarded.

        How a regression manifests: dropping the tail from the error leaves the
        user with a bare number again and this assertion fails.
        """
        engine = _engine(estimated_install_minutes=8)
        manager = _manager(tmp_path)

        def fake_run_monitored_command(cmd, cwd, on_line, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, BUILD_STALL_SECONDS, output="cc -c search.cpp\ncc -c eval.cpp"
            )

        monkeypatch.setattr(manager, "_run_monitored_command", fake_run_monitored_command)

        result = manager._install_from_source(engine, lambda *args, **kwargs: None)

        assert result is False
        assert "eval.cpp" in manager.get_install_error()
