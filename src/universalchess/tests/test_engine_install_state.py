"""Tests for the engine install state store.

This module owns the single source of truth for engine-install progress: the
structured stage, a derived percent (stage-weighted with a time-based creep
during the build and a real byte fraction during a download), and on-disk
persistence so the UI survives a process/board restart and can offer a manual
resume of an interrupted install.

Each test states the behavior it guards and how a regression would surface.
"""

import json
import time

import pytest

from universalchess.services.engine_install_state import (
    InstallStage,
    InstallState,
    InstallStateStore,
    compute_percent,
)


def _state(stage, *, stage_started_at=0.0, download_fraction=None,
           estimated_seconds=480.0, percent_snapshot=None):
    """Build an InstallState for percent tests with sensible defaults."""
    return InstallState(
        engine="rodentIV",
        display_name="Rodent IV",
        stage=stage,
        message="",
        started_at=0.0,
        stage_started_at=stage_started_at,
        updated_at=stage_started_at,
        estimated_seconds=estimated_seconds,
        download_fraction=download_fraction,
        active=stage not in (
            InstallStage.COMPLETED, InstallStage.FAILED,
            InstallStage.INTERRUPTED, InstallStage.CANCELLED,
        ),
        result=None,
        percent_snapshot=percent_snapshot,
    )


class TestComputePercent:
    """The pure percent mapping that drives the progress bar."""

    # Why: each point stage must report its fixed band value so the bar advances
    # monotonically through the source/prebuilt flows. Regression: a wrong band
    # constant makes the bar jump backwards or stall at the wrong place.
    @pytest.mark.parametrize("stage,expected", [
        (InstallStage.STARTING, 2),
        (InstallStage.CHECKING_PREBUILT, 5),
        (InstallStage.INSTALLING_DEPS, 15),
        (InstallStage.CLONING, 30),
        (InstallStage.INSTALLING_FILES, 97),
        (InstallStage.COMPLETED, 100),
    ])
    def test_point_stages_report_fixed_band(self, stage, expected):
        assert compute_percent(_state(stage), now=0.0) == expected

    # Why: a download exposes a real byte fraction, which must map across the
    # download band [5, 85]. Regression: if the fraction is ignored the bar would
    # sit at 5 for the whole download.
    @pytest.mark.parametrize("fraction,expected", [
        (0.0, 5),
        (0.5, 45),
        (1.0, 85),
    ])
    def test_downloading_maps_real_fraction(self, fraction, expected):
        s = _state(InstallStage.DOWNLOADING, download_fraction=fraction)
        assert compute_percent(s, now=0.0) == expected

    # Why: the build emits no real percent, so the bar must creep over the build
    # band [35, 95] using elapsed/estimated time. Regression: no creep means the
    # bar freezes at 35 for the entire (longest) phase.
    def test_building_creeps_with_elapsed_time(self):
        s = _state(InstallStage.BUILDING, stage_started_at=0.0, estimated_seconds=480.0)
        assert compute_percent(s, now=0.0) == 35       # just started
        assert compute_percent(s, now=240.0) == 65     # halfway -> midpoint of band
        assert compute_percent(s, now=480.0) == 95     # at estimate -> band ceiling

    # Why: the build creep must never reach 100 before completion, otherwise a
    # slow build shows a finished bar while still working. Regression: missing cap
    # lets elapsed > estimate push the percent past the band ceiling.
    def test_building_capped_at_band_ceiling(self):
        s = _state(InstallStage.BUILDING, stage_started_at=0.0, estimated_seconds=480.0)
        assert compute_percent(s, now=100_000.0) == 95

    # Why: a failed/interrupted/cancelled install must freeze the bar where it
    # stopped (the snapshot), not recompute or reset. Regression: returning a live
    # value would make an interrupted build keep creeping while idle.
    @pytest.mark.parametrize("stage", [
        InstallStage.FAILED, InstallStage.INTERRUPTED, InstallStage.CANCELLED,
    ])
    def test_terminal_stages_hold_snapshot(self, stage):
        s = _state(stage, percent_snapshot=62)
        assert compute_percent(s, now=999_999.0) == 62


class TestInstallStateStore:
    """The persisted store used by the web layer and install thread."""

    def _store(self, tmp_path):
        return InstallStateStore(tmp_path / "engine_install_state.json")

    # Why: starting an install must create an active STARTING state and write it
    # to disk so a page load (even before the first progress) sees it. Regression:
    # not persisting means a reload during startup shows nothing.
    def test_start_persists_active_state(self, tmp_path):
        store = self._store(tmp_path)
        store.start("rodentIV", "Rodent IV", estimated_seconds=480.0)

        status = store.status_dict()
        assert status["active"] is True
        assert status["installing"] is True
        assert status["engine"] == "rodentIV"
        assert status["stage"] == InstallStage.STARTING.value
        assert status["interrupted"] is False

        on_disk = json.loads((tmp_path / "engine_install_state.json").read_text())
        assert on_disk["engine"] == "rodentIV"
        assert on_disk["active"] is True

    # Why: a stage update must change stage and reset the stage clock so the build
    # creep measures elapsed time within the build, not since install start.
    # Regression: not resetting stage_started_at makes the build bar jump.
    def test_update_changes_stage_and_resets_stage_clock(self, tmp_path):
        store = self._store(tmp_path)
        store.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        store.update(InstallStage.BUILDING, "Building Rodent IV...")

        s = store.get()
        assert s.stage == InstallStage.BUILDING
        assert s.message == "Building Rodent IV..."
        # stage_started_at is recent (within the test window)
        assert abs(s.stage_started_at - time.time()) < 5

    # Why: a download fraction must be carried into status so the bar reflects
    # bytes received. Regression: dropping the fraction stalls the bar at 5.
    def test_update_carries_download_fraction(self, tmp_path):
        store = self._store(tmp_path)
        store.start("berserk", "Berserk", estimated_seconds=900.0)
        store.update(InstallStage.DOWNLOADING, "Downloading Berserk... 50%", download_fraction=0.5)

        assert store.status_dict()["percent"] == 45

    # Why: a successful finish must report 100, mark inactive, and record the
    # result so the UI can show success and re-enable actions. Regression: leaving
    # active True would wedge the UI in "installing" forever.
    def test_finish_success(self, tmp_path):
        store = self._store(tmp_path)
        store.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        store.finish(success=True)

        status = store.status_dict()
        assert status["active"] is False
        assert status["stage"] == InstallStage.COMPLETED.value
        assert status["percent"] == 100
        assert status["result"] == {"success": True, "error": None}

    # Why: a failed finish must freeze the percent at where it stopped (snapshot)
    # and record the error. Regression: recomputing would reset/inflate the bar.
    def test_finish_failure_snapshots_percent(self, tmp_path):
        store = self._store(tmp_path)
        store.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        store.update(InstallStage.CLONING, "Cloning...")
        store.finish(success=False, error="Build failed")

        status = store.status_dict()
        assert status["active"] is False
        assert status["stage"] == InstallStage.FAILED.value
        assert status["percent"] == 30  # held at the cloning band, not reset
        assert status["result"] == {"success": False, "error": "Build failed"}

    # Why: a process/board restart leaves an active state on disk with no live
    # thread; reconcile must flip it to INTERRUPTED (inactive) so the UI offers
    # manual Resume/Cancel. Regression: without reconcile the UI would poll an
    # install that is no longer running and wait forever.
    def test_reconcile_marks_orphaned_active_as_interrupted(self, tmp_path):
        path = tmp_path / "engine_install_state.json"
        writer = InstallStateStore(path)
        writer.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        writer.update(InstallStage.BUILDING, "Building Rodent IV...")

        # Fresh process: a new store loads the file and reconciles.
        fresh = InstallStateStore(path)
        result = fresh.reconcile_interrupted()

        assert result is not None
        status = fresh.status_dict()
        assert status["active"] is False
        assert status["interrupted"] is True
        assert status["stage"] == InstallStage.INTERRUPTED.value
        assert status["engine"] == "rodentIV"

    # Why: reconcile must be a no-op when the persisted install already finished,
    # so a completed/failed result is not rewritten to interrupted on restart.
    # Regression: a too-broad reconcile would mark finished installs interrupted.
    def test_reconcile_ignores_finished_state(self, tmp_path):
        path = tmp_path / "engine_install_state.json"
        writer = InstallStateStore(path)
        writer.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        writer.finish(success=True)

        fresh = InstallStateStore(path)
        assert fresh.reconcile_interrupted() is None
        assert fresh.status_dict()["stage"] == InstallStage.COMPLETED.value

    # Why: clearing must remove both the in-memory state and the file so a
    # dismissed/cancelled state does not reappear on the next status poll or
    # restart. Regression: a lingering file would resurrect a dismissed banner.
    def test_clear_removes_state_and_file(self, tmp_path):
        path = tmp_path / "engine_install_state.json"
        store = InstallStateStore(path)
        store.start("rodentIV", "Rodent IV", estimated_seconds=480.0)
        store.clear()

        assert store.get() is None
        assert not path.exists()
        assert store.status_dict()["active"] is False

    # Why: an idle store (no install ever started) must still return a well-formed
    # status so the endpoint and frontend can render the default state. Regression:
    # returning None/raising would break the status endpoint on a clean board.
    def test_status_dict_idle_when_no_state(self, tmp_path):
        store = self._store(tmp_path)
        status = store.status_dict()
        assert status["active"] is False
        assert status["installing"] is False
        assert status["engine"] is None
        assert status["percent"] == 0
        assert status["interrupted"] is False
