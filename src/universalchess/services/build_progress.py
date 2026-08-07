"""Real progress for a source build, read from the compiler's own processes.

Why this exists: a build is the longest phase of an engine install and the least
willing to say so. Rodent IV compiles with one ``g++ ... src/*.cpp`` invocation and
``-w``, which on a Raspberry Pi Zero W means 13m31s of work and exactly one line
of output (measured). Output can therefore drive neither a progress bar nor a hang
detector, and an install that guessed from elapsed time was killed mid-compile at
95% because the guess ran out before the compiler did.

What is observable: compiler drivers list every input on their command line, and
they fork one backend process (``cc1``, ``cc1plus``, ``rustc``, Go's ``compile``)
per translation unit which names the file it is working on. Sampling the build's
process tree therefore yields an exact denominator and a real numerator for a
build that prints nothing.

Five things measured on the board shaped this design:

1. Units are missed by sampling. Claudia's 14-unit build finished in 83s and only
   six units were ever caught in a sample. Because a driver processes inputs in
   the order listed, seeing unit N proves the earlier ones are done, which
   recovers the rest exactly.
2. Units are wildly unequal. CT800's generated ``bookdata.c`` is 828875 bytes,
   4.4x the next largest file, and its single unit outlasted the other seven
   combined. Counting units equally pins the bar near its ceiling for minutes, so
   units are weighted by source size.
3. A long unit is loud, not silent. Across 21 minutes of compiling, the longest
   stretch with no signal of any kind was 7.0s. Liveness therefore comes from
   consumed CPU and moved bytes as well as units and output, which keeps a build
   inside one four-minute unit -- or blocked on slow SD-card I/O -- from being
   mistaken for a wedged one.
4. Units the build has not reached yet still have to be paid for. Where the unit
   set is discovered by watching rather than declared on a command line, the only
   units with a known cost are those already sampled -- and every one of those
   except the unit in flight is finished. Dividing by them alone reports (n-1)/n
   whatever remains: Reckless read 94% and "less than a minute remaining" at its
   17th module of roughly 120. The denominator is therefore every unit the build
   has, with the unreached ones costed at the mean of the known ones.
5. A unit is not always a file. ``rustc`` compiles a whole crate per invocation
   and is handed only that crate's root, so the modules beside that root are never
   units and the crate count -- not the file count -- is the denominator. Reckless
   v0.9.0 resolves 36 packages in a tree whose crate directories hold hundreds of
   ``.rs`` files.

Structure: everything here is pure and takes an injected process table, so it is
testable without ``/proc`` or a live compiler. Reading ``/proc`` is isolated in
:func:`read_process_table`, the only function that touches the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Compiler front ends: invoked once per file by a makefile, or once with every
# input by a single-invocation build. More than one input means the command line
# declares the whole unit set.
_COMPILER_DRIVERS = frozenset({"gcc", "g++", "cc", "c++", "clang", "clang++", "cc1obj"})

# Per-unit backends. These name exactly the file being compiled, which is what
# makes the current unit observable. ``compile`` is Go's; ``rustc`` is Rust's.
_COMPILER_BACKENDS = frozenset({"cc1", "cc1plus", "rustc", "compile"})

# Rust is the one toolchain here whose unit is not a file. ``rustc`` compiles a
# whole crate per invocation and is handed only that crate's root, so the modules
# beside that root are never units of their own and the crate name -- not the root
# file, which is ``lib.rs`` or ``main.rs`` in every crate ever written -- is what
# identifies what is compiling.
_RUST_BACKEND = "rustc"
_CRATE_NAME_OPTION = "--crate-name"

# Cargo's resolved package list, and the line that opens one entry in it. Counting
# those entries is the only count of a cargo build's units available before the
# build reaches them: cargo prints no total, and nothing on a rustc command line
# describes the crates still to come.
_CARGO_LOCK_NAME = "Cargo.lock"
_CARGO_LOCK_PACKAGE_HEADER = "[[package]]"

# Extensions treated as translation units.
_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".cu", ".rs", ".go", ".S", ".s")

# Options whose following argument is a path and must not be read as an input.
#
# The -dump*/-aux* group is what the GCC driver adds when it invokes cc1plus, and it
# is the reason this set has to cover more than the user-facing options: cc1plus
# receives `-dumpbase eval.cpp` -- the unit it is already compiling, named again
# without its directory -- so omitting these counted every unit twice. A 32-file
# Rodent IV build reported 65 units on the Pi Zero W.
_OPTIONS_TAKING_A_VALUE = frozenset({
    "-o", "-I", "-include", "-isystem", "-iquote", "-MF", "-MT", "-MQ", "-x",
    "-aux-info", "--out-dir", "-L", "-idirafter",
    "-dumpbase", "-dumpbase-ext", "-dumpdir", "-auxbase", "-auxbase-strip",
    "-imultilib", "-imultiarch",
    # Separated macro forms: `-D NDEBUG` and `-D BOOKPATH=/usr/share/rodentIV`.
    "-D", "-U", "-A",
})

# Observed progress is capped just below completion: the last unit finishing is not
# the build finishing, because linking follows and is not instant on this hardware.
# Reporting 1.0 would drive the bar to its ceiling and hold there, which is the
# frozen-bar symptom this work removes.
_MAX_OBSERVED_FRACTION = 0.99

# Completed share required before a remaining-time projection is published. Below
# this, the measured time is dominated by the first unit's warm-up rather than by a
# representative pace, so the extrapolation is wildly high: measured on the board, a
# Rodent IV build that took about 11 minutes projected 5689s remaining when 0.5%
# done, then 2054s, before settling near 650s. Withholding the number until there is
# something to extrapolate from is more useful than showing one that immediately
# contradicts itself.
_MIN_FRACTION_FOR_ETA = 0.05

# Indices into /proc/<pid>/stat, counted after the comm field (which is skipped
# because a process name may itself contain spaces or brackets). The fields run:
# state, ppid, pgrp, session, tty, tpgid, flags, minflt, cminflt, majflt, cmajflt,
# utime, stime -- so ppid is at 1 and the two CPU counters at 11 and 12.
_STAT_PPID_INDEX = 1
_STAT_UTIME_INDEX = 11
_STAT_STIME_INDEX = 12
_STAT_FIELDS_THROUGH_STIME = _STAT_STIME_INDEX + 1


@dataclass(frozen=True)
class ProcessInfo:
    """One sampled process. ``cpu_ticks`` and ``io_bytes`` are cumulative counters."""

    pid: int
    ppid: int
    comm: str
    args: tuple[str, ...] = ()
    cpu_ticks: int = 0
    io_bytes: int = 0
    # The process's own working directory, which is where its relative input paths
    # resolve. Needed because a build command commonly navigates before compiling
    # (Rodent IV runs "cd sources && make"), so the directory the installer knows
    # about is not the one the compiler names its files from. None when unreadable.
    cwd: str | None = None


@dataclass(frozen=True)
class BuildProgress:
    """A progress reading. ``fraction`` is None when the build is unobservable."""

    observed: bool
    fraction: float | None
    units_finished: int
    units_total: int | None
    total_is_exact: bool
    current_unit: str | None
    eta_seconds: int | None
    # Seconds the current unit has been compiling. A compiler reports nothing about
    # its progress through one translation unit, so this is the only measurable
    # thing about the module in flight -- and it is what tells a slow one (CT800's
    # 829 KB bookdata.c) apart from a wedged one. None when no unit is observable.
    current_unit_seconds: float | None = None


# Sentinel a build script uses to publish its own exact unit counts. Newline
# terminated by the producer, because the installer reads its output line by line.
_REPORTED_UNITS_PATTERN = re.compile(r"^UC_BUILD_PROGRESS\s+units=(\d+)/(\d+)\s*$")


@dataclass(frozen=True)
class ReportedBuildProgress:
    """Unit counts a build published about itself, and the fraction they imply."""
    units_finished: int
    units_total: int
    fraction: float | None
    # Projected seconds until the last unit finishes, or None before there is a
    # measured rate to project from. Without this a build that publishes its own
    # counts showed a percentage and nothing else: Maia sat at "module 177 of 259
    # (68%)" on a Pi Zero W for four hours with no indication that the board needs
    # six to eight of them.
    eta_seconds: int | None = None


# Multiple of the projected remaining time granted whenever a build finishes more
# work. Generous because the projection is a rate measured over past units and the
# later units of a build are commonly the slowest -- Maia's went from 1.5 to 3.2
# minutes each over one run -- so a tight multiple would expire mid-unit.
BUILD_CEILING_PROJECTION_HEADROOM = 2.0


class BuildCeiling:
    """A backstop deadline that finished work can push further out.

    The ceiling exists for one pathology: a build consuming CPU forever without
    ever completing, which every liveness signal reads as work. It cannot be a
    fixed budget, because a budget is a guess about hardware. Maia's was four
    times its catalog estimate and still killed a healthy Pi Zero W build at four
    hours, at module 177 of 259 -- the same failure as the ten-minute budget that
    started all of this, only more expensive to discover.

    So time is granted for work completed, never for time elapsed: each time the
    finished fraction increases, the deadline moves to the projected finish plus
    headroom. A build that keeps compiling keeps earning time and can run as long
    as it genuinely needs, while one that stops finishing units earns nothing more
    and runs out at the last deadline it was granted.
    """

    def __init__(
        self,
        started_at: float,
        base_seconds: float,
        headroom: float = BUILD_CEILING_PROJECTION_HEADROOM,
    ) -> None:
        self._deadline = started_at + base_seconds
        self._headroom = headroom
        self._granted_at_fraction: float | None = None

    def deadline(self) -> float:
        """The monotonic time at which the build should be given up on."""
        return self._deadline

    def note_progress(
        self,
        now: float,
        fraction: float | None,
        eta_seconds: int | None,
    ) -> None:
        """Grant more time if the build has finished work since the last grant.

        A reading that is missing either half is ignored rather than defaulted.
        Treating an absent fraction as zero progress would extend the deadline on
        every sample of an unobservable build, which is the one case the fixed
        backstop is there to cover.
        """
        if fraction is None or eta_seconds is None:
            return
        if self._granted_at_fraction is not None and fraction <= self._granted_at_fraction:
            return
        self._granted_at_fraction = fraction
        # Only ever later: a projection that falls as a build speeds up must not
        # pull in a deadline the build is already relying on.
        self._deadline = max(self._deadline, now + eta_seconds * self._headroom)


class BuildReportReader:
    """Exact progress read from a build that reports its own unit counts.

    Process observation cannot cover every toolchain. Maia builds lc0 through
    meson/ninja with clang, where ninja invokes the compiler once per file (so no
    command line ever declares the whole unit set) and clang compiles in-process
    rather than forking a named backend (so no per-unit process is ever visible).
    Nothing in that build's process tree reveals how far along it is.

    What the build does know is exact: ninja counts its edges. ``build-maia.sh``
    already reconciles that count against resumes -- ninja's own ``[current/total]``
    is per-invocation, so a resumed build restarts at zero against a smaller total --
    and publishes the reconciled figure on a sentinel line. Parsing that line is
    therefore both more accurate than observation and the only option here.

    Only the sentinel is parsed, deliberately. Ninja's raw ``[14/245]`` is ignored
    because reading it would duplicate the resume accounting the script has already
    done, and a second implementation of it is a second chance to get it wrong.
    """

    def __init__(self) -> None:
        self._units_finished = 0
        self._units_total = 0
        # First reading seen with a clock, used as the origin for the rate. Not the
        # start of the process: build-maia.sh reconciles ninja's counter across
        # resumes, so a resumed build's first report can be at 200/240 having done
        # none of it in this run. Crediting those units to this run's elapsed time
        # implies a huge rate and reports a six-hour build as nearly done.
        self._baseline_units: int | None = None
        self._baseline_at = 0.0

    def read_line(self, line: str) -> bool:
        """Consume one line, returning whether it was a progress report.

        Reports that would move the count backwards are ignored rather than applied:
        the publisher derives its figure arithmetically across resumes, and a bar
        that retreats reads as a fault.
        """
        match = _REPORTED_UNITS_PATTERN.match(line.strip())
        if match is None:
            return False
        total = int(match.group(2))
        if total <= 0:
            # The total is a denominator; a zero one is a publisher bug, and treating
            # it as data would either divide by zero or assert completion.
            return False
        self._units_total = total
        self._units_finished = max(self._units_finished, int(match.group(1)))
        return True

    def progress(self, now: float | None = None) -> ReportedBuildProgress:
        """Report the latest published counts, with a projection when ``now`` is given.

        The fraction is None until something has actually been reported. Zero would
        be a fabricated measurement -- "no work done" rather than "not known" -- and
        the install-percent calculation prefers any non-None fraction over its
        time-based fallback, so a fabricated zero would pin the bar at the bottom of
        the build band for the whole build.

        ``now`` is supplied by the caller rather than read here so the projection
        stays a pure function of the readings it was given.
        """
        if self._units_total <= 0:
            return ReportedBuildProgress(0, 0, None)
        return ReportedBuildProgress(
            units_finished=self._units_finished,
            units_total=self._units_total,
            fraction=min(self._units_finished / self._units_total, 1.0),
            eta_seconds=None if now is None else self._eta_seconds(now),
        )

    def _eta_seconds(self, now: float) -> int | None:
        """Seconds of work left at the rate measured since the first reading."""
        if self._baseline_units is None:
            self._baseline_units = self._units_finished
            self._baseline_at = now
            return None
        units_done = self._units_finished - self._baseline_units
        elapsed = now - self._baseline_at
        if units_done <= 0 or elapsed <= 0:
            return None
        remaining = max(0, self._units_total - self._units_finished)
        return int(remaining * elapsed / units_done)


def _is_source_path(candidate: str) -> bool:
    """Whether ``candidate`` names a compilable source file.

    Tests the parsed suffix rather than the trailing characters of the string. The
    two differ for a bare extension: ``".cpp"`` ends with ``.cpp`` but is a name
    with no suffix, and GCC passes exactly that as the value of ``-dumpbase-ext``.
    A string test admitted it as a phantom unit.
    """
    return Path(candidate).suffix in _SOURCE_SUFFIXES


def _source_arguments(args: Sequence[str]) -> list[str]:
    """Source files named on a command line, in the order given, without repeats.

    Order is load-bearing: it is what lets a unit seen mid-build imply that the
    units before it are finished. Options that take a path are skipped so an
    ``-o out.c`` or an include directory is never counted as an input.
    """
    sources: list[str] = []
    skip_next = False
    for arg in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in _OPTIONS_TAKING_A_VALUE:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if _is_source_path(arg) and arg not in sources:
            sources.append(arg)
    return sources


def _crate_name(args: Sequence[str]) -> str | None:
    """The crate ``rustc`` was told to compile, or None if it was not named.

    ``--crate-name`` is always present when cargo drives the build; a hand-rolled
    ``rustc`` invocation may omit it, in which case the caller falls back to the
    crate root path so the unit still has an identity.
    """
    for index, arg in enumerate(args):
        if arg == _CRATE_NAME_OPTION and index + 1 < len(args):
            return args[index + 1]
    return None


def _count_locked_packages(source_root: Path) -> int:
    """Packages Cargo.lock resolved for the build, or 0 where it cannot be read.

    Approximate in both directions, deliberately. The lock file is the resolved
    dependency graph rather than the build plan: it omits the build scripts cargo
    compiles as units of their own, and it includes packages for other platforms
    that are never built here (10 of Reckless v0.9.0's 36 entries are Windows
    targets). It is nonetheless the right order of magnitude, where counting the
    ``.rs`` files beside each crate root is not -- that counted Reckless as ~120
    units and rising against roughly 36 real ones.
    """
    counted = 0
    try:
        # Streamed rather than read whole: a custom engine is built from a clone of
        # a URL the user supplied, so the size of anything in it is not this
        # module's to assume, and this board has 426 MB of RAM.
        with (source_root / _CARGO_LOCK_NAME).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip() == _CARGO_LOCK_PACKAGE_HEADER:
                    counted += 1
    except OSError:
        return 0
    return counted


def _descendants(root_pid: int, table: Mapping[int, ProcessInfo]) -> list[ProcessInfo]:
    """Every process descended from ``root_pid``, excluding the root itself.

    The build runs under a shell that spawns make, which spawns the driver, which
    spawns the backend, so the process of interest is several levels down. Walking
    by ancestry (rather than matching on process name) keeps a compile belonging to
    the app, or to a second install, out of this build's numbers.
    """
    children: dict[int, list[int]] = {}
    for process in table.values():
        children.setdefault(process.ppid, []).append(process.pid)

    found: list[ProcessInfo] = []
    stack = list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        process = table.get(pid)
        if process is None:
            continue
        found.append(process)
        stack.extend(children.get(pid, ()))
    return found


def read_process_table() -> dict[int, ProcessInfo]:
    """Sample every readable process from ``/proc``.

    The only function here that touches the system. Returns an empty table where
    ``/proc`` is absent or unreadable, which callers treat as "unobservable" and
    fall back on rather than as "nothing is happening".
    """
    table: dict[int, ProcessInfo] = {}
    try:
        entries = [entry.name for entry in Path("/proc").iterdir()]
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        process = _read_one_process(int(entry))
        if process is not None:
            table[process.pid] = process
    return table


def _read_one_process(pid: int) -> ProcessInfo | None:
    """Read one process's identity and counters, or None if it has exited."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    close = stat.rfind(")")
    open_paren = stat.find("(")
    if close == -1 or open_paren == -1:
        return None
    comm = stat[open_paren + 1:close]
    fields = stat[close + 2:].split()
    if len(fields) < _STAT_FIELDS_THROUGH_STIME:
        return None
    try:
        ppid = int(fields[_STAT_PPID_INDEX])
        cpu_ticks = int(fields[_STAT_UTIME_INDEX]) + int(fields[_STAT_STIME_INDEX])
    except ValueError:
        return None
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        comm=comm,
        args=tuple(_read_cmdline(pid)),
        cpu_ticks=cpu_ticks,
        io_bytes=_read_io_bytes(pid),
        cwd=_read_cwd(pid),
    )


def _resolve_against(path: Path, base: Path) -> Path:
    """``path`` made absolute against ``base``, or unchanged if already absolute."""
    return path if path.is_absolute() else base / path


def _read_cwd(pid: int) -> str | None:
    """Resolve ``pid``'s working directory, or None if it cannot be read.

    Unreadable for a process owned by another user, and for one that exits between
    the scan and this read; both are ordinary, so callers fall back to the directory
    the build was launched in rather than treating it as an error.

    Uses readlink rather than ``resolve()`` deliberately. ``resolve()`` does not
    raise when it cannot follow a link -- it returns the path it was given -- so on
    a board where most processes belong to root it reported success for every one of
    them and handed back "/proc/<pid>/cwd" as though that were a directory. A
    fabricated base is worse than none, because it silently misdirects every source
    path resolved against it instead of falling back.
    """
    try:
        return str(Path(f"/proc/{pid}/cwd").readlink())
    except OSError:
        return None


def _read_cmdline(pid: int) -> list[str]:
    """Argument list for ``pid``; empty for a kernel thread or an exited process."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def _read_io_bytes(pid: int) -> int:
    """Bytes ``pid`` has read and written, 0 when unreadable.

    Needed because CPU time does not advance while a process is blocked. On this
    board iowait is a large share of real time, and a repair command is a download
    that is almost entirely waiting -- both would look wedged on CPU alone.
    """
    total = 0
    try:
        with Path(f"/proc/{pid}/io").open(encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in ("rchar", "wchar"):
                    total += int(value.strip())
    except (OSError, ValueError):
        return 0
    return total


@dataclass
class _Counter:
    """A cumulative total over a changing set of processes, made monotonic.

    A plain sum over live processes falls every time a translation unit's backend
    exits -- precisely when progress just happened. Remembering each pid's last
    reading and retiring it on exit keeps the total non-decreasing.
    """

    last_seen: dict[int, int] = field(default_factory=dict)
    retired: int = 0

    def observe(self, pid: int, value: int) -> None:
        self.last_seen[pid] = max(self.last_seen.get(pid, 0), value)

    def retire_absent(self, live_pids: set[int]) -> None:
        for pid in [pid for pid in self.last_seen if pid not in live_pids]:
            self.retired += self.last_seen.pop(pid)

    def total(self) -> int:
        return self.retired + sum(self.last_seen.values())


class BuildProgressTracker:
    """Accumulates observations of one build's process tree.

    ``record`` folds in a sample; ``progress`` reports the current reading. Both
    take the current time so the caller controls the clock, and the tracker holds
    no timing source of its own.
    """

    def __init__(self, root_pid: int, source_root: Path, started_at: float = 0.0) -> None:
        """Track the tree under ``root_pid``, resolving unit paths under ``source_root``."""
        self._root_pid = root_pid
        self._source_root = Path(source_root)
        self._started_at = started_at
        self._declared_units: list[str] = []
        self._total_is_exact = False
        self._seen_units: set[str] = set()
        self._active_units: set[str] = set()
        self._source_dirs: set[str] = set()
        # Units that are whole crates rather than files, and the resolved package
        # count they are measured against (None until Cargo.lock has been read).
        self._crate_units: set[str] = set()
        self._locked_packages: int | None = None
        # Display name per unit, where the path that identifies a unit is not what
        # should be shown for it. Only crates have one: every crate root in every
        # cargo build is called lib.rs or main.rs.
        self._unit_labels: dict[str, str] = {}
        self._current_unit: str | None = None
        self._current_unit_since: float | None = None
        # When the first unit was seen, which is when compiling began. The command
        # that runs a build does more than compile -- Reckless bootstraps a rustup
        # toolchain and lets cargo fetch its registry first -- so this, not the
        # command's own start, is the origin the compile rate is measured from.
        self._first_unit_at: float | None = None
        self._max_ordinal = 0
        self._observed = False
        self._max_fraction = 0.0
        self._sizes: dict[str, int | None] = {}
        # Per-unit resolution base: the working directory of the process that named
        # it. Falls back to source_root where a process's cwd could not be read.
        self._unit_bases: dict[str, str] = {}
        self._cpu = _Counter()
        self._io = _Counter()
        self._output_events = 0
        self._previous_signals: tuple[int, int, int, int] | None = None
        self._last_activity_at = started_at

    # -- observation --------------------------------------------------------
    def note_output(self, now: float) -> None:
        """Record a line of build output, which is unambiguous proof of work."""
        self._output_events += 1
        self._last_activity_at = now

    def record(self, processes: Mapping[int, ProcessInfo], now: float) -> None:
        """Fold one sample of the process table into the accumulated picture."""
        tree = _descendants(self._root_pid, processes)
        live_pids: set[int] = set()
        active: set[str] = set()
        current: str | None = None

        for process in tree:
            live_pids.add(process.pid)
            self._cpu.observe(process.pid, process.cpu_ticks)
            self._io.observe(process.pid, process.io_bytes)
            if process.comm not in _COMPILER_DRIVERS and process.comm not in _COMPILER_BACKENDS:
                continue
            sources = _source_arguments(process.args)
            if not sources:
                continue
            self._observed = True
            in_flight, unit = self._note_compilation(process, sources)
            active.update(in_flight)
            if unit is not None:
                current = unit

        self._cpu.retire_absent(live_pids)
        self._io.retire_absent(live_pids)
        self._seen_units.update(active)
        self._active_units = active
        if self._seen_units and self._first_unit_at is None:
            self._first_unit_at = now
        if current is not None:
            if current != self._current_unit:
                # Timed from the first sample naming this unit, not from every
                # sample, or a long compile would always look freshly started.
                self._current_unit_since = now
            self._current_unit = current
            self._advance_ordinal(current)
        self._update_activity(now)

    def _note_compilation(
        self, process: ProcessInfo, sources: Sequence[str],
    ) -> tuple[set[str], str | None]:
        """Fold one compiler process in, returning the units it has in flight.

        The second element is the unit to display as current, which only a backend
        has: a driver names every input it was given but compiles none of them
        itself.
        """
        if process.comm == _RUST_BACKEND:
            return self._note_crate(process, sources)
        self._note_source_locations(process, sources)
        if process.comm in _COMPILER_DRIVERS and len(sources) > 1:
            self._declare_units(sources)
        if process.comm in _COMPILER_BACKENDS:
            return set(sources), sources[0]
        return set(), None

    def _note_crate(
        self, process: ProcessInfo, sources: Sequence[str],
    ) -> tuple[set[str], str | None]:
        """Record the crate a ``rustc`` process is compiling as a single unit.

        Identified by the crate root, which is unique, and labelled with the crate
        name, which is not: cargo compiles every package's build script under the
        name ``build_script_build``, so keying on the name would merge several
        separate compilations into one unit and show a finished one running again.

        The crate's directory is deliberately not recorded as a source directory.
        The modules beside a crate root compile as part of the crate and never on
        their own, so counting them counts work that does not exist -- that is what
        made a 36-crate Reckless build report ~120 units and rising.
        """
        unit = sources[0]
        self._crate_units.add(unit)
        crate = _crate_name(process.args)
        if crate is not None:
            self._unit_labels.setdefault(unit, crate)
        return {unit}, unit

    def _note_source_locations(
        self, process: ProcessInfo, sources: Sequence[str],
    ) -> None:
        """Resolve a process's inputs against the directory it is running in.

        Done while the process that named these paths is in hand. A compiler names
        its inputs relative to its own working directory, which is commonly not the
        one the build was launched from: Smallbrain runs ``cd src && make``, so its
        bare ``unit.cpp`` belongs to ``<root>/src``. Resolving later against the
        launch directory looked in the clone root, found no sources, and left the
        unit total equal to the number already seen -- the "module 1 of 1, module 2
        of 2" the board displayed.
        """
        base = Path(process.cwd) if process.cwd is not None else self._source_root
        for source in sources:
            self._source_dirs.add(str(_resolve_against(Path(source).parent, base)))
            if process.cwd is not None:
                self._unit_bases.setdefault(source, process.cwd)

    def _declare_units(self, sources: Sequence[str]) -> None:
        """Adopt a driver's input list as the exact, ordered unit set."""
        for source in sources:
            if source not in self._declared_units:
                self._declared_units.append(source)
        self._total_is_exact = True

    def _advance_ordinal(self, current: str) -> None:
        """Remember the furthest position reached in the declared input order."""
        if current in self._declared_units:
            self._max_ordinal = max(self._max_ordinal, self._declared_units.index(current))

    def _update_activity(self, now: float) -> None:
        """Refresh the activity timestamp when any liveness signal has moved."""
        signals = (
            self._cpu.total(),
            self._io.total(),
            len(self._seen_units),
            self._output_events,
        )
        # Any single counter advancing is enough; they are compared component-wise
        # rather than as tuples, since a lexicographic comparison would miss a rise
        # in a later component when an earlier one is unchanged.
        if self._previous_signals is None or any(
            new > old for new, old in zip(signals, self._previous_signals)
        ):
            self._last_activity_at = now
        self._previous_signals = signals

    # -- reads -------------------------------------------------------------
    @property
    def last_activity_at(self) -> float:
        """When a liveness signal last moved. The stall deadline is measured off this."""
        return self._last_activity_at

    def progress(self, now: float) -> BuildProgress:
        """Report the current reading, with an ETA once there is work to project from."""
        if not self._observed:
            return BuildProgress(
                observed=False, fraction=None, units_finished=0, units_total=None,
                total_is_exact=False, current_unit=None, eta_seconds=None,
            )

        finished = self._finished_units()
        total_units = self._total_units()
        # No unit seen yet means completion is unknown, not zero. Reporting 0.0 here
        # would be a fabricated measurement, and because compute_percent prefers any
        # non-None fraction over its time-based creep it would pin the bar at the
        # bottom of the build band. That is the whole outcome for a toolchain this
        # module cannot watch per unit: Maia's ninja+clang build declares no unit set
        # and forks no named backend, so it would sit frozen for 45-60 minutes.
        fraction = self._fraction(finished, total_units) if self._seen_units else None
        current_unit_seconds = (
            None if self._current_unit_since is None
            else max(0.0, now - self._current_unit_since)
        )

        return BuildProgress(
            observed=True,
            fraction=fraction,
            units_finished=len(finished),
            units_total=total_units,
            total_is_exact=(
                self._total_is_exact
                and len(self._declared_units) >= len(self._seen_units)
            ),
            current_unit=self._describe_unit(self._current_unit),
            eta_seconds=self._eta_seconds(fraction, now, current_unit_seconds),
            current_unit_seconds=current_unit_seconds,
        )

    def _eta_seconds(
        self,
        fraction: float | None,
        now: float,
        current_unit_seconds: float | None,
    ) -> int | None:
        """Seconds of work left at the pace measured so far, or None when unknown.

        The pace is timed from the first unit rather than from the command's start,
        so it describes compiling and nothing else. A build command does more than
        compile -- Reckless bootstraps a pinned rustup toolchain and lets cargo
        fetch and unpack its registry first, minutes of network on this board --
        and that happens once, so billing it to every unit still to come inflates
        the whole projection by it.

        The projection is withdrawn once the unit in flight has run longer than it
        says the entire build has left. The pace was measured over other units, and
        a unit that has already outlasted the time allowed for everything remaining
        has shown that pace not to describe it. Every build here ends with such a
        unit: Reckless's last crate compiles the engine with fat LTO across all 35
        crates before it, so their pace projects about a minute for a unit that
        runs for tens of them. Publishing nothing leaves the unit's own elapsed
        time -- which is measured, and displayed beside it -- as the reading, where
        republishing a minute that has already been disproved does not.
        """
        if fraction is None or fraction < _MIN_FRACTION_FOR_ETA:
            return None
        if self._first_unit_at is None:
            return None
        compiling_seconds = max(0.0, now - self._first_unit_at)
        if compiling_seconds <= 0.0:
            return None
        remaining = compiling_seconds / fraction - compiling_seconds
        if current_unit_seconds is not None and current_unit_seconds > remaining:
            return None
        return int(remaining)

    def _describe_unit(self, unit: str | None) -> str | None:
        """The name to show for ``unit``: a crate's own name, else the path given."""
        return None if unit is None else self._unit_labels.get(unit, unit)

    def _finished_units(self) -> set[str]:
        """Units known complete: those observed finishing, plus those overtaken.

        The second group is what a sampler cannot see directly. Claudia's build
        finished with only six of fourteen units ever sampled; the input order
        supplies the rest.
        """
        finished = self._seen_units - self._active_units
        if self._declared_units:
            finished |= set(self._declared_units[:self._max_ordinal])
        return finished

    def _total_units(self) -> int:
        """Return the unit denominator, never below what has already been seen.

        Where no command line declared the whole unit set, the count comes from the
        source directories observed being compiled. Those were resolved when they
        were recorded, against the working directory of the process that named
        them, so nothing here needs a base to guess at. A Rust build is counted in
        crates instead, because that is what its units are.
        """
        if self._declared_units:
            return max(len(self._declared_units), len(self._seen_units))
        if self._crate_units:
            if self._locked_packages is None:
                self._locked_packages = _count_locked_packages(self._source_root)
            return max(self._locked_packages, len(self._seen_units))
        counted = 0
        for directory in self._source_dirs:
            try:
                counted += sum(
                    1 for entry in Path(directory).iterdir()
                    if entry.is_file() and _is_source_path(entry.name)
                )
            except OSError:  # noqa: S112  # nosec B112  # an unlistable source dir is expected (a generated or out-of-tree layout); it contributes nothing and the seen-unit floor below keeps the total usable, so logging per poll would flood the log for no decision
                continue
        return max(counted, len(self._seen_units))

    def _fraction(self, finished: set[str], total_units: int) -> float | None:
        """Completed share of the work, weighted by source size where readable.

        The denominator is every unit the build has, not only the units already
        named. Where the unit set is discovered by watching -- any build that
        invokes the compiler once per file, and every cargo build -- the units with
        a known cost are exactly those already sampled, and all but the one in
        flight are finished. Dividing by them alone therefore reports (n-1)/n
        whatever remains: on the board a Reckless build read 94% and "less than a
        minute remaining" at its 17th module of roughly 120.

        Units not yet reached have no name and so no readable size; each is costed
        at the mean of the units whose size is known, which is the same rule an
        unreadable source path already follows.

        Monotonic and capped below 1.0: samples are noisy (a poll can land between
        units), and a bar that retreats or completes early reads as a fault.
        """
        if total_units <= 0:
            return None
        units = self._declared_units or sorted(self._seen_units)
        weights = {unit: self._weight(unit) for unit in units}
        known_weight = sum(weights.values())
        if known_weight <= 0:
            return None
        unreached = max(0, total_units - len(weights))
        total_weight = known_weight + unreached * known_weight / len(weights)
        done_weight = sum(weights.get(unit, 0.0) for unit in finished)
        fraction = min(done_weight / total_weight, _MAX_OBSERVED_FRACTION)
        self._max_fraction = max(self._max_fraction, fraction)
        return self._max_fraction

    def _weight(self, unit: str) -> float:
        """Relative cost of a unit: its source size, or the mean where unknown.

        Size is a proxy for compile time, and a coarse one, but the spread it
        captures is real: CT800's largest unit is 30x the mean and took longer than
        the rest of the build. Units whose size cannot be read (generated sources,
        an out-of-tree layout) fall back to the mean of those that can, so an
        unreadable path costs accuracy rather than the whole reading.
        """
        if unit not in self._sizes:
            self._sizes[unit] = self._source_bytes(unit)
        size = self._sizes[unit]
        if size is not None:
            return float(size)
        known = [value for value in self._sizes.values() if value is not None]
        return float(sum(known) / len(known)) if known else 1.0

    def _source_bytes(self, unit: str) -> int | None:
        """Size of ``unit``'s source, or None where no size describes the unit.

        A crate has no such size. It compiles as one ``rustc`` invocation covering
        every module in it, while the only file named is the crate root -- commonly
        a short list of ``mod`` declarations whatever the crate holds. Reading it
        would rank crates by a number unrelated to their cost; reporting no size
        instead costs each crate the mean weight, which asserts nothing false about
        how they compare.
        """
        if unit in self._crate_units:
            return None
        base = self._unit_bases.get(unit)
        path = _resolve_against(
            Path(unit),
            Path(base) if base is not None else self._source_root,
        )
        try:
            return path.stat().st_size
        except OSError:
            return None
