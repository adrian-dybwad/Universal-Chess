"""Regression tests for engine source-build command definitions.

These pin platform-specific build flags in the ``ENGINES`` catalog that cannot
be exercised in CI (they only fail when compiling on the target board), so the
build-command string itself is the highest deterministic level at which the
regression can be guarded.
"""

import re
import subprocess  # nosec B404  # runs the repo's own build script to verify its real output
from pathlib import Path

import pytest

import universalchess.managers.engine_manager as engine_manager_module
from universalchess.managers.engine_manager import (
    _BUILD_PARALLELISM_ENV,
    ENGINES,
    SCRIPTS_DIR,
    _build_env,
    _build_parallelism,
    _mem_total_mb,
)
from universalchess.services.build_progress import BuildReportReader


def _build_script(engine_name: str) -> str:
    """Join an engine's build_commands into the single shell script that runs."""
    return "\n".join(ENGINES[engine_name].build_commands)


def _maia_script_packages() -> set:
    """The apt packages build-maia.sh declares it needs, from REQUIRED_PACKAGES.

    The script verifies these are present rather than installing them, so this
    list is the contract between the script and the engine catalog that supplies
    them.
    """
    text = (SCRIPTS_DIR / "build-maia.sh").read_text()
    match = re.search(r'^REQUIRED_PACKAGES="([^"]*)"', text, re.MULTILINE)
    assert match, "no REQUIRED_PACKAGES assignment in build-maia.sh"
    return set(match.group(1).split())


class TestMaiaProgressReporting:
    """The progress contract between build-maia.sh and the installer's parser."""

    @staticmethod
    def _emit(completed: int, total: int) -> str:
        """Run build-maia.sh's real progress emitter and capture what it prints.

        Sources the script and calls the function rather than reproducing its logic,
        so this exercises the shipped shell code. Captured output is a pipe, not a
        terminal, which is exactly the installer's situation and selects the
        machine-readable branch.
        """
        script = SCRIPTS_DIR / "build-maia.sh"
        # `trap - EXIT` clears the script's cleanup trap. The trap belongs to a real
        # build and fires when this sourced shell exits, printing its own log lines;
        # leaving it armed would mix that output into the progress being asserted.
        result = subprocess.run(  # noqa: S603  # nosec B603  # runs the repo's own script through a resolved interpreter, with no external input
            ["/bin/bash", "-c",
             f'source "{script}"; trap - EXIT; '
             f"report_build_progress {completed} {total}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_piped_progress_is_parsed_by_the_installers_reader(self):
        """The script's real output, through a pipe, drives the real reader.

        Why this test exists: the format contract is checked textually elsewhere, but
        that cannot catch the failure that actually occurred -- output written only
        with a carriage return never reaches a line-oriented reader. Running the
        shipped emitter with its stdout captured reproduces the installer's exact
        conditions and proves the whole producer-to-consumer path, across two
        languages, works end to end.

        How a regression manifests: the emitter takes its terminal branch under a
        pipe, or changes format, and the reader parses nothing -- so a 45-60 minute
        Maia install silently falls back to a time estimate.
        """
        reader = BuildReportReader()
        recognised = [
            reader.read_line(line)
            for line in self._emit(130, 259).splitlines()
            if line.strip()
        ]

        assert any(recognised), "no line the reader recognised"
        assert reader.progress().units_finished == 130
        assert reader.progress().units_total == 259
        assert reader.progress().fraction == pytest.approx(130 / 259)

    def test_piped_progress_rises_monotonically_across_a_build(self):
        """Successive readings drive the fraction forward, never backward.

        Why this test exists: the counts are derived arithmetically from ninja's
        per-invocation numbers plus a persisted total, so this checks the emitted
        sequence a real build produces actually increases through the reader rather
        than only the endpoints being right.

        How a regression manifests: the emitted count or total is computed wrongly at
        some point in the range and the bar stalls or retreats mid-build.
        """
        reader = BuildReportReader()
        fractions = []
        for completed in (0, 1, 65, 130, 258, 259):
            for line in self._emit(completed, 259).splitlines():
                reader.read_line(line)
            fractions.append(reader.progress().fraction)

        assert fractions == sorted(fractions)
        assert fractions[-1] == pytest.approx(1.0)

    def test_a_zero_total_publishes_nothing(self):
        """No total established yet means nothing is reported, not a division.

        Why this test exists: the emitter divides by the total to render a percentage.
        A zero would abort the build under `set -e` on a purely cosmetic logging path,
        turning a progress bug into a failed install.

        How a regression manifests: the emitter exits non-zero (asserted inside
        _emit) or prints a nonsense reading.
        """
        assert self._emit(0, 0).strip() == ""
        assert BuildReportReader().progress().fraction is None

    @staticmethod
    def _shell(arch: str, snippet: str) -> subprocess.CompletedProcess:
        """Run a snippet against the real script with `uname -m` reporting `arch`.

        Shadowing uname with a shell function is what allows the shipped
        architecture logic to be exercised for boards not on hand. The function is
        defined after sourcing because the script only calls uname from inside its
        functions, never at load time.
        """
        script = SCRIPTS_DIR / "build-maia.sh"
        return subprocess.run(  # noqa: S603  # nosec B603  # runs the repo's own script through a resolved interpreter, with no external input
            ["/bin/bash", "-c",
             f'source "{script}"; trap - EXIT; '
             f'uname() {{ echo "{arch}"; }}; {snippet}'],
            capture_output=True, text=True, timeout=30, check=False,
        )

    @pytest.mark.parametrize(("arch", "expected"), [
        ("armv6l", True),
        ("armv7l", True),
        ("aarch64", True),
        ("x86_64", True),
        ("riscv64", False),
    ])
    def test_architecture_gate_accepts_the_boards_this_project_targets(self, arch, expected):
        """ARMv6 must pass the gate, since Maia is offered on those boards.

        Why this test exists: the gate previously rejected armv6l, while the engine
        catalog placed no architecture restriction on Maia. A Pi Zero W user was
        therefore offered Maia, waited through dependency installation, and got
        "Unsupported architecture: armv6l". The catalog and the script have to agree
        on where Maia can be installed.

        How a regression manifests: armv6l falls through to the catch-all branch and
        the install dies before cloning.
        """
        result = self._shell(arch, "check_architecture")

        assert (result.returncode == 0) is expected, result.stdout + result.stderr

    @pytest.mark.parametrize(("arch", "targets_armv6"), [
        ("armv6l", True),
        ("armv7l", False),
        ("aarch64", False),
    ])
    def test_clang_is_aimed_at_the_actual_cpu_on_armv6(self, arch, targets_armv6):
        """On ARMv6 the compiler must be told to target ARMv6.

        Why this test exists: Raspbian's clang defaults to ``__ARM_ARCH 7`` even on
        a Pi Zero W, so every binary it produces -- including the trivial one meson
        compiles and *runs* as its configure-time sanity check -- dies with SIGILL
        on that hardware. The build therefore failed at "Executables created by cpp
        compiler clang++ are not runnable" before compiling a single line of lc0.
        Raspbian's gcc targets armv6 by default, which is why every other engine
        builds there and only Maia, which uses clang, broke.

        How a regression manifests: dropping these flags returns the install to
        failing during meson setup with a message that names the compiler rather
        than the architecture, roughly two minutes into a Maia install.
        """
        result = self._shell(arch, "arch_meson_options")

        assert result.returncode == 0, result.stderr
        # Both languages: meson applies each to its own sanity check, and abseil
        # compiles C as well as C++.
        for option in ("-Dcpp_args=", "-Dc_args="):
            targeted = any(
                line.startswith(option) and "arm1176jzf-s" in line
                for line in result.stdout.splitlines()
            )
            assert targeted is targets_armv6, f"{option} on {arch}: {result.stdout!r}"

    @pytest.mark.parametrize(("arch", "neon_disabled"), [
        ("armv6l", True),
        ("armv7l", False),
        ("aarch64", False),
    ])
    def test_neon_is_disabled_only_where_the_cpu_lacks_it(self, arch, neon_disabled):
        """ARMv6 must build without NEON; every other ARM must keep it.

        Why this test exists: lc0's `neon` meson option defaults to true and its
        meson.build adds -mfpu=neon unconditionally when set. The guard it uses only
        filters flags the compiler rejects, and clang accepts -mfpu=neon while
        targeting ARMv6 -- so NEON instructions are emitted for the ARM1176JZF-S,
        which has no NEON unit. The build succeeds and the binary dies with "Illegal
        instruction" on its first evaluation (upstream lc0 issue #1551). Disabling it
        everywhere would instead cost real speed on ARMv7 and ARMv8, which do have
        NEON, so this must be conditional rather than blanket.

        How a regression manifests: armv6l loses the flag and Maia installs
        "successfully" but crashes when it plays; or ARMv7/ARMv8 gain it and every
        capable board silently loses vector performance.
        """
        result = self._shell(arch, "arch_meson_options")

        assert result.returncode == 0, result.stderr
        assert ("-Dneon=false" in result.stdout) is neon_disabled

    @pytest.mark.parametrize(("arch", "lto_disabled"), [
        ("armv6l", True),
        ("armv7l", True),
        ("aarch64", False),
    ])
    def test_lto_stays_disabled_on_every_32_bit_arm(self, arch, lto_disabled):
        """The existing 32-bit LTO workaround must still cover ARMv6.

        Why this test exists: whole-program LTO of lc0 plus abseil exhausts the ~3 GB
        virtual address space on 32-bit ARM and the linker segfaults. ARMv6 is 32-bit
        and so needs the same workaround; adding it to the gate must not accidentally
        route it down the 64-bit path.

        How a regression manifests: armv6l keeps LTO on and the build fails at the
        final link after hours of compiling -- the most expensive possible failure.
        """
        result = self._shell(arch, "arch_meson_options")

        assert result.returncode == 0, result.stderr
        assert ("-Db_lto=false" in result.stdout) is lto_disabled
        # The abseil subproject must drop LTO too, or LTO bitcode survives in its
        # archives and re-triggers LTO codegen at link time despite the main override.
        assert ("-Dabseil-cpp:b_lto=false" in result.stdout) is lto_disabled

    def test_published_progress_is_newline_terminated(self):
        """Each reading must arrive as its own complete line.

        Why this test exists: this is the specific defect that made the script's
        correct counts useless. The installer iterates newline-separated lines, so a
        reading written only with a carriage return is never delivered -- it
        accumulates invisibly until some unrelated newline arrives. Asserting on the
        captured bytes catches that where a format check cannot.

        How a regression manifests: output comes back with no trailing newline, or
        several readings share one line, and the installer sees nothing until the
        build ends.
        """
        emitted = self._emit(130, 259)

        assert emitted.endswith("\n")
        assert "\r" not in emitted
        assert len([line for line in emitted.splitlines() if line.strip()]) == 1


class TestRodentIVBuildCommand:
    """Guards the 32-bit ARM libatomic link fix for Rodent IV."""

    def test_links_libatomic_for_arm(self):
        """Rodent IV must link libatomic on 32-bit ARM.

        Why this test exists: a field board (armhf) failed to install Rodent IV
        because its Makefile omits ``-latomic``. Rodent IV uses 64-bit
        ``std::atomic``; on 32-bit ARM those lower to libatomic calls
        (``__atomic_store_8`` etc.), so the link aborts with "undefined reference
        to __atomic_store_8" and the engine never builds.

        How the regression manifests: if someone reverts to the bare
        ``make ... EXENAME=../rodentIV`` command, ``-latomic`` disappears from the
        script below and this assertion fails -- mirroring the on-device link
        failure that cannot be reproduced in CI.
        """
        script = _build_script("rodentIV")
        assert "-latomic" in script

    def test_forces_libatomic_against_as_needed(self):
        """The atomic lib must be forced in with ``-Wl,--no-as-needed``.

        Why this test exists: the Makefile recipe places ``$(LDFLAGS)`` before the
        source objects, and the toolchain links ``--as-needed`` by default, which
        silently drops a ``-latomic`` that appears before any reference to it. Only
        ``-Wl,--no-as-needed`` keeps it linked in that position.

        How the regression manifests: dropping ``--no-as-needed`` would let the
        link succeed nowhere on 32-bit ARM despite ``-latomic`` being present, so
        this guards that the flag pairing stays intact.
        """
        script = _build_script("rodentIV")
        assert "-Wl,--no-as-needed" in script

    def test_atomic_flags_guarded_by_arm_arch_check(self):
        """The libatomic flags must be gated on a 32-bit ARM arch check.

        Why this test exists: 64-bit targets inline these atomics and need no
        libatomic; forcing ``--no-as-needed -latomic`` unconditionally would add an
        unused runtime dependency there. The fix gates the flags on ``uname -m``.

        How the regression manifests: if the arch guard is removed, the flags would
        apply on every architecture; this checks the ``uname -m`` ARM case remains.
        """
        script = _build_script("rodentIV")
        assert "uname -m" in script
        assert "arm" in script


class TestEtherealBuildCommand:
    """Guards Ethereal's compiler pin against the clang Makefile default."""

    def test_pins_cc_gcc(self):
        """Ethereal must build with ``CC=gcc``.

        Why this test exists: a field board failed to install Ethereal with
        "clang: not found". Ethereal's Makefile declares ``CC = clang`` and its
        default target is ``pgo``, so a bare ``make`` shells out to clang -- which
        is not in the engine's dependencies (build-essential provides gcc, not
        clang). Pinning ``CC=gcc`` makes the build use the compiler the deps
        actually install.

        How the regression manifests: if the command reverts to a bare
        ``make ... EXE=ethereal``, ``CC=gcc`` disappears from the script below and
        this assertion fails -- mirroring the on-device "clang: not found" abort
        that cannot be reproduced in CI (CI happens to have clang installed for
        other engines).
        """
        script = _build_script("ethereal")
        assert "CC=gcc" in script

    def test_does_not_depend_on_clang(self):
        """Ethereal must not require clang as a build dependency.

        Why this test exists: the point of the ``CC=gcc`` pin is to avoid pulling
        the heavyweight clang package onto a storage-constrained Pi. Declaring
        clang as a dependency would defeat that and reintroduce the coupling the
        fix removes.

        How the regression manifests: re-adding "clang" to ``dependencies`` (e.g.
        copying Demolito's entry) trips this assertion.
        """
        assert "clang" not in ENGINES["ethereal"].dependencies


# Arasan's NNUE network filename is version-specific (changes per release), so the
# build stages and installs it by glob rather than a hardcoded name. This lets the
# tag picker build any ref without a catalog edit. Guarded below.
ARASAN_NNUE_GLOB = "*.nnue"


class TestArasanBuildCommand:
    """Guards Arasan's arm64 clang/NEON recipe, release pin, and NNUE install.

    Each of these was an independent on-device build failure that compiles fine in
    isolation but breaks only on the target board, so the catalog definition is the
    highest deterministic level at which they can be guarded.
    """

    def test_binary_path_points_to_bin_not_src(self):
        """Arasan's binary_path must be ``bin/arasan``, never ``src/arasan``.

        Why this test exists: Arasan's Makefile writes the executable to
        ../bin/arasanx-<bits> (EXPORT=../bin), not to src/. The catalog previously
        declared binary_path=src/arasan, so every install compiled fine and then
        failed the post-build existence check with "Binary not found: src/arasan".
        The fix pairs EXE=arasan (fixed name) with binary_path=bin/arasan.

        How the regression manifests: reverting binary_path to src/arasan (or any
        src/ path) trips this and reintroduces the silent install failure.
        """
        engine = ENGINES["arasan"]
        assert engine.binary_path == "bin/arasan"
        assert not engine.binary_path.startswith("src/")

    def test_fixes_exe_name(self):
        """The build must pass ``EXE=arasan`` so the output name is deterministic.

        Why this test exists: without EXE the Makefile emits arasanx-$(LONG_BIT),
        which neither binary_path nor the CI cp can predict. EXE=arasan pins
        ../bin/arasan.

        How the regression manifests: dropping EXE=arasan reverts to the
        arch-dependent arasanx-<bits> name and the binary is not found post-build.
        """
        assert "EXE=arasan" in _build_script("arasan")

    def test_builds_with_clang(self):
        """Arasan must build with ``CC=clang++``.

        Why this test exists: the Makefile defaults to g++, which rejects Arasan's
        NEON vector-type conversions in nnue/simddefs.h ("cannot convert
        int16x8_t to int32x4_t" and similar). doc/BUILD.md states clang is the
        required compiler for ARM; clang accepts those conversions. A g++ build of
        this engine cannot succeed on the board.

        How the regression manifests: dropping CC=clang++ falls back to g++ and the
        on-device build dies in nnue/simddefs.h before producing any binary.
        """
        assert "CC=clang++" in _build_script("arasan")

    def test_requests_neon_unconditionally(self):
        """BUILD_TYPE=neon must be passed (Arasan is arm64-only).

        Why this test exists: Arasan's NNUE SparseLinear layer has
        ``static_assert(0, "requires SIMD")``, so a non-SIMD build does not compile
        at all -- BUILD_TYPE=neon is mandatory, not an optimization. Because the
        engine is gated to arm64 (the only arch with a NEON path), the flag is now
        unconditional rather than guarded by a uname check.

        How the regression manifests: removing BUILD_TYPE=neon selects the scalar
        path and the build fails on the static_assert.
        """
        assert "BUILD_TYPE=neon" in _build_script("arasan")

    def test_overrides_gold_linker(self):
        """The build must not link with the removed gold linker.

        Why this test exists: Arasan's Makefile hardcodes ``-fuse-ld=gold``, but the
        gold linker was removed from binutils 2.44 (Raspberry Pi OS Trixie), so the
        link aborts with "invalid linker name in argument '-fuse-ld=gold'". A
        command-line LDFLAGS assignment overrides the Makefile's append and drops the
        flag, falling back to the default bfd linker.

        How the regression manifests: dropping the LDFLAGS override lets the
        Makefile's -fuse-ld=gold through and the link fails on every board.
        """
        script = _build_script("arasan")
        assert "LDFLAGS=" in script
        assert "-fuse-ld=gold" not in script

    def test_pinned_to_release_tag(self):
        """Arasan must be pinned to a tagged release, not track master.

        Why this test exists: master's NEON path has regressed (the nnue/simd.h
        calcNnzData rewrite fails to compile), so an unpinned clone builds or fails
        depending on the day. Pinning to a verified release tag makes installs
        reproducible.

        How the regression manifests: clearing git_ref reverts to cloning master,
        reintroducing the intermittent compile failure.
        """
        assert ENGINES["arasan"].git_ref == "v25.4"

    def test_clones_submodules(self):
        """Arasan must clone with submodules.

        Why this test exists: the Syzygy probing code (syzygy/src/tbprobe.h) and the
        NNUE network live in git submodules. Without them the build dies at
        "syzygy/src/tbprobe.h: No such file or directory".

        How the regression manifests: setting clone_with_submodules False omits the
        submodules and the build fails compiling syzygy.cpp.
        """
        assert ENGINES["arasan"].clone_with_submodules is True

    def test_installs_nnue_network_by_glob(self):
        """The NNUE network must be staged and installed beside the binary via glob.

        Why this test exists: Arasan loads its network from the executable's own
        directory and embeds an exact, version-specific filename; without the file
        it refuses to evaluate ("failed to open network file"). The build copies the
        network to the repo root and extra_files installs it next to the engine. A
        glob (not a hardcoded name) is required so the tag picker can build any
        release -- a different tag ships a differently-named network.

        How the regression manifests: hardcoding a specific filename would let only
        that one release build (other tags' cp would match nothing and the engine
        would install without a network); dropping the cp/extra_files entirely
        installs a binary that cannot load its network.
        """
        engine = ENGINES["arasan"]
        assert ARASAN_NNUE_GLOB in engine.extra_files
        # The build must stage *.nnue from the submodule's network/ dir so whatever
        # network the checked-out ref ships lands beside the binary.
        assert "network/*.nnue" in _build_script("arasan")

    def test_requires_clang_not_bc_gawk(self):
        """Dependencies must include clang and must not include bc/gawk.

        Why this test exists: clang is required to compile (see test_builds_with_clang).
        bc/gawk are only used by the Makefile's g++ branch (to compute the gcc
        version); with clang they are unnecessary, and a missing bc previously
        produced a confusing "No 'bc' found" Makefile error on boards where apt was
        wedged. Keeping the dependency list accurate avoids installing tools the
        clang build never uses.

        How the regression manifests: re-adding bc/gawk reintroduces unused deps
        whose install failure could abort the build; dropping clang makes the build
        fail to find a usable compiler.
        """
        deps = ENGINES["arasan"].dependencies
        assert "clang" in deps
        assert "bc" not in deps
        assert "gawk" not in deps


class TestDemolitoBuildCommand:
    """Guards Demolito's compiler pin against the clang makefile default."""

    def test_pins_cc_gcc(self):
        """Demolito must build with ``CC=gcc``.

        Why this test exists: a field board (armhf) failed to install Demolito with
        "clang: not found". Demolito's makefile declares ``CC = clang`` and its
        default target runs ``$(CC) -march=native ...``. Installing clang on a
        32-bit Pi Zero pulls the whole LLVM toolchain and was failing/timing out,
        and because a failed dependency install is non-fatal the build proceeded
        without clang. Pinning ``CC=gcc`` uses the compiler build-essential
        provides; the makefile's clang-only flags are gated behind
        ``ifeq ($(CC),clang)``.

        How the regression manifests: dropping ``CC=gcc`` reverts to the clang
        default and the build aborts with "clang: not found" wherever clang is not
        installed.
        """
        assert "CC=gcc" in _build_script("demolito")

    def test_does_not_depend_on_clang(self):
        """Demolito must not require clang as a build dependency.

        Why this test exists: the ``CC=gcc`` pin exists precisely to avoid pulling
        clang onto the device. Declaring clang as a dependency would reintroduce
        the heavyweight install that was failing. After this change no device
        engine depends on clang.

        How the regression manifests: re-adding "clang" to ``dependencies`` trips
        this assertion.
        """
        assert "clang" not in ENGINES["demolito"].dependencies


class TestZahakBuildCommand:
    """Guards Zahak's Go-module build against the bare ``go build`` failure."""

    def test_builds_via_make_not_bare_go_build(self):
        """Zahak must build through ``make``, not a bare ``go build``.

        Why this test exists: a field board (armhf) failed to install Zahak with
        "no Go files in .../zahak". Zahak is a Go module whose ``main`` package
        lives in the zahak/ subdirectory, so ``go build`` in the repo root finds no
        Go files. The Makefile also runs a ``netgen`` step that generates
        engine/nn.go from default.nn (not committed); without it the engine package
        does not compile. ``make`` (default goal ``build``) does both.

        How the regression manifests: reverting to ``go build -o zahak`` drops
        ``make`` from the script and reintroduces the "no Go files" abort -- and,
        had the path issue not existed, a later compile failure from the missing
        generated network source.
        """
        script = _build_script("zahak")
        assert "make" in script
        assert "go build" not in script

    def test_disables_cgo(self):
        """The build must pass ``CGO_ENABLED=0``.

        Why this test exists: the Makefile defaults to ``CC=cc CGO_ENABLED=1`` to
        compile fathom's Syzygy probing C code, but Zahak declares only golang+git
        as dependencies -- no C compiler. With CGO enabled the build needs ``cc``;
        disabling it selects fathom_stub.go (no tablebase probing, irrelevant
        on-device) and keeps the build to the declared dependencies.

        How the regression manifests: dropping ``CGO_ENABLED=0`` reverts to the
        Makefile's CGO default and the build fails wherever a C toolchain is absent.
        """
        assert "CGO_ENABLED=0" in _build_script("zahak")

    def test_does_not_pin_parallelism(self):
        """Zahak must NOT hard-code Go compile parallelism in its command.

        Why this test exists: parallelism is centralized in the build environment
        (GOFLAGS=-p=N via _build_env) so it is one tunable knob, not a per-engine
        constant. An inline ``GOFLAGS=-p=1`` in FLAGS would override the
        environment value and silently pin Zahak to serial compiles regardless of
        the knob -- and the previous OOM that motivated -p=1 is now handled by the
        temporary build-memory swap, not by serializing.

        How the regression manifests: re-adding GOFLAGS=-p=… to the command makes
        the central knob a no-op for Zahak; this asserts the command carries no
        parallelism flag.
        """
        script = _build_script("zahak")
        assert "GOFLAGS=-p" not in script
        assert "-p=" not in script

    def test_binary_path_points_to_bin(self):
        """Zahak's binary_path must be ``bin/zahak``.

        Why this test exists: the Makefile emits the executable to bin/zahak. The
        ``EXE=`` move target is deliberately not used because the repo root already
        contains a zahak/ directory -- ``mv bin/zahak zahak`` would move the binary
        *into* that directory (verified on-device), so the post-build existence
        check would not find it at the repo root.

        How the regression manifests: setting binary_path to ``zahak`` (or adding
        ``EXE=zahak``) makes the binary land at zahak/zahak and the install fails
        the "Binary not found" check.
        """
        assert ENGINES["zahak"].binary_path == "bin/zahak"


class TestSourceInstallOwnership:
    """Guards that installing an engine from source needs no privileged chown."""

    def test_source_install_does_not_shell_out_to_sudo_chown(self):
        """The source-install path must not run ``sudo chown`` on the engines dir.

        Why this test exists: it used to run ``sudo chown -R pi:pi <engines_dir>``
        after building. That call was wrong three ways at once. There is no
        sudoers grant for chown, so it always failed and merely logged a warning;
        the user was hardcoded to ``pi``, so it named the wrong account on an
        install whose primary user is not pi; and it was unnecessary, because the
        build already runs as the service user and the postinst grants that user
        ownership of engines/ explicitly. Adding a grant to make it "work" would
        be worse still -- passwordless ``chown`` as root is a privilege
        escalation, since it can change ownership of any path on the system.

        How the regression manifests: no visible failure, just a warning on every
        source build plus a standing temptation to "fix" it by granting sudo
        chown, which would hand the service user a route to root.
        """
        source = Path(engine_manager_module.__file__).read_text()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "chown" in line and not line.strip().startswith("#")
        ]
        assert offenders == [], (
            f"engine install must not change ownership via sudo: {offenders}"
        )


class TestMaiaBuildCommand:
    """Guards Maia's build-script path against the dev-vs-installed layout bug.

    Maia is the one engine that shells out to a bundled build script instead of
    cloning and compiling inline, so its command embeds a filesystem path. That
    path must resolve on an *installed board*, not just in a dev checkout.
    """

    def test_commands_do_not_invoke_sudo(self):
        """Maia's build and repair commands must not use sudo.

        Why this test exists: these commands ran ``sudo build-maia.sh``, but the
        postinst grants passwordless sudo only to specific pinned helpers and
        build-maia.sh is not one of them. On a board without blanket passwordless
        sudo -- e.g. one provisioned by cloud-init rather than the userconf path
        that carries /etc/sudoers.d/010_pi-nopasswd forward -- that sudo is
        denied and the whole Maia install dies before the script runs. Root is
        not needed either: the only privileged step was apt, which now goes
        through uc-engine-deps, and everything else writes under
        /opt/universalchess, which the postinst chowns to the service user.

        How the regression manifests: reintroducing sudo makes Maia install and
        repair fail with "sudo: a password is required" on exactly the boards
        this change exists to support, while still passing on boards that happen
        to have an ambient NOPASSWD grant.
        """
        maia = ENGINES["maia"]
        for command in [*maia.build_commands, *maia.repair_commands]:
            assert "sudo" not in command.split(), (
                f"Maia command needs a sudoers grant that does not exist: {command}"
            )

    def test_declares_every_package_the_build_script_requires(self):
        """Maia must declare the build script's packages as engine dependencies.

        Why this test exists: build-maia.sh used to apt-install its own toolchain
        as root. It no longer can, so the packages must come from the catalog,
        which routes them through the pinned uc-engine-deps helper before the
        script runs. The script's list and the catalog's list are in different
        files and different languages, so they can drift.

        How the regression manifests: a package the script needs but the catalog
        omits is never installed, and the build fails partway through with a
        cryptic meson/compiler error (e.g. "Program 'ninja' not found") after
        the user has already waited -- the exact class of late, unreadable
        failure the dependency gate exists to prevent.
        """
        script_packages = _maia_script_packages()
        declared = set(ENGINES["maia"].dependencies)

        assert script_packages, "build-maia.sh declares no REQUIRED_PACKAGES"
        missing = script_packages - declared
        assert not missing, (
            f"build-maia.sh needs packages Maia does not declare: {sorted(missing)}"
        )

    def test_build_script_does_not_install_packages_itself(self):
        """build-maia.sh must not run apt.

        Why this test exists: the script runs unprivileged now. An apt call in it
        would fail for lack of root, and "fixing" that by restoring sudo would
        reintroduce the ungranted privileged call. Package installation belongs
        to uc-engine-deps, which is behind the one grant that authorizes it.

        How the regression manifests: adding an apt-get line back makes the
        script fail with a permissions error mid-install, or silently skip
        provisioning if the failure is tolerated.
        """
        commands = [
            line for line in (SCRIPTS_DIR / "build-maia.sh").read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        offenders = [line.strip() for line in commands if "apt-get " in line]
        assert not offenders, f"build-maia.sh still runs apt: {offenders}"

    def test_invokes_packaged_build_script_that_exists(self):
        """The command must invoke ``{SCRIPTS_DIR}/build-maia.sh`` and that file
        must exist.

        Why this test exists: the command previously used a REPO_ROOT computed as
        four parents up from engine_manager.py. That assumes a dev checkout
        (src/universalchess/managers/...). On a board the package is flattened to
        /opt/universalchess, so engine_manager.py sits two levels down and four
        parents overshoot to "/", producing the literal install failure
        "sudo: //scripts/engines/build-maia.sh: command not found". Worse, the
        script lived at the repo root (scripts/engines/) which is never packaged.

        How the regression manifests: reverting to a repo-root path makes
        SCRIPTS_DIR/build-maia.sh either point outside the package or not exist;
        this asserts the command names a real file inside the packaged scripts
        dir -- the only location both the .deb and deploy-to-pi.sh actually ship.
        """
        script_path = SCRIPTS_DIR / "build-maia.sh"
        command = _build_script("maia")
        assert str(script_path) in command
        assert script_path.is_file()

    def test_build_script_lives_inside_the_packaged_tree(self):
        """SCRIPTS_DIR must be the package's own scripts dir so the script ships.

        Why this test exists: the .deb tars src/universalchess into
        /opt/universalchess and deploy-to-pi.sh rsyncs the same tree; a script
        outside that tree (e.g. the repo-root scripts/) is absent on-device. The
        package dir is two parents up from engine_manager.py (managers ->
        universalchess), and its scripts/ subdir is where runtime helpers
        (uc-build-memory, bluez-selfheal) already live.

        How the regression manifests: pointing SCRIPTS_DIR at the repo root again
        moves it outside the package; this asserts the build script is a
        descendant of the package dir that gets deployed.
        """
        package_dir = Path(engine_manager_module.__file__).resolve().parent.parent
        assert SCRIPTS_DIR == package_dir / "scripts"
        script_path = SCRIPTS_DIR / "build-maia.sh"
        assert package_dir in script_path.parents

    def test_build_script_does_not_use_tmpfs_or_self_manage_swap(self):
        """build-maia.sh must build on disk and must not create its own swap.

        Why this test exists: on a 512 MB Pi /tmp is a ~208 MB RAM-backed tmpfs.
        The script originally placed both a 2 GB swapfile (/tmp/maia-build-swap)
        and the lc0 checkout/build tree (/tmp/maia-build-$$) there, so the install
        failed with "No space left on device" -- the swapfile write aborted
        outright, and once the caller already provisioned swap the clone filled the
        tiny tmpfs (the build's cleanup logged "Removing build directory"). Build
        headroom now comes solely from the caller (uc-build-memory), and the build
        directory lives on the SD card beside the install dir.

        How the regression manifests: reintroducing an absolute ``/tmp`` build dir
        or an in-script ``mkswap``/``swapon`` brings back the tmpfs space failure on
        low-RAM boards -- which only reproduces on-device, so the script text is the
        highest deterministic level to guard it.
        """
        content = (SCRIPTS_DIR / "build-maia.sh").read_text()
        # No absolute /tmp build dir (the disk path is <install>/../../tmp, derived
        # via dirname, which is fine -- only a leading "/tmp is the tmpfs).
        assert 'BUILD_DIR="/tmp' not in content
        assert "/tmp/maia-build-swap" not in content  # noqa: S108 - asserting a string is absent
        # Swap is provisioned by the caller (uc-build-memory), not here.
        assert "mkswap" not in content
        assert "swapon" not in content

    def test_build_is_resumable_across_interruptions(self):
        """build-maia.sh must reuse its build tree so a resumed install is incremental.

        Why this test exists: lc0 takes 30-60 min single-threaded on a Pi Zero 2 W.
        The script used a PID-suffixed build dir (``maia-build-$$``) and removed the
        whole tree on every exit, so an install interrupted near the end (e.g.
        250/259) restarted from 0/259 on the next attempt. A stable build dir that
        reuses the existing checkout and meson configuration lets ninja -- which
        records a target as done only after it completes -- pick up where it stopped.

        How the regression manifests: reintroducing a PID suffix, or an
        unconditional ``rm -rf $BUILD_DIR`` on failure, brings back the full restart
        -- which only surfaces as wasted hours on-device, so the script text is the
        deterministic level to guard it.
        """
        content = (SCRIPTS_DIR / "build-maia.sh").read_text()
        # Stable build dir: no per-process suffix that would orphan the prior tree.
        assert "maia-build-$$" not in content
        # The resume paths must exist: reuse an existing checkout, and reapply meson
        # options to the existing build dir (--reconfigure keeps unaffected objects
        # while still picking up recipe changes such as the LTO toggle).
        assert "Reusing existing lc0 checkout" in content
        assert "meson setup --reconfigure" in content

    # The LTO gate was previously pinned here by matching the script's source text.
    # It is now covered by executing arch_meson_options across each architecture in
    # TestMaiaProgressReporting, which checks the behaviour rather than the wording
    # and caught nothing the textual version did while surviving the refactor that
    # moved the logic into a function.

    def test_resume_progress_is_cumulative_not_per_invocation(self):
        """Resumed Maia builds must show cumulative progress, not ninja's per-run count.

        Why this test exists: ninja's "[current/total]" is per-invocation -- on a
        resume that already compiled 14 of 259 units it prints "[x/245]" starting at
        0/245. Raw, that looks like a restart from scratch even though the 14 units
        are reused. The script persists the full total and renders
        ``already_done + current`` of the full total so the bar continues (e.g.
        14/259) on resume.

        How the regression manifests: dropping the persisted-total file or the
        ``full_total - remaining`` computation reverts the display to ninja's raw
        "[0/245]", which is the exact confusing behavior this guards against.
        """
        content = (SCRIPTS_DIR / "build-maia.sh").read_text()
        # Full total is persisted with the build tree so resumes can read it back.
        assert ".uc-build-total" in content
        # already-completed is derived as full_total - remaining-this-run.
        assert "full_total - remaining" in content

    def test_build_script_passes_no_removed_nvcc_meson_option(self):
        """build-maia.sh must not pass the ``-Dnvcc`` meson option.

        Why this test exists: the script pins lc0 ``v0.32.1``, whose
        meson_options.txt has no boolean ``nvcc`` option (it was superseded by
        ``nvcc_ccbin``/``cc_cuda``). Passing ``-Dnvcc=false`` makes ``meson setup``
        abort immediately with ``Unknown options: "nvcc"``, so the build never
        starts -- a configure-time failure that only reproduces on-device.

        How the regression manifests: re-adding ``-Dnvcc`` (e.g. by copying an
        older option list) reintroduces the meson configure abort for the pinned
        lc0 version.
        """
        content = (SCRIPTS_DIR / "build-maia.sh").read_text()
        assert "-Dnvcc" not in content

    def test_script_path_has_no_engines_component(self):
        """The build script must not sit under any directory named ``engines``.

        Why this test exists: deploy-to-pi.sh rsyncs with ``--exclude='engines'``
        (unanchored) to protect the installed engine-binaries dir. That pattern
        matches an ``engines`` path component at any depth, so a build script
        under scripts/engines/ would be silently dropped by deploy while still
        shipping in the .deb -- an install that works one way and not the other.

        How the regression manifests: relocating build-maia.sh back under an
        ``engines/`` subdir reintroduces the exclude collision; this guards that
        no ``engines`` component appears in the script path.
        """
        script_path = SCRIPTS_DIR / "build-maia.sh"
        assert "engines" not in script_path.parts


class TestRecklessBuildCommand:
    """Guards Reckless's Rust toolchain bootstrap and clang-free build recipe.

    Reckless is the project's first Rust engine, and it exposes two on-device-only
    failure modes that no C engine has: the apt toolchain is far too old to
    compile it, and its build steps run in separate shells so a naively-split
    rustup bootstrap loses cargo from PATH. Both only surface on a real board, so
    the catalog command string is the highest deterministic level to guard them.
    """

    def test_bootstraps_rustup_instead_of_using_apt_rust(self):
        """Reckless must build with a rustup-provisioned toolchain, not apt rustc.

        Why this test exists: Reckless is edition 2024 and upstream requires Rust
        >= 1.88, but Debian Bookworm's apt ``rustc``/``cargo`` is 1.63 -- old
        enough that the compile aborts immediately with "feature `edition2024` is
        required". The toolchain therefore cannot come from ``dependencies`` (which
        are apt-installed); the build must bootstrap rustup from sh.rustup.rs.

        How the regression manifests: replacing the rustup bootstrap with reliance
        on an apt rustc (or adding rustc/cargo to dependencies) reintroduces the
        1.63 toolchain and the edition-2024 compile failure on the board.
        """
        script = _build_script("reckless")
        assert "sh.rustup.rs" in script

    def test_does_not_install_apt_rust_toolchain(self):
        """rustc/cargo must not be declared as apt build dependencies.

        Why this test exists: the whole point of the rustup bootstrap is that
        apt's Rust (1.63) cannot build the engine. Declaring ``rustc``/``cargo`` as
        dependencies would install the too-old toolchain and, worse, put an ancient
        cargo on PATH that could shadow the rustup one depending on ordering.

        How the regression manifests: re-adding ``rustc`` or ``cargo`` to
        ``dependencies`` trips this and signals the too-old apt toolchain crept back.
        """
        deps = ENGINES["reckless"].dependencies
        assert "rustc" not in deps
        assert "cargo" not in deps

    def test_pins_rust_toolchain_version(self):
        """The rustup bootstrap must pin an explicit toolchain >= 1.88.

        Why this test exists: an unpinned ``rustup`` install tracks whatever
        ``stable`` happens to be, making the build non-reproducible and able to
        silently regress if a future stable drops edition-2024 support the engine
        relies on. Pinning ``--default-toolchain 1.88.0`` (the upstream minimum)
        makes every install build the same way.

        How the regression manifests: dropping ``--default-toolchain`` reverts to
        floating ``stable``; this asserts the pin (and its minimum version) stay.
        """
        script = _build_script("reckless")
        assert "--default-toolchain" in script
        assert "1.88" in script

    def test_toolchain_bootstrap_and_build_share_one_shell(self):
        """rustup install, cargo-env sourcing, and the build must be one command.

        Why this test exists: each entry in ``build_commands`` runs in its own
        subprocess (see EngineManager._run_monitored_command), so a PATH exported by a
        separate rustup step would not reach a later build step -- the build would
        die with "cargo: command not found". The bootstrap, the
        ``. $HOME/.cargo/env`` that puts cargo on PATH, and the ``cargo rustc``
        build must all live in a single ``&&``-chained command so they share one
        shell.

        How the regression manifests: splitting the bootstrap and the build into
        separate list entries (or omitting the cargo-env source) breaks PATH
        propagation and the on-device build cannot find cargo.
        """
        combined = [
            cmd for cmd in ENGINES["reckless"].build_commands
            if "sh.rustup.rs" in cmd and "cargo rustc" in cmd
        ]
        assert len(combined) == 1, (
            "rustup bootstrap and build must be in exactly one shared shell command"
        )
        assert ".cargo/env" in combined[0]

    def test_builds_without_syzygy_to_avoid_clang(self):
        """Reckless must build with cargo ``--no-default-features``.

        Why this test exists: the default feature set enables ``syzygy``, and
        Reckless's build.rs compiles the Fathom binding by shelling out to
        ``clang`` for it (the binding is gated behind ``#[cfg(feature =
        "syzygy")]``). clang is deliberately not on the device (it pulls the whole
        LLVM toolchain onto a storage-constrained Pi -- the same reason
        Ethereal/Demolito pin CC=gcc and Zahak disables CGO).
        ``--no-default-features`` turns the syzygy feature off so that binding is
        never generated. It only removes endgame-tablebase probing, which needs
        multi-GB external files this project never ships, so nothing usable is lost.

        How the regression manifests: dropping ``--no-default-features`` (or
        building the default feature set via the repo Makefile) compiles the Fathom
        binding, build.rs invokes the absent clang, and the install aborts.
        """
        script = _build_script("reckless")
        assert "--no-default-features" in script

    def test_builds_via_cargo_not_the_repo_makefile(self):
        """Reckless must build by invoking cargo directly, not a Makefile target.

        Why this test exists: the pinned release (v0.9.0) ships a Makefile with no
        ``no-syzygy``/``all`` target -- its default target builds WITH syzygy and
        hardcodes ``RUSTFLAGS := -Ctarget-cpu=native`` -- while master's Makefile
        has different targets. A ``make <target>`` therefore couples the build to
        whatever the checked-out tag happens to name and defeats the RUSTFLAGS
        override; the first on-device install failed with "No rule to make target
        'no-syzygy'". Calling ``cargo rustc`` directly is tag-independent and lets
        the environment's RUSTFLAGS take effect.

        How the regression manifests: reintroducing a ``make`` invocation ties the
        build to a tag-specific target that the pinned ref may not define, and the
        install aborts before compiling.
        """
        script = _build_script("reckless")
        assert "cargo rustc" in script
        assert "make no-syzygy" not in script

    def test_emits_binary_to_repo_root(self):
        """The build must emit the linked binary to the repo root as ``reckless``.

        Why this test exists: a bare ``cargo build`` leaves the executable at
        ``target/release/reckless``, which would not match binary_path=``reckless``
        and would fail the post-build existence check. ``--emit link=reckless``
        writes the final linked output to the repo root, matching binary_path.

        How the regression manifests: dropping ``--emit link=reckless`` leaves the
        binary under target/ and the install fails the "Binary not found" check.
        """
        assert "--emit link=reckless" in _build_script("reckless")

    def test_does_not_depend_on_clang(self):
        """Reckless must not require clang as a build dependency.

        Why this test exists: the ``no-syzygy`` build exists precisely so the Pi
        needs no clang. Declaring clang as a dependency would reintroduce the
        heavyweight LLVM install this project removed from every other engine.

        How the regression manifests: adding "clang" to ``dependencies`` trips this.
        """
        assert "clang" not in ENGINES["reckless"].dependencies

    def test_declares_linker_and_download_dependencies(self):
        """Dependencies must provide a C linker and curl.

        Why this test exists: even without clang, Rust links its final binary
        through the system ``cc``/linker, so ``build-essential`` is required or the
        link fails with "linker `cc` not found". ``curl`` is required twice: to
        bootstrap rustup, and by Reckless's build.rs, which downloads the NNUE
        network with ``curl`` at build time (the net is embedded, so no separate
        file ships).

        How the regression manifests: dropping build-essential breaks the link
        step; dropping curl breaks both the rustup bootstrap and the net download.
        """
        deps = ENGINES["reckless"].dependencies
        assert "build-essential" in deps
        assert "curl" in deps

    def test_binary_path_is_repo_root_reckless(self):
        """Reckless's binary_path must be ``reckless`` at the repo root.

        Why this test exists: the Makefile's ``no-syzygy`` target emits the linked
        binary to the repo root as ``reckless`` (``cargo rustc ... -- --emit
        link=reckless``), NOT to target/release/. Pointing binary_path at a
        target/ path would fail the post-build existence check even though the
        compile succeeded.

        How the regression manifests: setting binary_path to a ``target/`` path
        makes the install fail the "Binary not found" check after a long build.
        """
        assert ENGINES["reckless"].binary_path == "reckless"


# Matches a hard-coded parallelism flag in a build command: -j2 / -j 2 /
# -j$(nproc) / -jN, or a go -p=N. The central knob (_build_env) is the only place
# parallelism should be set, so none of these may appear in the catalog.
_PARALLELISM_FLAG = re.compile(r"(-j\s*(\d+|\$\(nproc\)))|(-p=\d+)")


class TestCentralizedParallelism:
    """Parallelism is one tunable knob (env), not a per-engine constant."""

    def test_no_engine_hardcodes_parallelism_in_its_command(self):
        """No catalog build command may pin -jN/-j$(nproc)/-p=N.

        Why this test exists: per-engine parallelism was the old pattern (-j2 for
        NNUE engines, -j1 for Arasan, -p=1 for Zahak) and it fragmented a decision
        that should be uniform and tunable. Parallelism now comes from MAKEFLAGS/
        GOFLAGS in the build environment, and a command-line -j overrides MAKEFLAGS
        -- so a stray inline flag silently defeats the central knob for that one
        engine.

        How the regression manifests: re-adding any -jN/-p=N to a build_commands
        entry trips this, naming the engine whose flag escaped centralization.
        """
        offenders = {
            name: eng.build_commands
            for name, eng in ENGINES.items()
            if _PARALLELISM_FLAG.search(_build_script(name))
        }
        assert offenders == {}, f"engines still pinning parallelism: {offenders}"

    def test_build_env_sets_makeflags_goflags_and_cargo_jobs(self):
        """_build_env injects the same N as -jN (make), -p=N (go), and cargo jobs.

        Why this test exists: make, go, and cargo each read parallelism from a
        different variable; all three must carry the same N or the build systems
        would diverge. Cargo in particular ignores MAKEFLAGS -- a ``make`` recipe
        that shells out to ``cargo`` (Reckless) would otherwise let cargo default
        to every core, which on a 512MB armhf board OOMs the fat-LTO build. Guards
        that the single knob drives all three build systems.

        How the regression manifests: dropping CARGO_BUILD_JOBS from _build_env
        lets cargo ignore the central knob and spawn one codegen job per core,
        reintroducing the low-RAM OOM the knob exists to prevent.
        """
        env = _build_env(3)
        assert env["MAKEFLAGS"] == "-j3"
        assert env["GOFLAGS"] == "-p=3"
        assert env["CARGO_BUILD_JOBS"] == "3"

    def test_parallelism_env_override_is_honored(self, monkeypatch):
        """UC_BUILD_PARALLELISM overrides the default so a board can be tuned.

        Why this test exists: the optimal job count is board-specific and is meant
        to be measured without a code change. A valid override must win over the
        CPU-count default.
        """
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "2")
        assert _build_parallelism() == 2

    def test_invalid_parallelism_override_falls_back_to_default(self, monkeypatch):
        """A non-positive/garbage override must be ignored, not crash the build.

        Why this test exists: an operator could set a bad value; the build must
        degrade to the computed default rather than passing -j0/-jfoo to make.
        With RAM unreadable (seam returns None) that default is the CPU count, so
        this pins the fallback deterministically on any host.
        How the regression manifests: if parsing did not validate, ``-j0`` (run
        unlimited jobs) or a ValueError would reach the build.
        """
        monkeypatch.setattr(engine_manager_module.os, "cpu_count", lambda: 4)
        monkeypatch.setattr(engine_manager_module, "_mem_total_mb", lambda: None)
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "0")
        assert _build_parallelism() == 4
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "notanint")
        assert _build_parallelism() == 4

    def test_default_parallelism_floored_by_ram_on_small_board(self, monkeypatch):
        """The default job count must be bounded by RAM, not just cores.

        Why this test exists: defaulting to the raw CPU count meant a ~415MB /
        4-core Pi Zero 2 W ran ``-j4`` compiles that overshoot RAM and thrash swap
        for tens of minutes (build-memory swap lets them *complete* but not run
        efficiently). Flooring by RAM/_BUILD_JOB_MB keeps the build CPU/IO-bound.

        How the regression manifests: dropping the RAM bound returns 4 here (the
        core count) instead of 1, reintroducing the swap thrash on small boards.
        """
        monkeypatch.delenv(_BUILD_PARALLELISM_ENV, raising=False)
        monkeypatch.setattr(engine_manager_module.os, "cpu_count", lambda: 4)
        monkeypatch.setattr(engine_manager_module, "_mem_total_mb", lambda: 415)
        assert _build_parallelism() == 1

    def test_default_parallelism_keeps_cores_when_ram_ample(self, monkeypatch):
        """Ample-RAM boards keep full core parallelism.

        Why this test exists: the RAM bound must not throttle capable hardware --
        an 8GB / 4-core board should still build at -j4.

        How the regression manifests: a too-aggressive clamp (e.g. dividing by a
        much larger per-job budget) would return <4, needlessly slowing big boards.
        """
        monkeypatch.delenv(_BUILD_PARALLELISM_ENV, raising=False)
        monkeypatch.setattr(engine_manager_module.os, "cpu_count", lambda: 4)
        monkeypatch.setattr(engine_manager_module, "_mem_total_mb", lambda: 8192)
        assert _build_parallelism() == 4

    def test_mem_total_mb_returns_none_off_linux(self, monkeypatch):
        """_mem_total_mb must return None when /proc/meminfo is absent.

        Why this test exists: the None sentinel is what makes the RAM bound skip
        on non-Linux dev/CI (falling back to the CPU count). If it instead raised
        or returned 0, the default parallelism path would crash or clamp to 1 on
        the dev box. Simulated by pointing the open at a missing file.
        """
        def _boom(*_a, **_k):
            raise FileNotFoundError("/proc/meminfo")

        monkeypatch.setattr("builtins.open", _boom)
        assert _mem_total_mb() is None
