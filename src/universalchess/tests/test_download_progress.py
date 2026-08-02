"""Tests for reading real byte counts out of a downloader's output.

Why this module exists: engine repair fetches Maia's neural nets by shelling out
to wget, and it used ``wget -q --show-progress``. That combination is the worst of
both worlds for observability -- ``-q`` suppresses the header carrying the file
size, and ``--show-progress`` draws a bar with carriage returns, which never
terminate a line. A line-reading caller therefore sees nothing at all for the
whole transfer, so the install could report neither progress nor whether the
download was alive.

``wget --progress=dot`` instead prints the total up front and one newline
terminated line per 50 KB, which yields a true "x of y bytes" and a steady
liveness signal.

Every sample below is real output captured from the board (GNU Wget 1.25.0 on
Raspberry Pi Zero W, armv6l) fetching a Maia net, not a reconstruction from
memory. maia-1100.pb.gz is 1313193 bytes and transferred at about 1.19 MB/s.
"""

import pytest

from universalchess.services.download_progress import DownloadProgressReader

# Real byte count of the net used in the captured samples.
_NET_TOTAL_BYTES = 1_313_193

# The header wget prints once the response arrives, carrying the exact total.
_LENGTH_LINE = "Length: 1313193 (1.3M) [application/octet-stream]"

# A dot-style progress line. The leading field is the bytes already written at the
# start of the line, in KiB; the trailing percentage is wget's own rounding.
_PROGRESS_LINE_50K = "    50K .......... .......... .......... .......... ..........  7%  120K 9s"

# wget's closing line, which restates bytes written over the expected total.
_FILE_ANNOUNCEMENT = "UC_DOWNLOAD_FILE index=3 total=10 name=maia-1300.pb.gz"

_SAVED_LINE = (
    "2026-08-01 22:34:09 (1.19 MB/s) - '/var/tmp/maia-1100.pb.gz' "
    "saved [1313193/1313193]"
)


class TestTotalDiscovery:
    """Finding the denominator of "x of y bytes"."""

    def test_length_header_sets_the_total(self):
        """The exact total comes from wget's Length header.

        Why: without a total there is no fraction and no ETA, only a growing
        number. The header is the one place the real size appears before the
        transfer starts, which is why the quiet flag that suppressed it had to go.

        How a regression manifests: ignoring this line leaves the total unknown, so
        the install shows bytes with no denominator and the progress bar cannot
        move proportionally.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)

        assert reader.progress().bytes_total == _NET_TOTAL_BYTES

    def test_no_progress_before_the_header_arrives(self):
        """Connection chatter is not mistaken for progress.

        Why: wget prints several lines (DNS resolution, the redirect to the release
        asset host, the 200) before any byte of the file is written. Treating those
        as progress would move the bar before the transfer begins.

        How a regression manifests: a loose parser matches a number in the redirect
        URL -- which contains long digit runs -- and reports a nonsense byte count.
        """
        reader = DownloadProgressReader()
        for line in (
            "--2026-08-01 22:34:06--  https://github.com/CSSLab/maia-chess/"
            "releases/download/v1.0/maia-1100.pb.gz",
            "Resolving github.com (github.com)... 140.82.113.4",
            "Connecting to github.com (github.com)|140.82.113.4|:443... connected.",
            "HTTP request sent, awaiting response... 302 Found",
        ):
            reader.read_line(line)

        progress = reader.progress()
        assert progress.bytes_done == 0
        assert progress.bytes_total is None
        assert progress.fraction is None


class TestByteProgress:
    """Reading how much has actually been written."""

    def test_dot_line_reports_bytes_completed(self):
        """The leading KiB field becomes a byte count.

        Why: this is the live signal. A line arrives every 50 KB, so on a 1.3 MB
        net the install gets about 26 updates instead of one silent wait.

        How a regression manifests: parsing the trailing percentage instead of the
        leading field loses resolution to whole percents and misreads the speed
        column ("120K") as progress.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)
        reader.read_line(_PROGRESS_LINE_50K)

        progress = reader.progress()
        assert progress.bytes_done == 50 * 1024
        assert progress.fraction == pytest.approx(50 * 1024 / _NET_TOTAL_BYTES)

    @pytest.mark.parametrize(("prefix", "expected_kib"), [
        ("     0K", 0),
        ("    50K", 50),
        ("   500K", 500),
        ("  1000K", 1000),
        (" 12500K", 12500),
    ])
    def test_byte_column_parsed_across_its_width(self, prefix, expected_kib):
        """The field is right-aligned and widens as the transfer grows.

        Why: wget pads the column, so a parser keyed to a fixed offset works at the
        start of a transfer and breaks later. The full Maia repair fetches nine
        nets, so multi-thousand-KiB values occur in normal use.

        How a regression manifests: an offset-based parse returns a truncated
        number, making the bar jump backwards partway through a download.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)
        reader.read_line(f"{prefix} .......... ..........  7%  120K 9s")

        assert reader.progress().bytes_done == expected_kib * 1024

    def test_progress_never_exceeds_the_total(self):
        """A fraction stays within [0, 1] when the counted bytes overrun the total.

        Why: the counter is in KiB while the total is exact bytes, and the two come
        from different lines, so they can disagree -- a resumed transfer, or a
        server whose Length understates what it sends. wget's own dot lines stay
        under the total in the captured runs (the last is 1250K of a 1282.4 KiB
        file), so this guards the mismatch rather than the normal case. An
        unclamped fraction above 1.0 would push the bar past its stage band.

        How a regression manifests: such a download reports over 100% and the bar
        overshoots into the next stage's range.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)
        reader.read_line("   1300K .......... ..........  99% 1.19M 0s")

        assert reader.progress().fraction == pytest.approx(1.0)

    def test_saved_line_completes_the_transfer(self):
        """wget's closing line reports the finished byte count.

        Why: the dot lines stop at the last 50 KB boundary, so without reading the
        summary a completed download would appear stuck just short of done.

        How a regression manifests: the final fraction sits below 1.0 and the UI
        shows an incomplete download for a file that arrived intact.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)
        reader.read_line(_SAVED_LINE)

        progress = reader.progress()
        assert progress.bytes_done == _NET_TOTAL_BYTES
        assert progress.fraction == pytest.approx(1.0)


class TestMultipleFiles:
    """The repair fetches nine nets in one command."""

    def test_each_file_restarts_the_byte_count_but_advances_the_overall_count(self):
        """Per-file progress is aggregated across the whole command.

        Why this test exists: Maia's repair downloads nine nets in a single
        invocation, and each new file resets wget's KiB counter to zero. Reporting
        the per-file figure as overall progress makes the bar sweep 0-100% nine
        times; the user cannot tell how much of the repair remains.

        How a regression manifests: the second file's early progress reads lower
        than the first file's completed total, so the bar resets visibly.
        """
        reader = DownloadProgressReader()
        reader.read_line(_LENGTH_LINE)
        reader.read_line(_SAVED_LINE)
        after_first = reader.progress()

        # A second net begins: new header, counter restarts at zero.
        reader.read_line(_LENGTH_LINE)
        reader.read_line("    50K .......... ..........  4%  120K 9s")
        during_second = reader.progress()

        assert after_first.files_completed == 1
        assert during_second.files_completed == 1
        assert during_second.bytes_done > after_first.bytes_done
        assert during_second.bytes_done == _NET_TOTAL_BYTES + 50 * 1024

    def test_total_accumulates_only_for_files_already_announced(self):
        """The denominator covers what is known, and is honest that more may follow.

        Why: the count of remaining files is not knowable from wget's output alone,
        so the total grows as each header arrives. The fraction must therefore be
        read as "of the work announced so far", and the caller needs the file count
        to present it sensibly.

        How a regression manifests: assuming the first Length applies to the whole
        command makes the fraction hit 1.0 after the first of nine nets.
        """
        reader = DownloadProgressReader()
        for _ in range(3):
            reader.read_line(_LENGTH_LINE)
            reader.read_line(_SAVED_LINE)

        progress = reader.progress()
        assert progress.files_completed == 3
        assert progress.bytes_total == 3 * _NET_TOTAL_BYTES
        assert progress.fraction == pytest.approx(1.0)


class TestRobustness:
    """Behaviour on output that is not a download at all."""

    def test_compiler_output_yields_no_download_progress(self):
        """Build lines are not misread as bytes.

        Why: the same line stream carries compiler output, because repair and build
        share one command runner. A parser that matches loosely would invent
        download progress during a compile and corrupt the build's own bar.

        How a regression manifests: a byte count appears while compiling, and the
        install reports a download that is not happening.
        """
        reader = DownloadProgressReader()
        for line in (
            "g++ -O3 -c src/search.cpp -o search.o",
            "  CC       eval.c",
            "make: Leaving directory '/var/tmp/build'",
        ):
            reader.read_line(line)

        progress = reader.progress()
        assert progress.bytes_done == 0
        assert progress.bytes_total is None
        assert progress.fraction is None

    def test_failed_download_does_not_report_bytes_it_never_wrote(self):
        """A 404 leaves progress empty rather than inventing a total.

        Why: a net that 404s (which happened when the weights URL moved) must not
        leave the bar claiming bytes arrived. The header is what establishes a
        total, and an error response carries none.

        How a regression manifests: fabricating a total on failure shows a
        stalled-looking partial download instead of surfacing the real error.
        """
        reader = DownloadProgressReader()
        reader.read_line("HTTP request sent, awaiting response... 404 Not Found")
        reader.read_line("2026-08-01 22:34:09 ERROR 404: Not Found.")

        assert reader.progress().bytes_total is None


class TestFilePosition:
    """Which file of how many is currently transferring."""

    def test_announcement_names_the_file_and_its_position(self):
        """The announced index, total and name are reported to the caller.

        Why this test exists: byte totals alone cannot say how much of a multi-file
        download remains, because wget announces one Length per file -- the bar would
        restart at zero ten times with nothing indicating how many were left. The
        script therefore announces each file, and this is what turns that into
        "file 3 of 10 (maia-1300.pb.gz)" in the install banner.

        How a regression manifests: the announcement stops parsing and the UI loses
        the file position, leaving only a byte count that repeatedly resets.
        """
        reader = DownloadProgressReader()

        assert reader.read_line(_FILE_ANNOUNCEMENT) is True
        progress = reader.progress()
        assert progress.file_index == 3
        assert progress.file_total == 10
        assert progress.file_name == "maia-1300.pb.gz"

    def test_no_position_before_any_file_is_announced(self):
        """Nothing is claimed about position until a file says so.

        Why this test exists: a downloader that never announces (any engine other
        than Maia) must not have a fabricated "file 1 of 1" attached to its bytes.

        How a regression manifests: unrelated downloads gain a meaningless file
        counter in their message.
        """
        progress = DownloadProgressReader().progress()

        assert progress.file_index is None
        assert progress.file_total is None
        assert progress.file_name is None

    def test_position_advances_and_bytes_reset_per_file(self):
        """A new announcement moves the position without losing completed bytes.

        Why this test exists: this is the real sequence -- announce, transfer, close,
        announce the next. The aggregate byte count must keep accumulating across
        files (so the overall bar advances) while the reported position tracks the
        file currently moving.

        How a regression manifests: either the position sticks at the first file, or
        the byte aggregate resets on each announcement and the bar restarts.
        """
        reader = DownloadProgressReader()
        reader.read_line(_FILE_ANNOUNCEMENT)
        reader.read_line(_LENGTH_LINE)
        reader.read_line(_SAVED_LINE)
        reader.read_line("UC_DOWNLOAD_FILE index=4 total=10 name=maia-1400.pb.gz")

        progress = reader.progress()
        assert progress.file_index == 4
        assert progress.file_name == "maia-1400.pb.gz"
        assert progress.files_completed == 1
        assert progress.bytes_done == _NET_TOTAL_BYTES

    def test_a_malformed_announcement_is_ignored(self):
        """A garbled announcement leaves the previous position untouched.

        Why this test exists: the position feeds a user-visible message, and a
        partial or corrupted line must not blank it out or raise inside the line
        callback, which would end an otherwise healthy install.

        How a regression manifests: a ValueError escapes the callback, or the banner
        loses the file position mid-download.
        """
        reader = DownloadProgressReader()
        reader.read_line(_FILE_ANNOUNCEMENT)
        for line in (
            "UC_DOWNLOAD_FILE index= total=10 name=x",
            "UC_DOWNLOAD_FILE",
            "UC_DOWNLOAD_FILE index=many total=10 name=x",
        ):
            assert reader.read_line(line) is False

        assert reader.progress().file_index == 3
