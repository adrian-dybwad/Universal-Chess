"""Tests that the apt dependency install is visible and is not on a fixed budget.

A Maia install on a Pi Zero W failed with::

    Failed to install Maia. Command timed out: ['sudo', '-n',
    '/opt/universalchess/scripts/uc-engine-deps', 'build-essential', 'git',
    'clang', 'meson', 'ninja-build', 'pkg-config', 'libopenblas-dev',
    'zlib1g-dev', 'wget']

Two separate faults produced that. The step ran with ``capture_output=True``, so
nothing it printed reached the UI until it returned -- and it never returned. And
it was bounded by a flat 300 seconds, which is not enough to fetch and unpack a
clang toolchain over a slow link on one ARMv6 core, so healthy work was killed.
These tests pin both fixes.
"""

from __future__ import annotations

import subprocess

import pytest

from universalchess.managers import engine_manager as engine_manager_module
from universalchess.managers.engine_manager import (
    DEPS_CEILING_SECONDS,
    DEPS_STALL_SECONDS,
    ENGINE_DEPS_HELPER,
    EngineManager,
)
from universalchess.services.engine_install_state import (
    InstallStage,
    InstallStateStore,
    compute_percent,
)

# The real sequence a dependency install produces: the helper's own log lines,
# its phase markers, and apt's APT::Status-Fd reports interleaved.
_APT_SESSION = [
    "uc-engine-deps: missing packages: clang meson",
    "UC_DEPS_PHASE phase=update",
    "dlstatus:1:0.0000:Retrieving file 1 of 4",
    "dlstatus:4:100.0000:Retrieving file 4 of 4",
    "uc-engine-deps: installing: clang meson",
    "UC_DEPS_PHASE phase=install",
    "dlstatus:1:25.0000:Retrieving file 1 of 8",
    "dlstatus:8:100.0000:Retrieving file 8 of 8",
    "pmstatus:clang:10.0000:Unpacking clang (1:14.0-55.7~deb12u1)",
    "pmstatus:clang:70.0000:Configuring clang (1:14.0-55.7~deb12u1)",
    "pmstatus:clang:100.0000:Installed clang",
]


class _Recorder:
    """Captures every progress update the install publishes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, message, stage=None, download_fraction=None, **kwargs) -> None:
        self.calls.append({
            "message": message,
            "stage": stage,
            "deps_fraction": kwargs.get("deps_fraction"),
        })

    @property
    def messages(self) -> list[str]:
        return [call["message"] for call in self.calls]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """An EngineManager whose progress updates are not throttled away."""
    # The throttle exists to spare the SD card, but it would drop most of a short
    # scripted session and hide exactly what these tests assert.
    monkeypatch.setattr(
        engine_manager_module, "_BUILD_PROGRESS_THROTTLE_SECONDS", 0.0
    )
    return EngineManager(engines_dir=str(tmp_path))


class TestDependencyOutputReachesTheUser:
    """Every line the dependency install prints must be shown."""

    def test_apt_progress_becomes_visible_progress(self, manager):
        """apt's reported activity and percentage drive the banner and the bar.

        Why this test exists: the step previously captured its output and showed
        none of it, which is what made a multi-minute toolchain install look like
        a hang. apt reports what it is doing and how far along it is, so both must
        reach the user.

        How a regression manifests: returning to a captured run leaves the banner
        empty and the bar pinned at the band floor for the whole step.
        """
        recorder = _Recorder()
        on_line = manager._make_deps_progress_updater("Maia", recorder)

        for line in _APT_SESSION:
            on_line(line)

        fractions = [
            call["deps_fraction"] for call in recorder.calls
            if call["deps_fraction"] is not None
        ]
        assert fractions == sorted(fractions), fractions
        assert fractions[-1] == pytest.approx(1.0)
        assert any("Unpacking clang" in message for message in recorder.messages)
        assert all(
            call["stage"] is InstallStage.INSTALLING_DEPS for call in recorder.calls
        )

    def test_plain_helper_output_is_shown_too(self, manager):
        """Lines that are not apt status reports still reach the banner.

        Why this test exists: the user asked for everything the console produces to
        be surfaced. The helper's own log lines say useful things ("missing
        packages: ...") that apt's status stream does not carry.

        How a regression manifests: only status-formatted lines appear and the
        helper's explanation of what it is doing is silently dropped.
        """
        recorder = _Recorder()
        on_line = manager._make_deps_progress_updater("Maia", recorder)

        on_line("uc-engine-deps: missing packages: clang meson")

        assert "missing packages: clang meson" in recorder.messages[-1]
        # The helper tags its own lines; repeating that inside a message already
        # naming the step reads as "Installing Maia dependencies: uc-engine-deps:".
        assert "uc-engine-deps:" not in recorder.messages[-1]

    def test_a_phase_with_no_reading_yet_names_the_phase(self, manager):
        """Starting a phase says what it is doing, not a placeholder.

        Why this test exists: the phase marker arrives before apt's first
        measurement, so there is a real gap with a known step but no percentage.
        Filling it with "working" wastes the one line the user is watching.

        How a regression manifests: the banner reads "Installing Maia
        dependencies: working" for the whole gap before apt's first report.
        """
        recorder = _Recorder()
        on_line = manager._make_deps_progress_updater("Maia", recorder)

        on_line("UC_DEPS_PHASE phase=update")

        assert "refreshing package lists" in recorder.messages[-1]

    def test_internal_phase_marker_is_not_shown_verbatim(self, manager):
        """The phase sentinel is consumed rather than printed.

        Why this test exists: the marker is a contract between the helper and this
        reader. Showing "UC_DEPS_PHASE phase=install" in the banner exposes an
        internal protocol string to the user.

        How a regression manifests: raw sentinel text appears in the install UI.
        """
        recorder = _Recorder()
        on_line = manager._make_deps_progress_updater("Maia", recorder)

        for line in _APT_SESSION:
            on_line(line)

        assert not any("UC_DEPS_PHASE" in message for message in recorder.messages)


class TestReadingsSurviveTheRealCallbackChain:
    """The whole path from a producer to the persisted state, with real signatures.

    The recorders elsewhere in this file accept ``**kwargs``, which is convenient
    but hides the failure that actually happened: the installer's own
    ``update_progress`` named its measurement arguments explicitly, so publishing a
    new one raised ``TypeError: update_progress() got an unexpected keyword
    argument 'deps_fraction'`` and killed the install at its first apt report.
    These tests use the real objects at every hop.
    """

    def test_a_dependency_reading_reaches_the_persisted_state(self, manager, tmp_path):
        """apt's progress survives publisher, web callback and store untouched.

        Why this test exists: three separate layers each listed the readings they
        forwarded, and all three had to agree. A reading added to the producer but
        missing from any one of them crashed the install rather than degrading.

        How a regression manifests: a TypeError from the progress callback aborts
        the dependency step -- the exact failure this chain is built to avoid.
        """
        store = InstallStateStore(path=tmp_path / "state.json")
        store.start("maia", "Maia", estimated_seconds=3600)

        # Exactly what the web layer installs as its callback.
        def on_stage(stage, message, fraction, **measurements):
            store.update(stage, message, fraction, **measurements)

        publish = manager._make_progress_publisher(None, on_stage, "Progress")
        on_line = manager._make_deps_progress_updater("Maia", publish)

        on_line("UC_DEPS_PHASE phase=install")
        on_line("pmstatus:clang:50.0000:Unpacking clang-14")

        state = store.get()
        assert state.stage is InstallStage.INSTALLING_DEPS
        assert state.deps_fraction is not None
        # Inside the dependency band, and moved off its floor by the reading.
        percent = compute_percent(state, now=state.updated_at)
        assert 10 < percent <= 28, percent

    def test_a_build_reading_reaches_the_persisted_state(self, manager, tmp_path):
        """The same chain carries observed build progress.

        Why this test exists: the dependency reading was not the first to travel
        this path, and pinning only the newest one would let the next refactor drop
        an older reading silently.

        How a regression manifests: the build bar stops following the compiler and
        falls back to a time-based creep with no error anywhere.
        """
        store = InstallStateStore(path=tmp_path / "state.json")
        store.start("maia", "Maia", estimated_seconds=3600)

        def on_stage(stage, message, fraction, **measurements):
            store.update(stage, message, fraction, **measurements)

        publish = manager._make_progress_publisher(None, on_stage, "Progress")
        publish("half way", InstallStage.BUILDING, build_fraction=0.5,
                build_eta_seconds=120)

        state = store.get()
        assert state.build_fraction == pytest.approx(0.5)
        assert state.build_eta_seconds == 120

    def test_a_message_only_update_keeps_the_stage_it_is_in(self, manager, tmp_path):
        """Publishing without a stage does not reset the stage.

        Why this test exists: most lines a build prints carry no stage, and the
        publisher is what remembers it. Losing that would send every ordinary log
        line back to the starting stage and drag the bar to the bottom.

        How a regression manifests: the bar collapses to 2% on every log line.
        """
        store = InstallStateStore(path=tmp_path / "state.json")
        store.start("maia", "Maia", estimated_seconds=3600)

        def on_stage(stage, message, fraction, **measurements):
            store.update(stage, message, fraction, **measurements)

        publish = manager._make_progress_publisher(None, on_stage, "Progress")
        publish("starting the build", InstallStage.BUILDING)
        publish("some ordinary compiler chatter")

        assert store.get().stage is InstallStage.BUILDING


class TestDependencyLimits:
    """What ends a dependency install that is not making progress."""

    def test_the_command_is_run_as_argv_not_through_a_shell(self, manager, monkeypatch):
        """The deps helper is executed directly, never via a shell.

        Why this test exists: the runner takes a string for build recipes and runs
        those through a shell. Package names must not travel that path -- the whole
        point of the pinned helper is a narrow, non-shell privileged surface.

        How a regression manifests: joining the argv into a string would put
        package arguments through shell parsing under sudo.
        """
        seen = {}

        def fake_monitored(cmd, cwd, on_line, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return 0, ""

        monkeypatch.setattr(manager, "_run_monitored_command", fake_monitored)

        manager._install_packages(["clang"], "Maia", _Recorder())

        assert isinstance(seen["cmd"], list)
        assert seen["cmd"][:2] == ["sudo", "-n"]
        assert seen["cmd"][2].endswith(ENGINE_DEPS_HELPER.name)
        assert seen["cmd"][-1] == "clang"

    def test_limits_are_activity_based_not_the_old_flat_budget(self, manager, monkeypatch):
        """The install is bounded by stall detection, not a fixed 300 seconds.

        Why this test exists: this is the exact failure the user hit. 300 seconds
        cannot fetch and unpack a clang toolchain on a Zero W, so a healthy install
        was killed. The limits must be the activity-based ones, and the backstop
        must be far above the old budget.

        How a regression manifests: reinstating a short flat timeout kills working
        dependency installs on slow boards again.
        """
        seen = {}

        def fake_monitored(cmd, cwd, on_line, **kwargs):
            seen.update(kwargs)
            return 0, ""

        monkeypatch.setattr(manager, "_run_monitored_command", fake_monitored)

        manager._install_packages(["clang"], "Maia", _Recorder())

        assert seen["stall_seconds"] == DEPS_STALL_SECONDS
        assert seen["ceiling_seconds"] == DEPS_CEILING_SECONDS
        # The budget that killed the field install, comfortably cleared.
        assert DEPS_CEILING_SECONDS > 300

    def test_the_helpers_dpkg_lock_wait_fits_inside_the_stall_window(self):
        """A dependency install is not declared stalled while it waits for dpkg.

        Why this test exists: the helper waits up to 120 seconds for another
        package operation to release the dpkg frontend lock before it starts. If
        the stall window were shorter than that wait, a perfectly normal queued
        install would be killed for being quiet.

        How a regression manifests: tightening the stall window below the lock wait
        makes concurrent installs fail intermittently and unreproducibly.
        """
        helper_lock_wait_seconds = 60 * 2  # 60 iterations of `sleep 2` in the helper

        assert helper_lock_wait_seconds < DEPS_STALL_SECONDS

    def test_giving_up_reports_what_apt_was_last_doing(self, manager, monkeypatch):
        """A stalled install surfaces the output tail instead of a bare timeout.

        Why this test exists: "Command timed out: ['sudo', '-n', ...]" was the
        entire failure report, which cannot distinguish a wedged install from a
        slow one. The tail names the last thing apt printed.

        How a regression manifests: the timeout message loses the diagnostic and
        the next failure is undiagnosable again.
        """
        def fake_monitored(cmd, cwd, on_line, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, DEPS_STALL_SECONDS, output="Unpacking clang (1:14.0)"
            )

        monkeypatch.setattr(manager, "_run_monitored_command", fake_monitored)

        returncode, tail = manager._install_packages(["clang"], "Maia", _Recorder())

        assert returncode != 0
        assert "Unpacking clang" in tail
