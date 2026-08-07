"""Tests for build progress derived from the compiler's own processes.

Why this module exists: a source build on a constrained board is the longest and
least observable phase of an engine install. Rodent IV compiles with a single
``g++ ... src/*.cpp`` invocation and ``-w``, so it prints nothing at all between
start and finish -- on a Pi Zero W that is 13m51s of total silence (measured).
Output lines therefore cannot drive either a progress bar or a hang detector.

What is observable: gcc/clang fork one backend process (``cc1``/``cc1plus``) per
translation unit and name the source file on its command line, and the driver
process names every input it was given. Sampling the build's process tree
therefore yields real progress -- which file is compiling, how many are done, how
many there are -- for a build that prints nothing.

The logic here is pure: the process table is injected, so these tests describe
real recorded process shapes without needing ``/proc`` or a live compiler.

Numbers used below are measurements from a Raspberry Pi Zero W (1 ARMv6 core,
426 MB RAM), not invented figures; each is named as a constant explaining what it
came from.
"""

from pathlib import Path

import pytest

from universalchess.services.build_progress import (
    BuildCeiling,
    BuildProgressTracker,
    BuildReportReader,
    ProcessInfo,
)

# The ceiling Maia was given on the Pi Zero W (4x its 3600s catalog estimate), and
# the one that killed its build at four hours while it was still compiling.
_MAIA_CEILING_SECONDS = 14400

# Maia builds lc0 through meson/ninja with CC=clang, which differs from every other
# catalog engine in two ways that together defeat process observation: ninja invokes
# the compiler once per file (so no command line ever declares the whole unit set),
# and clang compiles in-process rather than forking a named cc1plus child (so no
# per-unit backend is ever visible). Real shape, one file per invocation.
_MAIA_NINJA_CLANG_ARGS = (
    "clang++", "-Isrc", "-fdiagnostics-color=always", "-O3", "-DNDEBUG",
    "-o", "src/chess/board.cc.o", "-c", "../../src/chess/board.cc",
)
# The number of ninja edges a clean lc0 build reports on the board, which is the
# denominator build-maia.sh persists across resumes.
_MAIA_TOTAL_EDGES = 259

# --- measured inputs ------------------------------------------------------
# Rodent IV on a Pi Zero W: one g++ driver naming every input, 33 units, 831s.
_RODENT_UNITS = [f"src/unit{index:02d}.cpp" for index in range(33)]

# CT800: 8 units where one generated table dominates. Sizes are the real byte
# counts from the board -- bookdata.c is 4.4x the next largest file and ~30x the
# mean, and its single unit ran for over three minutes while the other seven took
# about a minute between them.
_CT800_DOMINANT_UNIT = "source/application/bookdata.c"
_CT800_DOMINANT_BYTES = 828_875
_CT800_SMALL_UNITS = {
    "source/application/eval.c": 186_866,
    "source/application/kpk_table.c": 153_684,
    "source/application/play.c": 140_855,
    "source/application/move_gen.c": 134_956,
    "source/application/book.c": 30_000,
    "source/application/hashtable.c": 20_000,
    "source/application/util.c": 10_000,
}

_BUILD_PID = 1000
_DRIVER_PID = 1001
_BACKEND_PID = 1002

# Cargo drives one ``rustc`` per crate, and that invocation names only the crate
# root (``lib.rs`` / ``main.rs``) -- never the modules the crate is made of. The
# crate source directories those invocations point at hold far more ``.rs`` files
# than there are crates: Reckless v0.9.0 resolves 36 locked packages, and the board
# displayed "module 17 of ~120" while compiling them.
_RECKLESS_LOCKED_PACKAGES = 36
# Files in one dependency's src/ directory, none of which is a unit of its own.
_CRATE_MODULE_FILES = 12

# Verbatim command lines captured from a Rodent IV build on the Pi Zero W (GCC 14,
# arm-linux-gnueabihf). Used instead of invented ones because a hand-written
# approximation missed the internal dump flags below and double-counted every unit:
# a 32-file build reported 65 units on the board.
_REAL_DRIVER_ARGS = (
    "g++", "-s", "-lm", "-Wl,--no-as-needed", "-latomic", "-g", "-w",
    "-Wfatal-errors", "-pipe", "-DNDEBUG", "-O3", "-fno-rtti", "-finline-functions",
    "-fprefetch-loop-arrays", "-DBOOKPATH=/usr/share/rodentIV", "-std=c++14",
    # The shell expands src/*.cpp before g++ sees it, so the inputs arrive listed.
    "src/eval.cpp", "src/search.cpp", "src/uci.cpp",
    "-o", "../rodentIV",
)
_REAL_BACKEND_ARGS = (
    "/usr/libexec/gcc/arm-linux-gnueabihf/14/cc1plus", "-quiet",
    "-imultilib", ".", "-imultiarch", "arm-linux-gnueabihf", "-D_GNU_SOURCE",
    "-D", "NDEBUG", "-D", "BOOKPATH=/usr/share/rodentIV",
    "src/eval.cpp",
    "-D_TIME_BITS=64", "-D_FILE_OFFSET_BITS=64", "-quiet",
    # These three are why a naive parse over-counts: -dumpbase names the same unit
    # again without its directory, and -dumpbase-ext's value is a bare extension
    # that a suffix test reads as a filename.
    "-dumpdir", "../rodentIV-", "-dumpbase", "eval.cpp", "-dumpbase-ext", ".cpp",
    "-mfloat-abi=hard", "-mtls-dialect=gnu", "-marm", "-mlibarch=armv6+fp",
    "-march=armv6+fp", "-g", "-O3", "-Wfatal-errors", "-w", "-std=c++14",
    "-fno-rtti", "-finline-functions", "-fprefetch-loop-arrays", "-o", "-",
)


def _write_sources(root: Path, sizes: dict) -> None:
    """Create source files of exact byte sizes so weighting reads real sizes.

    Sparse files keep this cheap: only the size is ever consulted.
    """
    for relative_path, size in sizes.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)


def _driver(units, *, pid=_DRIVER_PID, ppid=_BUILD_PID, cpu_ticks=1, cwd=None) -> ProcessInfo:
    """A g++ driver process naming every input, as a single-invocation build does."""
    return ProcessInfo(
        pid=pid, ppid=ppid, comm="g++", cpu_ticks=cpu_ticks, cwd=cwd,
        args=("g++", "-O3", "-c", *units, "-o", "engine"),
    )


def _backend(unit, **fields) -> ProcessInfo:
    """A compiler backend process, which names exactly the unit it is compiling.

    Remaining ProcessInfo fields are passed through so a test can vary any one of
    them (comm for a different toolchain, ppid for ancestry, cwd for a build that
    changes directory) without this helper growing a parameter per field.
    """
    comm = fields.pop("comm", "cc1plus")
    defaults = {"pid": _BACKEND_PID, "ppid": _DRIVER_PID, "cpu_ticks": 1}
    return ProcessInfo(
        comm=comm,
        args=(f"/usr/libexec/gcc/{comm}", "-quiet", unit, "-o", "-"),
        **{**defaults, **fields},
    )


def _rustc(crate_name, crate_root, **fields) -> ProcessInfo:
    """A ``rustc`` process shaped as cargo invokes it: one crate, one root file.

    The argument order and the option spellings are cargo's own, including the
    ``-L dependency=...`` and ``--out-dir`` pairs whose values must not be read as
    inputs, so the parse under test sees a real command line.
    """
    defaults = {"pid": _BACKEND_PID, "ppid": _BUILD_PID, "cpu_ticks": 1}
    return ProcessInfo(
        comm="rustc",
        args=(
            "rustc", "--crate-name", crate_name, "--edition=2024", crate_root,
            "--error-format=json", "--crate-type", "lib",
            "--emit=dep-info,metadata,link", "-C", "opt-level=3",
            "--out-dir", "/build/target/release/deps",
            "-L", "dependency=/build/target/release/deps",
        ),
        **{**defaults, **fields},
    )


def _write_cargo_lock(root: Path, package_count: int) -> None:
    """Write a Cargo.lock naming ``package_count`` resolved packages."""
    entries = "\n".join(
        f'[[package]]\nname = "crate{index}"\nversion = "1.0.0"\n'
        for index in range(package_count)
    )
    (root / "Cargo.lock").write_text(f'version = 4\n\n{entries}', encoding="utf-8")


def _table(*processes) -> dict:
    """A process table keyed by pid, including the build's own shell."""
    root = ProcessInfo(pid=_BUILD_PID, ppid=1, comm="sh", cpu_ticks=1,
                       args=("/bin/sh", "-c", "make"))
    return {process.pid: process for process in (root, *processes)}


def _tracker(tmp_path, **kwargs) -> BuildProgressTracker:
    return BuildProgressTracker(root_pid=_BUILD_PID, source_root=tmp_path, **kwargs)


class TestCurrentUnitDuration:
    """How long the module being compiled right now has been running."""

    def test_elapsed_is_measured_from_when_the_unit_first_appeared(self):
        """The current module reports how long it has been compiling.

        Why this test exists: a compiler publishes nothing about its progress
        through a single translation unit, so there is no honest percentage to show
        for the module in flight. Elapsed time is the one thing that can actually be
        measured, and it is what distinguishes a slow module from a wedged one --
        CT800's 829 KB bookdata.c holds the bar for minutes with no other signal.

        How a regression manifests: the timer restarts on every sample, so a long
        module always reads as having just begun.
        """
        tracker = _tracker(Path("/src"), started_at=0.0)
        sample = {
            _BUILD_PID: _driver(["eval.cpp"], pid=_BUILD_PID, ppid=0),
            _BACKEND_PID: _backend("eval.cpp", ppid=_BUILD_PID),
        }
        tracker.record(sample, 10.0)
        tracker.record(sample, 40.0)

        assert tracker.progress(40.0).current_unit_seconds == pytest.approx(30.0)

    def test_the_timer_restarts_when_the_next_module_begins(self):
        """Moving to a new module resets its elapsed time.

        Why this test exists: the number describes the module named beside it. If it
        kept accumulating it would silently become total build time while still
        being labelled as the current module's.

        How a regression manifests: elapsed time only ever grows, so every module
        after the first appears to have taken the whole build.
        """
        tracker = _tracker(Path("/src"), started_at=0.0)
        driver = _driver(["eval.cpp", "search.cpp"], pid=_BUILD_PID, ppid=0)
        tracker.record({
            _BUILD_PID: driver,
            _BACKEND_PID: _backend("eval.cpp", ppid=_BUILD_PID),
        }, 10.0)
        tracker.record({
            _BUILD_PID: driver,
            _BACKEND_PID: _backend("search.cpp", ppid=_BUILD_PID),
        }, 50.0)

        progress = tracker.progress(60.0)
        assert progress.current_unit == "search.cpp"
        assert progress.current_unit_seconds == pytest.approx(10.0)

    def test_no_duration_before_any_unit_is_seen(self):
        """Nothing is claimed until a module is actually observed.

        Why this test exists: an unobservable toolchain (Maia's ninja/clang) never
        names a unit. Reporting 0 seconds there would imply a module just started
        when none is known at all.

        How a regression manifests: the banner shows a module timer for a build
        whose modules cannot be seen.
        """
        tracker = _tracker(Path("/src"), started_at=0.0)
        tracker.record({
            _BUILD_PID: ProcessInfo(
                pid=_BUILD_PID, ppid=0, comm="ninja",
                args=("ninja", "-C", "build"), cpu_ticks=1,
            ),
        }, 10.0)

        assert tracker.progress(10.0).current_unit_seconds is None


class TestUnobservableToolchain:
    """What is reported when the compiler cannot be watched per unit."""

    def test_no_fraction_until_a_unit_has_actually_been_seen(self, tmp_path):
        """An unwatchable toolchain reports no fraction, not a fraction of zero.

        Why this test exists: Maia builds lc0 with ninja + clang, where no command
        line declares the unit set and no per-unit backend process exists, so no unit
        is ever observed. Returning 0.0 there is a fabricated measurement: it claims
        "none of the work is done" when the truth is "completion is unknown".
        compute_percent prefers any non-None build fraction over its time-based
        creep, so a fabricated 0.0 pins the bar at the bottom of the BUILDING band
        for the entire 45-60 minute build -- worse than the creep it replaced. None
        is the honest answer and the one that preserves the fallback.

        How a regression manifests: the fraction comes back as 0.0 instead of None,
        and a Maia install shows a frozen 35% for an hour.
        """
        tracker = _tracker(tmp_path)
        compile_step = ProcessInfo(
            pid=_DRIVER_PID, ppid=_BUILD_PID, comm="clang++", cpu_ticks=1,
            args=_MAIA_NINJA_CLANG_ARGS,
        )
        tracker.record(_table(compile_step), now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.fraction is None
        assert progress.eta_seconds is None
        assert progress.units_finished == 0

    def test_cpu_liveness_still_holds_without_unit_observation(self, tmp_path):
        """A build that cannot be counted must still register as alive.

        Why this test exists: stall detection and unit counting are deliberately
        independent -- CPU and I/O are accumulated for every process in the tree
        before the compiler-name filter. If they were coupled, Maia would be
        unobservable AND look wedged, and the stall window would kill an hour-long
        healthy build within two minutes.

        How a regression manifests: last_activity_at stops advancing for a build
        that is plainly burning CPU, so the stall timeout fires on a healthy build.
        """
        tracker = _tracker(tmp_path, started_at=0.0)
        for tick, now in ((1, 1.0), (2, 2.0)):
            tracker.record(
                _table(ProcessInfo(
                    pid=_DRIVER_PID, ppid=_BUILD_PID, comm="clang++",
                    cpu_ticks=tick, args=_MAIA_NINJA_CLANG_ARGS,
                )),
                now=now,
            )

        assert tracker.last_activity_at == 2.0
        assert tracker.progress(now=2.0).fraction is None


class TestBuildReportReader:
    """Exact progress a build reports about itself, for builds that cannot be watched."""

    def test_reads_reported_unit_counts_as_a_fraction(self):
        """A reported count becomes the completed fraction directly.

        Why this test exists: ninja knows exactly how many edges a build has, and
        build-maia.sh already reconciles that against resumes. Parsing the number it
        reports gives Maia exact progress where process observation gives none.

        How a regression manifests: the fraction stays None and Maia falls back to
        the time-based creep despite the build reporting real numbers.
        """
        reader = BuildReportReader()
        reader.read_line(f"UC_BUILD_PROGRESS units={_MAIA_TOTAL_EDGES // 2}/{_MAIA_TOTAL_EDGES}")

        progress = reader.progress()
        assert progress.units_finished == _MAIA_TOTAL_EDGES // 2
        assert progress.units_total == _MAIA_TOTAL_EDGES
        assert progress.fraction == pytest.approx(0.498, abs=0.01)

    def test_no_fraction_before_anything_is_reported(self):
        """Silence reports nothing, so the caller keeps its fallback.

        Why this test exists: same fabrication risk as the tracker -- a reader that
        answers 0.0 before the build has said anything would displace the creep from
        the first second of every build that never reports at all.

        How a regression manifests: engines whose scripts report nothing get pinned
        at the band floor instead of creeping.
        """
        assert BuildReportReader().progress().fraction is None

    def test_ignores_lines_that_are_not_progress_reports(self):
        """Ordinary build chatter must not be mistaken for a progress report.

        Why this test exists: the sentinel has to be unambiguous, because build logs
        contain bracketed counters, percentages and ratios of their own. Ninja's own
        "[123/456]" is deliberately NOT parsed here: its total is per-invocation and
        restarts on resume, which is the accounting build-maia.sh already corrects.
        Parsing it directly would reintroduce that bug in a second place.

        How a regression manifests: a resumed build reads a partial total and the
        percentage jumps backwards mid-install.
        """
        reader = BuildReportReader()
        for line in (
            "[14/245] Compiling C++ object src/chess/board.cc.o",
            "Progress: [14/259] (5%)",
            "ninja: build stopped at 42/259",
            "",
        ):
            reader.read_line(line)

        assert reader.progress().fraction is None

    def test_later_reports_supersede_earlier_ones(self):
        """The newest report wins, and the fraction never retreats.

        Why this test exists: reports arrive continuously, and a bar that moves
        backwards reads as a fault. A resume also re-reports lower absolute numbers
        briefly if accounting slips, so monotonicity is enforced here rather than
        assumed of the producer.

        How a regression manifests: the fraction follows a dip in the reported count
        and the bar visibly retreats.
        """
        reader = BuildReportReader()
        reader.read_line(f"UC_BUILD_PROGRESS units=200/{_MAIA_TOTAL_EDGES}")
        reader.read_line(f"UC_BUILD_PROGRESS units=100/{_MAIA_TOTAL_EDGES}")

        assert reader.progress().units_finished == 200

    @pytest.mark.parametrize("line", [
        "UC_BUILD_PROGRESS units=5/0",
        "UC_BUILD_PROGRESS units=notanumber/259",
        "UC_BUILD_PROGRESS units=",
        "UC_BUILD_PROGRESS",
    ])
    def test_malformed_reports_yield_no_fraction(self, line):
        """A malformed or zero-total report is discarded, never divided by.

        Why this test exists: the total is a denominator, so a zero or unparseable
        one must not raise mid-build or produce a nonsense fraction. The install
        would fail on a cosmetic logging bug.

        How a regression manifests: ZeroDivisionError or ValueError escapes the line
        callback and kills an otherwise healthy install.
        """
        reader = BuildReportReader()
        reader.read_line(line)

        assert reader.progress().fraction is None

    def test_reported_fraction_is_clamped_to_one(self):
        """An overshooting report cannot push the bar past its band.

        Why this test exists: the script derives its count arithmetically across
        resumes, so an accounting slip can exceed the total. A fraction above 1.0
        would drive the percentage past the stage band into a later stage's range.

        How a regression manifests: the install shows a percentage above the
        BUILDING band's ceiling before the build has finished.
        """
        reader = BuildReportReader()
        reader.read_line(f"UC_BUILD_PROGRESS units={_MAIA_TOTAL_EDGES + 10}/{_MAIA_TOTAL_EDGES}")

        assert reader.progress().fraction == 1.0


class TestTotalForOneFileAtATimeBuilds:
    """The denominator for a make that compiles each file in its own invocation.

    Why this class exists: Smallbrain (``cd src && make EXE=smallbrain``) reported
    "module 1 of 1", then "module 2 of 2", and so on -- a total that was only ever
    the count of units already seen, so the bar sat at 100% of an unknown build and
    the user could not tell how much was left.
    """

    def test_total_counts_the_sources_in_the_directory_being_compiled(self, tmp_path):
        """The denominator is the real file count, not the number seen so far.

        Why this test exists: no single command line declares the unit set in a
        one-file-per-invocation build, so the total has to come from the source
        directory. That directory is named relative to the compiler's own working
        directory, which is not where the build was launched -- Smallbrain's make
        runs from ``src`` while the installer launched it from the clone root.

        How a regression manifests: resolving against the launch directory finds no
        sources there, the count falls back to the units already seen, and the
        display reads "1 of 1" then "2 of 2" as it did on the board.
        """
        _write_sources(tmp_path, {f"src/unit{index}.cpp": 100 for index in range(6)})
        tracker = _tracker(tmp_path)

        # One backend, compiling a bare filename from inside src -- exactly what
        # `cd src && make` produces.
        tracker.record(
            _table(_backend("unit0.cpp", ppid=_BUILD_PID, cwd=str(tmp_path / "src"))), now=1.0,
        )

        assert tracker.progress(now=1.0).units_total == 6

    def test_the_total_holds_steady_as_further_units_appear(self, tmp_path):
        """Seeing more units does not inflate a total that is already known.

        Why this test exists: the reported symptom was the total tracking the count
        of seen units. Pinning the second and third samples is what distinguishes a
        real denominator from one that merely happens to match at the first sample.

        How a regression manifests: the total climbs with each new unit and every
        reading shows "N of N".
        """
        _write_sources(tmp_path, {f"src/unit{index}.cpp": 100 for index in range(6)})
        tracker = _tracker(tmp_path)
        source_dir = str(tmp_path / "src")

        totals = []
        for index in range(3):
            tracker.record(
                _table(_backend(f"unit{index}.cpp", ppid=_BUILD_PID, cwd=source_dir)), now=float(index),
            )
            totals.append(tracker.progress(now=float(index)).units_total)

        assert totals == [6, 6, 6]

    def test_seen_units_still_floor_the_total_when_the_directory_is_unreadable(
        self, tmp_path
    ):
        """An uncountable source directory degrades rather than reporting nonsense.

        Why this test exists: generated sources and out-of-tree layouts do exist,
        and the count must never drop below what has already been observed -- a
        total of zero would divide by nothing, and one below the seen count would
        report more units finished than exist.

        How a regression manifests: returning the directory count unconditionally
        yields 0 here, and the fraction calculation has no usable denominator.
        """
        tracker = _tracker(tmp_path)

        tracker.record(
            _table(_backend("unit0.cpp", ppid=_BUILD_PID,
                     cwd=str(tmp_path / "does-not-exist"))),
            now=1.0,
        )

        assert tracker.progress(now=1.0).units_total == 1


class TestRustCrateUnits:
    """A cargo build's unit is a crate, not a file.

    Why this class exists: Reckless is the catalog's only Rust engine, and every
    other toolchain here compiles one file per invocation. ``rustc`` compiles a
    whole crate per invocation and is handed only that crate's root file, so
    counting the ``.rs`` files sitting beside that root counts modules that are
    never units of their own. On the board that inflated a ~36-crate build to
    "~120" and rising, and it named every one of those crates "lib.rs".
    """

    def _record_crates(self, tracker, crates, *, root, at=1.0):
        """Sample one rustc process per crate, one crate per second."""
        for offset, (crate_name, crate_root) in enumerate(crates):
            tracker.record(
                _table(_rustc(crate_name, crate_root, cwd=str(root))),
                now=at + offset,
            )

    def test_the_denominator_is_the_locked_crate_count_not_the_module_count(
        self, tmp_path
    ):
        """The total counts crates to compile, not files in a crate's directory.

        Why this test exists: the denominator drives both the bar and the
        remaining-time projection, and for a Rust build it was reading the wrong
        kind of thing entirely -- every module file beside a crate root was counted
        as a unit that would never be compiled on its own. Cargo.lock names the
        resolved packages, which is the set of crates the build actually has to
        get through.

        How a regression manifests: the total becomes the number of ``.rs`` files
        in the sampled crate directories (24 here, and climbing with every new
        dependency), so the projected time left is several times the truth.
        """
        _write_cargo_lock(tmp_path, _RECKLESS_LOCKED_PACKAGES)
        for crate in ("bindgen", "syn"):
            _write_sources(tmp_path, {
                f"registry/{crate}/src/module{index}.rs": 500
                for index in range(_CRATE_MODULE_FILES)
            })
        tracker = _tracker(tmp_path)

        self._record_crates(tracker, [
            ("bindgen", str(tmp_path / "registry/bindgen/src/lib.rs")),
            ("syn", str(tmp_path / "registry/syn/src/lib.rs")),
        ], root=tmp_path)

        assert tracker.progress(now=3.0).units_total == _RECKLESS_LOCKED_PACKAGES

    def test_crates_seen_still_floor_the_total_when_the_lockfile_understates_it(
        self, tmp_path
    ):
        """The total never drops below the crates already compiled.

        Why this test exists: Cargo.lock is the resolved graph, not the build
        plan. It omits the build scripts cargo compiles as units of their own, so
        the real invocation count can exceed it; it also lists packages for other
        platforms that are never built. The count is therefore approximate in both
        directions, and the one thing it must never do is claim fewer crates than
        have already been observed finishing.

        How a regression manifests: with three crates seen against a two-package
        lockfile the total reads 2, so more units are reported finished than exist
        and the fraction exceeds 1.
        """
        _write_cargo_lock(tmp_path, 2)
        tracker = _tracker(tmp_path)

        self._record_crates(tracker, [
            ("libc", "/registry/libc/src/lib.rs"),
            ("cc", "/registry/cc/src/lib.rs"),
            ("reckless", "src/main.rs"),
        ], root=tmp_path)

        progress = tracker.progress(now=4.0)
        assert progress.units_total == 3
        assert progress.units_finished <= progress.units_total

    def test_a_crate_is_named_by_its_crate_name_not_its_root_file(self, tmp_path):
        """The module in flight is identified by something that distinguishes it.

        Why this test exists: every crate root in a cargo build is called
        ``lib.rs`` or ``main.rs``, so the install banner read "module 17 of ~120 -
        lib.rs" and then "module 18 of ~120 - lib.rs". The crate name is on the
        same command line and is the only part of it that says what is compiling.

        How a regression manifests: the current unit is the root file's path, so
        the banner names the same file for every crate in the build.
        """
        _write_cargo_lock(tmp_path, _RECKLESS_LOCKED_PACKAGES)
        tracker = _tracker(tmp_path)

        self._record_crates(
            tracker, [("regex_automata", "/registry/regex-automata/src/lib.rs")],
            root=tmp_path,
        )

        assert tracker.progress(now=2.0).current_unit == "regex_automata"

    def test_each_packages_build_script_is_counted_as_its_own_crate(self, tmp_path):
        """Crates that share a name are still separate units.

        Why this test exists: cargo compiles every package's build script under
        the crate name ``build_script_build``, and several packages in Reckless's
        graph have one. Identifying a unit by the crate name alone merges those
        into a single unit, which under-counts the work finished and makes a unit
        that already completed go active again later in the build. The crate root
        path is unique where the name is not.

        How a regression manifests: the two build scripts collapse into one unit,
        so only one unit is reported finished where two have compiled.
        """
        _write_cargo_lock(tmp_path, 4)
        tracker = _tracker(tmp_path)

        self._record_crates(tracker, [
            ("build_script_build", "/registry/libc/build.rs"),
            ("build_script_build", "/registry/clang-sys/build.rs"),
            ("libc", "/registry/libc/src/lib.rs"),
        ], root=tmp_path)

        assert tracker.progress(now=4.0).units_finished == 2

    def test_crates_are_weighted_equally_rather_than_by_their_root_file(self, tmp_path):
        """A crate's cost is not described by the size of its root file.

        Why this test exists: units are weighted by source size because a C
        translation unit's cost tracks its file size. A crate root does not: it is
        usually a short list of ``mod`` declarations for a crate of any size, so
        weighting by it would rank crates by an unrelated number. Here the second
        crate's root is 100x the first's while both are single crates.

        How a regression manifests: sizes leak back into crate weighting and the
        fraction after one of two crates is 0.01 or 0.99 instead of one half.
        """
        _write_cargo_lock(tmp_path, 2)
        _write_sources(tmp_path, {"tiny/src/lib.rs": 100, "huge/src/lib.rs": 10_000})
        tracker = _tracker(tmp_path)

        self._record_crates(tracker, [
            ("tiny", str(tmp_path / "tiny/src/lib.rs")),
            ("huge", str(tmp_path / "huge/src/lib.rs")),
        ], root=tmp_path)

        assert tracker.progress(now=3.0).fraction == pytest.approx(0.5)

    def test_a_rust_build_without_a_lockfile_falls_back_to_the_crates_seen(
        self, tmp_path
    ):
        """An unreadable lockfile costs the denominator, not the whole reading.

        Why this test exists: ``rustc`` can be driven without cargo, and a clone
        can be laid out so the lockfile is not where the build was launched. The
        reading must degrade to what has been observed rather than dividing by a
        count of files that are not units.

        How a regression manifests: a missing Cargo.lock makes the total zero, so
        there is no usable denominator and the fraction disappears for the build.
        """
        tracker = _tracker(tmp_path)

        self._record_crates(
            tracker, [("libc", "/registry/libc/src/lib.rs")], root=tmp_path,
        )

        progress = tracker.progress(now=2.0)
        assert progress.units_total == 1
        assert progress.fraction is not None


class TestReportedEta:
    """Projecting the time left for a build that publishes its own unit counts.

    Why this class exists: Maia's install on a Pi Zero W ran for four hours and
    reached "module 177 of 259 (68%)" without ever showing a remaining time,
    because only the process-observed path computed one. The user had no way to
    learn that this board needs six to eight hours for that build until four of
    them had been spent.
    """

    def test_no_estimate_from_a_single_report(self):
        """One report establishes a baseline but cannot imply a rate.

        Why this test exists: a rate needs two observations. Dividing the elapsed
        time by the first reported fraction assumes the build began when this run
        began, which is exactly the assumption that breaks on a resume.

        How a regression manifests: an estimate appears immediately and is wildly
        wrong, which is worse than showing none -- it was the reason the observed
        path already withholds one below 5%.
        """
        reader = BuildReportReader()
        reader.read_line(f"UC_BUILD_PROGRESS units=10/{_MAIA_TOTAL_EDGES}")

        assert reader.progress(now=100.0).eta_seconds is None

    def test_estimate_projects_the_remaining_units_at_the_observed_rate(self):
        """Remaining time comes from how long the completed units actually took.

        Why this test exists: this is the number that tells a user a build is a
        multi-hour job. The rate here is one unit per 60s over 10 units, with 20
        units left, so the projection is 1200s -- derived from the build's own
        pace on this board rather than from a catalog estimate written elsewhere.

        How a regression manifests: reverting to elapsed/fraction arithmetic gives
        a different number here, and reintroduces the resume bug below.
        """
        reader = BuildReportReader()
        reader.read_line("UC_BUILD_PROGRESS units=10/40")
        reader.progress(now=0.0)
        reader.read_line("UC_BUILD_PROGRESS units=20/40")

        assert reader.progress(now=600.0).eta_seconds == 1200

    def test_a_resumed_build_is_projected_from_this_runs_work_only(self):
        """Units finished by an earlier run do not inflate the measured rate.

        Why this test exists: build-maia.sh reconciles ninja's per-invocation
        counter across resumes, so a resumed build's first report can be well
        above zero. Treating those units as work done since this process started
        implies an enormous rate and a near-zero estimate, telling the user a
        six-hour build is minutes from finishing.

        How a regression manifests: projecting from the absolute count rather than
        the delta returns roughly 26s here instead of 1200s, because it credits
        this run with the 200 units a previous run compiled.
        """
        reader = BuildReportReader()
        reader.read_line("UC_BUILD_PROGRESS units=200/240")
        reader.progress(now=0.0)
        reader.read_line("UC_BUILD_PROGRESS units=210/240")

        assert reader.progress(now=600.0).eta_seconds == 1800

    def test_no_estimate_without_a_clock(self):
        """Callers that do not supply a time get counts but no projection.

        Why this test exists: the reader is also used purely to drive the bar, and
        an ETA needs a clock the caller owns. Fabricating one from a module-level
        time call would make the reader untestable and couple it to wall time.

        How a regression manifests: an internal clock makes the assertions above
        depend on real elapsed time and turn flaky.
        """
        reader = BuildReportReader()
        reader.read_line("UC_BUILD_PROGRESS units=10/40")

        assert reader.progress().eta_seconds is None


class TestBuildCeiling:
    """The backstop deadline, and when finishing work earns more of it.

    Why this class exists: Maia's build was killed at exactly four hours -- its
    ceiling of 4x a 3600s catalog estimate -- while healthy and advancing, at
    module 177 of 259. The ceiling had become the thing this whole design was
    meant to remove: a hardware-independent guess that kills working builds.
    """

    def test_work_completing_extends_the_deadline_past_the_base(self):
        """A build that keeps finishing units earns time beyond its base ceiling.

        Why this test exists: this is the Maia failure. The base ceiling here is
        the 14400s that killed it; a build still completing units with hours of
        projected work left must be allowed past it.

        How a regression manifests: reverting to a fixed deadline leaves the
        ceiling at the base and this build is killed while working, exactly as it
        was on the board.
        """
        ceiling = BuildCeiling(started_at=0.0, base_seconds=_MAIA_CEILING_SECONDS)

        ceiling.note_progress(now=14000.0, fraction=0.68, eta_seconds=6700)

        assert ceiling.deadline() > _MAIA_CEILING_SECONDS

    def test_the_deadline_stops_moving_when_work_stops_completing(self):
        """Time is granted for finished work, never for elapsed time.

        Why this test exists: this is what keeps the ceiling a real backstop. A
        compiler spinning in an infinite loop refreshes every liveness signal
        forever, so if the deadline advanced simply because the clock did, nothing
        would ever stop it. Grants are tied to the fraction increasing.

        How a regression manifests: extending on every call instead of on new work
        makes the deadline here keep climbing, and a runaway build runs forever.
        """
        # A base small enough that the first grant really does move the deadline,
        # so a deadline that never moves again is evidence and not a coincidence.
        ceiling = BuildCeiling(started_at=0.0, base_seconds=60)
        ceiling.note_progress(now=100.0, fraction=0.5, eta_seconds=100)
        granted = ceiling.deadline()
        assert granted > 60

        # The same fraction reported again later: the build is burning CPU but has
        # not finished anything since.
        for now in (200.0, 400.0, 800.0):
            ceiling.note_progress(now=now, fraction=0.5, eta_seconds=100)

        assert ceiling.deadline() == granted

    def test_the_deadline_is_never_shortened(self):
        """A shrinking projection cannot pull the deadline in.

        Why this test exists: the projection is an estimate and can fall as a
        build speeds up. If it could lower the deadline, a build could be killed
        because it started going faster.

        How a regression manifests: assigning the deadline instead of taking the
        later of the two brings it below the base and kills short builds early.
        """
        ceiling = BuildCeiling(started_at=0.0, base_seconds=_MAIA_CEILING_SECONDS)

        ceiling.note_progress(now=10.0, fraction=0.9, eta_seconds=1)

        assert ceiling.deadline() == _MAIA_CEILING_SECONDS

    @pytest.mark.parametrize(
        ("fraction", "eta_seconds"),
        [(None, 600), (0.5, None), (None, None)],
        ids=["no_fraction", "no_estimate", "nothing_observable"],
    )
    def test_an_unobservable_build_keeps_its_base_deadline(self, fraction, eta_seconds):
        """With nothing to project from, the base ceiling is what applies.

        Why this test exists: not every toolchain can be watched, and a build with
        no readings is precisely the case the fixed backstop exists for. It must
        not be extended on the strength of a missing measurement.

        How a regression manifests: treating a missing reading as zero progress
        with a zero estimate would extend the deadline to the current time on
        every sample, which both defeats the backstop and is arithmetic on data
        that does not exist.
        """
        ceiling = BuildCeiling(started_at=0.0, base_seconds=_MAIA_CEILING_SECONDS)

        ceiling.note_progress(now=500.0, fraction=fraction, eta_seconds=eta_seconds)

        assert ceiling.deadline() == _MAIA_CEILING_SECONDS


class TestRealCommandLines:
    """Parsing the exact command lines GCC produces on the target board."""

    def test_backend_dump_flags_do_not_invent_extra_units(self, tmp_path):
        """One backend invocation is exactly one unit, whatever GCC appends.

        Why this test exists: measured on the board, a 32-file Rodent IV build
        reported 65 units and finished "65 of 65". GCC passes ``-dumpbase eval.cpp``
        (the same unit again, without its directory) and ``-dumpbase-ext .cpp`` (a
        bare extension) to cc1plus, so a parser that skips only the documented
        path-taking options counts each unit twice and the extension once:
        32 + 32 + 1 = 65. That inflates the denominator and makes the percentage and
        the ETA wrong for every C/C++ engine in the catalog.

        How a regression manifests: the unit count roughly doubles and the reported
        current unit loses its directory, exactly as observed on the board.
        """
        tracker = _tracker(tmp_path)
        backend = ProcessInfo(
            pid=_BACKEND_PID, ppid=_BUILD_PID, comm="cc1plus", cpu_ticks=1,
            args=_REAL_BACKEND_ARGS,
        )
        tracker.record(_table(backend), now=1.0)

        progress = tracker.progress(now=1.0)
        # The path-qualified input, not the bare -dumpbase copy of it.
        assert progress.current_unit == "src/eval.cpp"
        assert progress.units_total == 1

    def test_real_driver_line_declares_exactly_its_inputs(self, tmp_path):
        """The driver's expanded input list is the unit set, with no flag artefacts.

        Why this test exists: the same over-counting risk applies to the driver,
        which carries ``-DBOOKPATH=/usr/share/rodentIV`` and ``-o ../rodentIV``.
        Neither is a translation unit, and the total is the denominator of the whole
        progress bar.

        How a regression manifests: a looser parse admits the -o target or a -D
        value, so the total exceeds the real file count and progress never reaches
        the top of its band.
        """
        tracker = _tracker(tmp_path)
        driver = ProcessInfo(
            pid=_DRIVER_PID, ppid=_BUILD_PID, comm="g++", cpu_ticks=1,
            args=_REAL_DRIVER_ARGS,
        )
        tracker.record(_table(driver), now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.units_total == 3
        assert progress.total_is_exact is True

    def test_driver_and_backend_agree_on_one_unit_set(self, tmp_path):
        """Together, the real driver and backend lines describe one consistent build.

        Why this test exists: this is the shape the board actually runs -- a single
        g++ invocation with every input, spawning one cc1plus per file. The count
        must equal the number of files, and the finished count must never exceed the
        total; "65 of 65 units" for a 32-file build was the observed failure.

        How a regression manifests: the totals diverge between the two sources and
        the finished count overruns the declared set.
        """
        tracker = _tracker(tmp_path)
        tracker.record(
            _table(
                ProcessInfo(pid=_DRIVER_PID, ppid=_BUILD_PID, comm="g++",
                            cpu_ticks=1, args=_REAL_DRIVER_ARGS),
                ProcessInfo(pid=_BACKEND_PID, ppid=_DRIVER_PID, comm="cc1plus",
                            cpu_ticks=1, args=_REAL_BACKEND_ARGS),
            ),
            now=1.0,
        )

        progress = tracker.progress(now=1.0)
        assert progress.units_total == 3
        assert progress.units_finished <= progress.units_total
        assert progress.current_unit == "src/eval.cpp"


class TestUnitDiscovery:
    """Which units exist, and which one is compiling right now."""

    def test_driver_command_line_yields_the_exact_unit_total(self, tmp_path):
        """A driver naming every input gives an exact total, not an estimate.

        Why: the total is the denominator of the whole progress bar. When one
        invocation lists its inputs, the total is knowable exactly, and saying so
        lets the caller trust the fraction (and the ETA derived from it) instead
        of hedging.

        How a regression manifests: if the driver's inputs are not parsed, the
        total falls back to counting files on disk -- which for Rodent IV includes
        sources the makefile does not compile, so the bar would under-report
        permanently and never reach the band ceiling.
        """
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(_RODENT_UNITS)), now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.observed is True
        assert progress.units_total == len(_RODENT_UNITS)
        assert progress.total_is_exact is True

    def test_backend_process_names_the_current_unit(self, tmp_path):
        """The unit in the backend's command line becomes the reported unit.

        Why: this is the only live status text available for a silent build. The
        installer shows it so a user watching a 14-minute compile can see it
        moving through files rather than facing a frozen message.

        How a regression manifests: reading the driver instead of the backend
        reports the first input forever, so the message never changes and the
        build looks hung.
        """
        tracker = _tracker(tmp_path)
        tracker.record(
            _table(_driver(_RODENT_UNITS), _backend(_RODENT_UNITS[4])), now=1.0
        )

        assert tracker.progress(now=1.0).current_unit == _RODENT_UNITS[4]

    @pytest.mark.parametrize(("comm", "unit"), [
        ("cc1plus", "src/search.cpp"),   # C++ (Rodent IV, Ethereal)
        ("cc1", "src/uci.c"),            # C (Claudia, CT800)
        ("rustc", "src/main.rs"),        # Rust (Reckless)
        ("compile", "engine/search.go"),  # Go (Zahak)
    ])
    def test_recognizes_each_toolchain_the_catalog_builds_with(self, tmp_path, comm, unit):
        """Every compiler the engine catalog uses must be observable.

        Why: the catalog builds C, C++, Rust and Go engines. A backend list that
        covers only gcc leaves Rust and Go installs with no observed progress,
        silently falling back to the guessed time-based bar this work replaces.

        How a regression manifests: dropping one entry makes that language's
        build report ``observed is False``, so its progress bar reverts to the
        elapsed-time creep and its stall detector loses its best signal.
        """
        tracker = _tracker(tmp_path)
        # Parented straight to the build shell: this shape (a backend with no
        # driver in the sample) occurs when a poll lands after the driver exited.
        tracker.record(_table(_backend(unit, comm=comm, ppid=_BUILD_PID)), now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.observed is True
        assert progress.current_unit == unit

    def test_processes_outside_the_build_tree_are_ignored(self, tmp_path):
        """Only descendants of the build process count.

        Why: the board compiles while the app and its engine keep running, and an
        unrelated compile (or a second install) must not be folded into this
        build's progress.

        How a regression manifests: attributing by process name instead of
        ancestry lets a foreign cc1plus set ``current_unit`` to a file this build
        never compiles, and inflates its finished count.
        """
        foreign = _backend("/somewhere/else/foreign.cpp", pid=9999, ppid=1)
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(_RODENT_UNITS), foreign), now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.current_unit is None
        assert progress.units_finished == 0

    def test_unobservable_build_reports_no_progress_rather_than_zero(self, tmp_path):
        """An unreadable or unrecognized process tree is not "0% done".

        Why: zero observed units must be distinguishable from "the build compiles
        in a way this cannot see" (an unknown toolchain, or a kernel without the
        expected /proc contents). The caller uses that flag to keep the old
        time-based bar instead of pinning a real bar at 0.

        How a regression manifests: returning ``fraction=0.0`` with
        ``observed=True`` makes such a build sit at the band floor for its whole
        duration, which is worse than the behaviour being replaced.
        """
        tracker = _tracker(tmp_path)
        tracker.record({}, now=1.0)

        progress = tracker.progress(now=1.0)
        assert progress.observed is False
        assert progress.fraction is None


class TestFraction:
    """Turning observed units into a fraction the progress bar can use."""

    def test_units_not_yet_reached_are_counted_in_the_denominator(self, tmp_path):
        """The fraction is finished work over all the work, not over what was seen.

        Why this test exists: a build that invokes the compiler once per file
        declares no unit set, so the only units with a known cost are the ones
        already sampled -- and every one of those except the unit in flight is
        finished. Dividing by them alone therefore reports (n-1)/n, which is above
        90% from the eleventh unit onwards no matter how many remain. Measured on
        the board: a Reckless build showed 94% and "less than a minute remaining"
        while compiling its 17th module of roughly 120, with most of an hour left.

        How a regression manifests: the fraction here jumps to 1/2 -- one of the
        two units seen -- instead of 1/6, and the ETA derived from it collapses
        towards zero for the whole build.
        """
        _write_sources(tmp_path, {f"src/unit{index}.c": 100 for index in range(6)})
        source_dir = str(tmp_path / "src")
        tracker = _tracker(tmp_path)

        tracker.record(
            _table(_backend("unit0.c", comm="cc1", ppid=_BUILD_PID, cwd=source_dir)),
            now=10.0,
        )
        tracker.record(
            _table(_backend("unit1.c", comm="cc1", ppid=_BUILD_PID, cwd=source_dir)),
            now=20.0,
        )

        progress = tracker.progress(now=20.0)
        assert progress.units_finished == 1
        assert progress.units_total == 6
        assert progress.fraction == pytest.approx(1 / 6)

    def test_units_already_passed_are_counted_when_the_sampler_missed_them(self, tmp_path):
        """Progress is inferred from position in the input list, not just sightings.

        Why this test exists: a sampler that polls once per second misses units
        that compile faster than the poll interval. Measured on the board:
        Claudia's 14-unit build finished in 83s and only 6 units were ever seen in
        a backend process, so a sightings-only count reported 43% at the moment
        the build completed. Because a driver compiles its inputs in the order
        given, seeing unit N in progress proves units before it are done -- which
        recovers the missed ones exactly.

        How a regression manifests: counting only observed units makes the bar
        badly under-report on fast builds (43% at completion) and then jump.
        """
        units = [f"src/f{index}.c" for index in range(14)]
        tracker = _tracker(tmp_path)
        # Only the final unit is ever sampled; the first thirteen were missed.
        tracker.record(_table(_driver(units), _backend(units[13], comm="cc1")), now=80.0)

        progress = tracker.progress(now=80.0)
        assert progress.units_finished == 13
        assert progress.fraction == pytest.approx(13 / 14)

    def test_a_dominant_unit_does_not_let_the_bar_run_ahead_of_the_work(self, tmp_path):
        """Units are weighted by source size, not counted equally.

        Why this test exists: CT800's eight units are wildly unequal --
        bookdata.c is 828875 bytes, 4.4x the next file, and on the board its
        single unit ran over three minutes while the other seven took about a
        minute in total. Counting units equally reports 7/8 = 88% with most of the
        real work still ahead, so the bar freezes near the ceiling for minutes,
        which is the exact "sits at 95%" complaint this work addresses.

        How a regression manifests: reverting to equal weights makes the asserted
        fraction jump to 0.875 while roughly half the compile time remains.
        """
        sizes = dict(_CT800_SMALL_UNITS)
        sizes[_CT800_DOMINANT_UNIT] = _CT800_DOMINANT_BYTES
        _write_sources(tmp_path, sizes)
        # The dominant unit is compiled last, as it is in the real build.
        units = [*_CT800_SMALL_UNITS, _CT800_DOMINANT_UNIT]

        tracker = _tracker(tmp_path)
        tracker.record(
            _table(_driver(units), _backend(_CT800_DOMINANT_UNIT, comm="cc1")), now=60.0
        )

        progress = tracker.progress(now=60.0)
        assert progress.units_finished == len(_CT800_SMALL_UNITS)
        small_bytes = sum(_CT800_SMALL_UNITS.values())
        expected = small_bytes / (small_bytes + _CT800_DOMINANT_BYTES)
        assert progress.fraction == pytest.approx(expected)
        # The honest reading is well under the naive count, with over half the
        # bytes still to compile.
        assert progress.fraction < 0.55

    def test_sizes_resolve_against_the_compilers_own_directory(self, tmp_path):
        """Unit paths are resolved from the compiler's cwd, not the clone root.

        Why this test exists: Rodent IV -- the engine whose install failed -- builds
        with ``cd sources && make``, so the compiler runs one directory below the
        clone root it was handed and names its inputs as ``src/eval.cpp`` relative to
        that. Resolving those against the clone root finds nothing, which silently
        drops every unit to an equal weight and reintroduces the uneven-cost problem
        for the exact engine this work is about. Reading each process's own working
        directory makes the resolution correct without the caller having to know how
        a build script navigates.

        How a regression manifests: resolving from the root only, the weights all
        collapse to the fallback and the fraction becomes a plain unit count, so the
        dominant unit's share (asserted below) is wrong.
        """
        build_root = tmp_path / "rodentIV"
        compiler_cwd = build_root / "sources"
        units = [*_CT800_SMALL_UNITS, _CT800_DOMINANT_UNIT]
        _write_sources(compiler_cwd, {**_CT800_SMALL_UNITS,
                                      _CT800_DOMINANT_UNIT: _CT800_DOMINANT_BYTES})

        # The tracker is given the clone root, as the installer does; the processes
        # report the subdirectory they actually run in.
        tracker = BuildProgressTracker(root_pid=_BUILD_PID, source_root=build_root)
        tracker.record(
            _table(
                _driver(units, cwd=str(compiler_cwd)),
                _backend(_CT800_DOMINANT_UNIT, comm="cc1", cwd=str(compiler_cwd)),
            ),
            now=1.0,
        )

        small_bytes = sum(_CT800_SMALL_UNITS.values())
        expected = small_bytes / (small_bytes + _CT800_DOMINANT_BYTES)
        assert tracker.progress(now=1.0).fraction == pytest.approx(expected)

    def test_equal_weights_are_used_when_sizes_cannot_be_read(self, tmp_path):
        """A build whose sources are not on disk still gets a fraction.

        Why: sizes are a refinement, not a requirement. Generated sources, an
        out-of-tree build directory, or a path this cannot resolve must degrade to
        equal weighting rather than producing no fraction at all.

        How a regression manifests: requiring a readable size raises or returns
        ``None`` for such builds, losing the observed bar entirely.
        """
        units = [f"generated/nowhere{index}.c" for index in range(4)]
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(units), _backend(units[2], comm="cc1")), now=5.0)

        assert tracker.progress(now=5.0).fraction == pytest.approx(2 / 4)

    def test_fraction_never_moves_backwards(self, tmp_path):
        """Progress is monotonic across samples.

        Why: the fraction drives a user-visible bar, and samples are noisy -- a
        poll can land when no backend is running (between units) or catch a
        recompile of an earlier file. A bar that goes backwards reads as a fault.

        How a regression manifests: recomputing from the current sample alone
        makes the second assertion drop below the first, so the bar visibly
        retreats between two polls.
        """
        units = [f"src/f{index}.c" for index in range(10)]
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(units), _backend(units[8], comm="cc1")), now=10.0)
        advanced = tracker.progress(now=10.0).fraction

        # A later sample catches a gap between units: no backend is running.
        tracker.record(_table(_driver(units)), now=11.0)

        assert tracker.progress(now=11.0).fraction >= advanced

    def test_fraction_stays_below_one_until_the_build_exits(self, tmp_path):
        """Observed progress never reports fully complete.

        Why: the last unit finishing is not the build finishing -- linking follows,
        and on this board a link is not instant. Reporting 1.0 would drive the bar
        to its ceiling and then hold, reintroducing the frozen-bar symptom. The
        build's exit status, not this fraction, decides completion.

        How a regression manifests: allowing 1.0 shows a full build bar while the
        linker is still running.
        """
        units = ["src/only.c"]
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(units), _backend(units[0], comm="cc1")), now=5.0)
        # The unit's backend is gone: every unit is compiled, link in progress.
        tracker.record(_table(_driver(units)), now=6.0)

        assert tracker.progress(now=6.0).fraction < 1.0


class TestEta:
    """The remaining-time estimate shown next to the bar."""

    def test_eta_extrapolates_from_observed_work(self, tmp_path):
        """Remaining time comes from measured rate, not a catalog guess.

        Why this test exists: the catalog's per-engine estimates are wrong in both
        directions on the same board -- measured, Rodent IV took 1.73x its
        estimate while Claudia took 0.46x -- so no fixed number describes a real
        install. Extrapolating from work actually completed does.

        How a regression manifests: deriving the ETA from the static estimate
        makes this fail for any build whose real rate differs from the guess,
        which is every build measured so far.
        """
        units = [f"src/f{index}.c" for index in range(10)]
        tracker = _tracker(tmp_path)
        # The first unit starts immediately, so compiling and the command begin
        # together and the measured rate spans the whole run.
        tracker.record(_table(_driver(units), _backend(units[0], comm="cc1")), now=0.0)
        # Half the units done after 100s implies about 100s remaining.
        tracker.record(_table(_driver(units), _backend(units[5], comm="cc1")), now=100.0)

        assert tracker.progress(now=100.0).eta_seconds == pytest.approx(100, abs=1)

    def test_eta_excludes_the_setup_that_ran_before_the_first_unit(self, tmp_path):
        """The rate is measured over compiling, not over everything the command did.

        Why this test exists: a build command is more than a compile. Reckless's
        bootstraps a pinned rustup toolchain over the network and lets cargo fetch
        and unpack its registry before ``rustc`` runs once, which on this board is
        minutes of the elapsed time. Charging that to the compile rate makes every
        unit look far more expensive than it is, and the error grows with how slow
        the board's network was.

        How a regression manifests: dividing total elapsed time by the completed
        fraction bills the 300s of setup here to five units, so the projection is
        400s where the compiling that has actually happened implies 100s.
        """
        units = [f"src/f{index}.c" for index in range(10)]
        tracker = _tracker(tmp_path)
        # Five minutes of the command with nothing compiling: no unit is visible.
        tracker.record(_table(), now=299.0)
        tracker.record(_table(_driver(units), _backend(units[0], comm="cc1")), now=300.0)
        tracker.record(_table(_driver(units), _backend(units[5], comm="cc1")), now=400.0)

        assert tracker.progress(now=400.0).eta_seconds == pytest.approx(100, abs=2)

    def test_the_eta_is_dropped_once_the_module_in_flight_outlasts_it(self, tmp_path):
        """A projection observation has already overtaken is withdrawn, not repeated.

        Why this test exists: a rate measured over the modules already compiled
        says nothing about a module unlike them, and every build here ends with
        one. Reckless's last crate compiles the engine with fat LTO across all 35
        crates before it, so at 35 of 36 done the measured pace projects about a
        minute for a module that runs for tens of them -- the "less than a minute
        remaining" that stood for the rest of the build. Once the module has run
        longer than the projection allows for the whole remainder, the projection
        is known to be wrong, and the module's own elapsed time (shown beside it)
        is the honest reading.

        How a regression manifests: the stale projection keeps being published, so
        the install advertises a minute left for as long as the final module runs.
        """
        units = [f"src/f{index:02d}.c" for index in range(20)]
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(units), _backend(units[0], comm="cc1")), now=0.0)
        # 19 of 20 done in 100s; the last module has only just started, so the
        # measured pace is not yet contradicted by anything.
        tracker.record(_table(_driver(units), _backend(units[19], comm="cc1")), now=100.0)
        assert tracker.progress(now=100.0).eta_seconds == pytest.approx(5, abs=1)

        # 100s later that module is still running, having outlasted the ~5s the
        # projection allowed for everything that was left.
        tracker.record(_table(_driver(units), _backend(units[19], comm="cc1")), now=200.0)

        progress = tracker.progress(now=200.0)
        assert progress.current_unit_seconds == pytest.approx(100.0)
        assert progress.eta_seconds is None

    def test_no_eta_while_too_little_work_has_finished_to_extrapolate_from(self, tmp_path):
        """An ETA is withheld until enough work is done for the projection to mean anything.

        Why this test exists: observed on the board, a Rodent IV build reported
        5689s remaining 31s in (at 0.5% complete), then 2054s, before settling near
        650s for a build that took about 11 minutes. Extrapolating from a fraction
        that small is dominated by fixed startup costs -- make's own startup, the
        first unit's warm-up -- so the first number the user sees is wrong by an
        order of magnitude and then visibly collapses, which reads as a fault.

        How a regression manifests: dropping the floor republishes that wild early
        figure, and the install shows an alarming remaining time that immediately
        contradicts itself.
        """
        units = [f"src/unit{index:02d}.cpp" for index in range(200)]
        tracker = _tracker(tmp_path)
        # One unit of two hundred: real progress, but far too little to project from.
        tracker.record(_table(_driver(units), _backend(units[1])), now=10.0)

        progress = tracker.progress(now=10.0)
        assert progress.fraction is not None
        assert progress.fraction > 0.0
        assert progress.eta_seconds is None

    def test_no_eta_before_any_unit_completes(self, tmp_path):
        """An ETA is withheld until there is work to extrapolate from.

        Why: dividing by a zero fraction has no meaningful result, and showing a
        fabricated remaining time at the start of a build is worse than showing
        none -- it is the guessing this work removes.

        How a regression manifests: extrapolating from zero yields infinity or a
        nonsense figure rendered to the user in the install status.
        """
        tracker = _tracker(tmp_path)
        tracker.record(_table(_driver(_RODENT_UNITS), _backend(_RODENT_UNITS[0])), now=3.0)

        assert tracker.progress(now=3.0).eta_seconds is None


class TestLiveness:
    """The signal that separates a slow build from a stalled one."""

    def test_advancing_cpu_time_is_progress_even_with_no_new_units(self, tmp_path):
        """A single long unit keeps the build alive.

        Why this test exists: CT800 spent over three minutes inside one unit and
        Rodent IV printed nothing for 13m51s, so neither new units nor new output
        can be required as proof of life. Consumed CPU time is the signal that
        covers those phases -- and linking, and toolchains whose units cannot be
        parsed at all.

        How a regression manifests: requiring a unit change or an output line
        marks these healthy builds stalled and kills them, which is the field
        failure being fixed, merely with a different message.
        """
        unit = _CT800_DOMINANT_UNIT
        tracker = _tracker(tmp_path)
        tracker.record(
            _table(_backend(unit, comm="cc1", ppid=_BUILD_PID, cpu_ticks=500)), now=10.0
        )
        first = tracker.last_activity_at

        # Same unit, no output, but the compiler has consumed more CPU.
        tracker.record(
            _table(_backend(unit, comm="cc1", ppid=_BUILD_PID, cpu_ticks=9000)), now=200.0
        )

        assert tracker.last_activity_at > first
        assert tracker.last_activity_at == 200.0

    def test_a_tree_burning_no_cpu_and_saying_nothing_is_not_progress(self, tmp_path):
        """A wedged build stops refreshing its activity timestamp.

        Why: this is what the stall deadline detects. A build blocked forever (a
        child waiting on a lock or a dead network fetch) keeps its processes alive
        but consumes no CPU and emits nothing, so liveness must not be implied by
        the mere existence of processes.

        How a regression manifests: treating a present process tree as activity
        means nothing is ever detected as stalled, and a genuinely hung install
        runs until the absolute ceiling instead of failing promptly.
        """
        unit = "src/blocked.c"
        tracker = _tracker(tmp_path)
        blocked = _backend(unit, comm="cc1", ppid=_BUILD_PID, cpu_ticks=42)
        tracker.record(_table(blocked), now=10.0)

        tracker.record(_table(blocked), now=600.0)

        assert tracker.last_activity_at == 10.0

    def test_an_output_line_counts_as_progress(self, tmp_path):
        """Build output refreshes liveness.

        Why: a chatty build (cmake, cargo) may print steadily while its process
        shapes are unhelpful, and output is unambiguous proof of work. Combining
        both signals means either one alone keeps a healthy build alive.

        How a regression manifests: ignoring output lets a verbose build whose
        compiler processes are not recognized trip the stall deadline.
        """
        tracker = _tracker(tmp_path)
        tracker.record(_table(), now=10.0)

        tracker.note_output(now=250.0)

        assert tracker.last_activity_at == 250.0
