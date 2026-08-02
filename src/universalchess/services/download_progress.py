"""Real byte counts for a download, read from the downloader's own output.

Why this exists: engine repair fetches Maia's neural nets with wget, and it used
``wget -q --show-progress``. That pairing hides everything a caller needs: ``-q``
suppresses the header carrying the file size, and ``--show-progress`` draws a bar
with carriage returns, which never terminate a line. A caller reading lines
therefore sees nothing at all for the whole transfer -- no size, no bytes, and no
sign the download is even alive.

``wget --progress=dot`` prints the total up front and one newline-terminated line
per 50 KB, which yields a true "x of y bytes" and a steady liveness signal. On the
board a Maia net is 1313193 bytes and arrives at about 1.19 MB/s, so a transfer
produces roughly 26 progress lines instead of one silent wait.

The parser is pure: it consumes lines and reports a reading. Sample output it was
written against (GNU Wget 1.25.0, armv6l)::

    Length: 1313193 (1.3M) [application/octet-stream]
         0K .......... .......... .......... .......... ..........  3%  434K 3s
      1250K .......... .......... .......... ..                   100% 1.42M=2.1s
    2026-08-01 22:34:09 (1.19 MB/s) - '/var/tmp/maia-1100.pb.gz' saved [1313193/1313193]

Aggregation across files matters: one repair command fetches nine nets, and wget
restarts its byte counter for each. Reporting the per-file figure would sweep the
progress bar from 0 to 100 percent nine times, so completed files are accumulated
and the running count is reported alongside.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BYTES_PER_KIB = 1024

# "Length: 1313193 (1.3M) [application/octet-stream]" -- the exact total. Anchored
# at the start of the line so a size mentioned elsewhere cannot be mistaken for it.
_LENGTH_PATTERN = re.compile(r"^Length:\s+(\d+)")

# A dot-style progress line: right-aligned KiB completed at the line's start, then
# dot clusters, then wget's own rounded percentage. The percentage is required so
# that an arbitrary line beginning with a number cannot match -- notably the long
# digit runs inside the signed release-asset redirect URL.
_DOT_PATTERN = re.compile(r"^\s*(\d+)K[\s.]+(\d+)%")

# "... saved [1313193/1313193]" -- wget's closing line. The dot lines stop at the
# last 50 KB boundary (1250K for a 1282.4 KiB file), so without this a completed
# download would appear stuck just short of done.
_SAVED_PATTERN = re.compile(r"saved\s+\[(\d+)/(\d+)\]")


# "UC_DOWNLOAD_FILE index=3 total=10 name=maia-1300.pb.gz" -- published by a script
# that knows how many files it will fetch. wget cannot supply this: it announces one
# Length per file and nothing about how many follow.
_FILE_PATTERN = re.compile(
    r"^UC_DOWNLOAD_FILE\s+index=(\d+)\s+total=(\d+)\s+name=(\S+)\s*$"
)


@dataclass(frozen=True)
class DownloadProgress:
    """A reading. ``bytes_total`` is None until a size has been announced.

    The ``file_*`` fields are None unless the producer announces its file positions;
    only a script that knows its own file list can, and most downloads do not.
    """

    bytes_done: int
    bytes_total: int | None
    files_completed: int
    fraction: float | None
    file_index: int | None = None
    file_total: int | None = None
    file_name: str | None = None
    file_bytes_done: int = 0
    file_bytes_total: int | None = None
    file_fraction: float | None = None


class DownloadProgressReader:
    """Folds a downloader's output lines into an aggregate byte count.

    Totals grow as each file's header arrives, because the number of files still to
    come is not knowable from the output. The fraction is therefore "of the work
    announced so far", which is why ``files_completed`` is reported with it.
    """

    def __init__(self) -> None:
        """Start with no file announced and nothing downloaded."""
        self._completed_bytes = 0
        self._completed_total = 0
        self._files_completed = 0
        self._current_done = 0
        self._current_total: int | None = None
        self._file_index: int | None = None
        self._file_total: int | None = None
        self._file_name: str | None = None

    def read_line(self, line: str) -> bool:
        """Consume one line of output, updating the reading if it carries progress.

        Returns whether the line was recognised as download output. Callers that
        read a stream carrying more than downloads (an engine build script logs its
        compile, fetches its nets, and prints ordinary chatter on one stream) need to
        know which kind of line they just saw in order to attribute it to the right
        install stage. Without that answer they can only test whether a total is
        known yet, which stays true for the rest of the build once the first file has
        been announced -- so every later log line would be reported as download
        progress.
        """
        text = line.rstrip()

        announced = _FILE_PATTERN.match(text)
        if announced is not None:
            # Close out the previous file first. Its bytes are otherwise still
            # "current" while the new index already counts it as finished, so the
            # job briefly reports that file twice and then drops back.
            self._retire_current()
            self._file_index = int(announced.group(1))
            self._file_total = int(announced.group(2))
            self._file_name = announced.group(3)
            return True

        saved = _SAVED_PATTERN.search(text)
        if saved is not None:
            self._complete_current(written=int(saved.group(1)),
                                   expected=int(saved.group(2)))
            return True

        length = _LENGTH_PATTERN.match(text)
        if length is not None:
            # A new file has been announced. Any previous file that never reported a
            # closing line is retired at whatever it reached, so its bytes are not
            # lost from the aggregate and not double-counted either.
            self._retire_current()
            self._current_total = int(length.group(1))
            self._current_done = 0
            return True

        dot = _DOT_PATTERN.match(text)
        if dot is not None and self._current_total is not None:
            self._current_done = int(dot.group(1)) * _BYTES_PER_KIB
            return True
        return False

    def progress(self) -> DownloadProgress:
        """Report the current aggregate reading across every file seen so far."""
        bytes_done = self._completed_bytes + self._current_done
        announced_total = self._completed_total + (self._current_total or 0)
        bytes_total = announced_total if announced_total > 0 else None
        file_fraction = None
        if self._current_total:
            file_fraction = min(self._current_done / self._current_total, 1.0)

        fraction = None
        if self._file_total:
            # Count whole files, because byte totals only cover files already
            # announced: on that basis the job reads as complete at the end of every
            # file and then falls back when the next one starts. The file count is
            # known up front, so completed files plus the current file's share is
            # the honest measure of the whole job.
            # Take whichever count is further along: the announced position implies
            # the files before it are done, and a closing summary can confirm the
            # current one too. Using only the position loses a file between its
            # summary and the next announcement, sending the bar backwards.
            finished_files = max((self._file_index or 1) - 1, self._files_completed)
            fraction = min(
                (finished_files + (file_fraction or 0.0)) / self._file_total, 1.0
            )
        elif bytes_total:
            # Clamped: the counter is in whole KiB while the total is exact bytes,
            # and they come from different lines, so a resumed transfer or an
            # understated Length could otherwise report over 100%.
            fraction = min(bytes_done / bytes_total, 1.0)
        return DownloadProgress(
            bytes_done=bytes_done,
            bytes_total=bytes_total,
            files_completed=self._files_completed,
            fraction=fraction,
            file_index=self._file_index,
            file_total=self._file_total,
            file_name=self._file_name,
            file_bytes_done=self._current_done,
            file_bytes_total=self._current_total,
            file_fraction=file_fraction,
        )

    def _complete_current(self, written: int, expected: int) -> None:
        """Bank a finished file at its reported size."""
        self._completed_bytes += written
        self._completed_total += max(expected, written, self._current_total or 0)
        self._files_completed += 1
        self._current_done = 0
        self._current_total = None

    def _retire_current(self) -> None:
        """Bank an unfinished file, used when the next one starts without a summary."""
        if self._current_total is None:
            return
        self._completed_bytes += self._current_done
        self._completed_total += self._current_total
        self._current_done = 0
        self._current_total = None
