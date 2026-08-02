"""Tests for the single output-line reader shared by installs and repairs.

Why this module exists: an engine's build output can carry three different kinds of
information on the same stream -- a downloader's byte counts, a build's own report of
how many units it has finished, and ordinary log chatter. Both the fresh-install path
and the in-place repair path read that stream, and before this was unified they read
it differently: repair understood download bytes while install did not, so the nine
weight files a fresh Maia install fetches showed no byte progress at all.

One reader serves both so the two cannot drift apart, and so a stream that mixes
downloads and compilation (which is exactly what build-maia.sh produces) is reported
correctly whichever path is running it.

Stage choice is the subtle part and is asserted here rather than left to inspection:
a repair is nothing but a download, so its bytes drive the wide DOWNLOADING band,
whereas a fresh install downloads nets only after the build has finished, so the same
bytes must drive a band above BUILDING's ceiling or the bar would retreat.
"""

import pytest

from universalchess.managers import engine_manager as engine_manager_module
from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_state import InstallStage

# wget --progress=dot output, in the shape verified against real output in
# test_download_progress.py: the total arrives once up front, and each data line
# begins with the KiB transferred BEFORE that line's dots.
_TOTAL_BYTES = 20 * 1024 * 1024
_WGET_LENGTH_LINE = f"Length: {_TOTAL_BYTES} (20M) [application/octet-stream]"
_WGET_HALFWAY_LINE = "  10240K .......... .......... .......... .......... 50%  120K 9s"
_WGET_EARLY_LINE = "   1024K ..........  5%  120K 9s"
_WGET_SAVED_LINE = (
    f"2026-08-02 10:20:31 (120 KB/s) - 'maia-1100.pb.gz' "
    f"saved [{_TOTAL_BYTES}/{_TOTAL_BYTES}]"
)

# The sentinel build-maia.sh emits per ninja edge, newline-terminated so the
# installer's line reader actually sees it.
_REPORTED_UNITS_DONE = 130
_REPORTED_UNITS_TOTAL = 259
_REPORTED_PROGRESS_LINE = (
    f"UC_BUILD_PROGRESS units={_REPORTED_UNITS_DONE}/{_REPORTED_UNITS_TOTAL}"
)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """Deliver every line's update.

    The updater throttles state writes because each one hits the SD card, which
    would otherwise collapse a multi-line test to a single recorded update and hide
    the per-line behaviour being asserted.
    """
    monkeypatch.setattr(
        engine_manager_module, "_BUILD_PROGRESS_THROTTLE_SECONDS", 0.0
    )


@pytest.fixture
def manager(tmp_path):
    """An EngineManager whose dirs point at a temp location (no real installs)."""
    return EngineManager(engines_dir=str(tmp_path))


class _Recorder:
    """Collects update_progress calls so a test can assert the whole shape."""

    def __init__(self):
        self.calls = []

    def __call__(self, message, stage=None, download_fraction=None, **kwargs):
        self.calls.append({
            "message": message,
            "stage": stage,
            "download_fraction": download_fraction,
            **kwargs,
        })

    @property
    def last(self):
        return self.calls[-1]


def _feed(updater, lines):
    """Push each line through the updater in order."""
    for line in lines:
        updater(line)


class TestSharedDownloadReporting:
    """Byte progress is reported the same way wherever it appears."""

    def test_install_reports_weight_bytes_in_a_forward_band(self, manager):
        """A fresh install's net download reports bytes without the bar retreating.

        Why this test exists: build-maia.sh runs download_weights AFTER build_lc0, so
        reporting those bytes in the DOWNLOADING band (5-85) would drop the bar from
        BUILDING's 95 back to as low as 5. The fetch therefore reports in
        FETCHING_NETS, which sits above BUILDING. This is the case the user asked for
        ("x of y bytes" on a fresh install), and the case that was silently missing.

        How a regression manifests: the stage comes back as DOWNLOADING and a Maia
        install visibly jumps backwards by sixty points once the nets start.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [_WGET_LENGTH_LINE, _WGET_HALFWAY_LINE])

        assert recorder.last["stage"] == InstallStage.FETCHING_NETS
        assert recorder.last["download_fraction"] == pytest.approx(0.5, abs=0.01)
        # The user-visible requirement: x of y, not a bare percentage.
        assert "10.0" in recorder.last["message"]
        assert "20.0" in recorder.last["message"]

    def test_repair_reports_the_same_bytes_in_the_download_band(self, manager):
        """A repair is only a download, so its bytes drive the wide band.

        Why this test exists: repair already worked this way, and unifying the reader
        must not change it. The same two lines must produce the same byte figures
        under a different stage, proving the parsing is shared rather than duplicated.

        How a regression manifests: repair loses byte reporting, or reports it in the
        post-build band where it would barely move.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.DOWNLOADING,
        )

        _feed(updater, [_WGET_LENGTH_LINE, _WGET_HALFWAY_LINE])

        assert recorder.last["stage"] == InstallStage.DOWNLOADING
        assert recorder.last["download_fraction"] == pytest.approx(0.5, abs=0.01)


class TestMultiFileDownloadReporting:
    """What the user sees while a multi-file net fetch runs."""

    def test_internal_sentinel_never_reaches_the_user(self, manager):
        """A file announcement is consumed, not displayed verbatim.

        Why this test exists: the announcement is recognised by the download reader
        but arrives before any Length line, so the byte total is still unknown. An
        implementation that only reports when bytes are known lets the line fall
        through to the plain-chatter branch and prints the raw protocol string
        "UC_DOWNLOAD_FILE index=1 total=10 name=..." into the install banner.

        How a regression manifests: internal sentinel text appears in the UI,
        labelled with the wrong stage.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, ["UC_DOWNLOAD_FILE index=1 total=10 name=maia-1100.pb.gz"])

        for call in recorder.calls:
            assert "UC_DOWNLOAD_FILE" not in call["message"]
        assert recorder.last["stage"] == InstallStage.FETCHING_NETS
        assert "file 1 of 10" in recorder.last["message"]

    def test_percentage_shown_is_the_files_own_progress(self, manager):
        """The percent beside a file name describes that file, not the aggregate.

        Why this test exists: "file 3 of 10 ... 25%" reads unambiguously as "this
        file is a quarter done". Reporting the aggregate there is simply untrue of
        the thing it is attached to, and on the second file onwards the two numbers
        diverge sharply.

        How a regression manifests: the percentage beside the file name disagrees
        with the megabyte figures printed next to it.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [
            "UC_DOWNLOAD_FILE index=1 total=10 name=maia-1100.pb.gz",
            _WGET_LENGTH_LINE,
            _WGET_HALFWAY_LINE,
        ])

        # Half of this file: 10 MiB of its 20 MiB, regardless of the wider job.
        assert "50%" in recorder.last["message"]
        assert "10.0 of 20.0 MB" in recorder.last["message"]

    def test_overall_bar_advances_across_files_without_retreating(self, manager):
        """The bar tracks whole-job completion, counting files not announced bytes.

        Why this test exists: byte totals only cover files already announced, so an
        aggregate fraction hits 1.0 at the end of file 1, drops to 0.5 when file 2 is
        announced, and oscillates all the way down the job. Knowing the file count
        makes real overall progress computable, and the bar must only move forwards.

        How a regression manifests: the fraction falls each time a new file starts.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [
            "UC_DOWNLOAD_FILE index=1 total=10 name=maia-1100.pb.gz",
            _WGET_LENGTH_LINE,
            _WGET_HALFWAY_LINE,
            "UC_DOWNLOAD_FILE index=2 total=10 name=maia-1200.pb.gz",
            _WGET_LENGTH_LINE,
            _WGET_EARLY_LINE,
        ])

        fractions = [
            call["download_fraction"] for call in recorder.calls
            if call["download_fraction"] is not None
        ]
        assert fractions == sorted(fractions), fractions
        # Two files into ten, the job is about a tenth done -- nowhere near full.
        assert fractions[-1] < 0.2

    def test_a_completed_file_neither_rewinds_nor_reads_as_starting(self, manager):
        """Finishing a file moves the bar forward and says so.

        Why this test exists: wget's closing "saved" line retires the file's byte
        counters, so a fraction derived only from "files announced so far plus the
        current file's share" loses that file entirely -- it counts as neither in
        progress nor complete. The bar falls back to the previous file boundary and
        the banner announces the just-finished file as "starting".

        How a regression manifests: at the end of every net the percentage drops and
        the message contradicts what actually happened.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [
            "UC_DOWNLOAD_FILE index=1 total=10 name=maia-1100.pb.gz",
            _WGET_LENGTH_LINE,
            _WGET_HALFWAY_LINE,
        ])
        halfway = recorder.last["download_fraction"]
        _feed(updater, [_WGET_SAVED_LINE])

        assert recorder.last["download_fraction"] >= halfway
        # One of ten nets is genuinely done.
        assert recorder.last["download_fraction"] == pytest.approx(0.1)
        assert "starting" not in recorder.last["message"]

    def test_downloader_chatter_is_not_labelled_as_building(self, manager):
        """Once nets are being fetched, unrecognised lines belong to that phase.

        Why this test exists: wget prints lines this reader does not parse
        ("Resolving github.com...", "HTTP request sent"). During a source install the
        default stage is BUILDING, so those appear as "Building Maia: Resolving
        github.com" while the compile has actually finished -- actively misleading
        about what the install is doing.

        How a regression manifests: download chatter is attributed to the build stage
        and drags the reported stage backwards after the build is done.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [
            "UC_DOWNLOAD_FILE index=1 total=10 name=maia-1100.pb.gz",
            "Resolving github.com (github.com)... 140.82.121.4",
        ])

        assert recorder.last["stage"] == InstallStage.FETCHING_NETS
        assert "Building" not in recorder.last["message"]


class TestSharedBuildReporting:
    """A build's own report of its progress is preferred where it exists."""

    def test_reported_units_drive_the_building_fraction(self, manager):
        """A build that reports exact counts sets build_fraction from them.

        Why this test exists: Maia's compiler cannot be observed per unit (ninja
        invokes clang once per file and clang forks no named backend), so the only
        exact progress available is what the script reports. Without this the bar
        falls back to a time-based creep for a 45-60 minute build.

        How a regression manifests: build_fraction stays absent while the script is
        plainly reporting 130 of 259 units done.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [_REPORTED_PROGRESS_LINE])

        assert recorder.last["stage"] == InstallStage.BUILDING
        assert recorder.last["build_fraction"] == pytest.approx(130 / 259, abs=0.01)
        assert "130" in recorder.last["message"]
        assert "259" in recorder.last["message"]

    def test_ordinary_chatter_updates_only_the_message(self, manager):
        """Log lines stay visible without asserting any progress fraction.

        Why this test exists: most build output is neither a byte count nor a
        progress report, and it must not be silently dropped -- naming what the build
        is doing is what makes a stalled install diagnosable. Equally it must not
        fabricate a fraction, which would displace the time-based fallback.

        How a regression manifests: either the message freezes at the last parsed
        line, or a fraction of 0.0 appears and pins the bar at the band floor.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, ["Configuring meson build"])

        assert "Configuring meson build" in recorder.last["message"]
        assert recorder.last["download_fraction"] is None
        assert recorder.last.get("build_fraction") is None

    def test_a_download_after_a_build_report_does_not_rewind_the_fraction(self, manager):
        """Switching from compiling to downloading keeps each band's own fraction.

        Why this test exists: this is the exact line order a fresh Maia install
        produces -- units reported to completion, then weight bytes from zero. The
        download's 0% must not be applied to the BUILDING band, and the build's 100%
        must not be applied to the fetch band.

        How a regression manifests: one shared fraction is reused across both stages,
        so the bar either snaps back to the build band's floor or jumps the fetch band
        straight to full.
        """
        recorder = _Recorder()
        updater = manager._make_output_progress_updater(
            "Maia", recorder, download_stage=InstallStage.FETCHING_NETS,
        )

        _feed(updater, [
            f"UC_BUILD_PROGRESS units={_REPORTED_UNITS_TOTAL}/{_REPORTED_UNITS_TOTAL}",
            _WGET_LENGTH_LINE,
            _WGET_EARLY_LINE,
        ])

        assert recorder.last["stage"] == InstallStage.FETCHING_NETS
        assert recorder.last["download_fraction"] == pytest.approx(0.05, abs=0.02)
        building = [call for call in recorder.calls if call["stage"] == InstallStage.BUILDING]
        assert building[-1]["build_fraction"] == pytest.approx(1.0)
