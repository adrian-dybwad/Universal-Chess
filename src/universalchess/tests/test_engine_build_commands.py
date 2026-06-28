"""Regression tests for engine source-build command definitions.

These pin platform-specific build flags in the ``ENGINES`` catalog that cannot
be exercised in CI (they only fail when compiling on the target board), so the
build-command string itself is the highest deterministic level at which the
regression can be guarded.
"""

import re
from pathlib import Path

import universalchess.managers.engine_manager as engine_manager_module
from universalchess.managers.engine_manager import (
    _BUILD_PARALLELISM_ENV,
    ENGINES,
    SCRIPTS_DIR,
    _build_env,
    _build_parallelism,
)


def _build_script(engine_name: str) -> str:
    """Join an engine's build_commands into the single shell script that runs."""
    return "\n".join(ENGINES[engine_name].build_commands)


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


class TestMaiaBuildCommand:
    """Guards Maia's build-script path against the dev-vs-installed layout bug.

    Maia is the one engine that shells out to a bundled build script instead of
    cloning and compiling inline, so its command embeds a filesystem path. That
    path must resolve on an *installed board*, not just in a dev checkout.
    """

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
        assert "/tmp/maia-build-swap" not in content
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
        # The resume paths must exist: reuse an existing checkout and meson config.
        assert "Reusing existing lc0 checkout" in content
        assert "Reusing existing meson configuration" in content

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

    def test_build_env_sets_makeflags_and_goflags(self):
        """_build_env injects -jN for make and -p=N for go from one value.

        Why this test exists: make and go read parallelism from different
        variables; both must carry the same N or the two build systems would
        diverge. Guards that a single knob drives both.
        """
        env = _build_env(3)
        assert env["MAKEFLAGS"] == "-j3"
        assert env["GOFLAGS"] == "-p=3"

    def test_parallelism_env_override_is_honored(self, monkeypatch):
        """UC_BUILD_PARALLELISM overrides the default so a board can be tuned.

        Why this test exists: the optimal job count is board-specific and is meant
        to be measured without a code change. A valid override must win over the
        CPU-count default.
        """
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "2")
        assert _build_parallelism() == 2

    def test_invalid_parallelism_override_falls_back_to_cpu_count(self, monkeypatch):
        """A non-positive/garbage override must be ignored, not crash the build.

        Why this test exists: an operator could set a bad value; the build must
        degrade to the CPU-count default rather than passing -j0/-jfoo to make.
        How the regression manifests: if parsing did not validate, ``-j0`` (run
        unlimited jobs) or a ValueError would reach the build.
        """
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "0")
        assert _build_parallelism() == (__import__("os").cpu_count() or 1)
        monkeypatch.setenv(_BUILD_PARALLELISM_ENV, "notanint")
        assert _build_parallelism() == (__import__("os").cpu_count() or 1)
