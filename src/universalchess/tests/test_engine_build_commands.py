"""Regression tests for engine source-build command definitions.

These pin platform-specific build flags in the ``ENGINES`` catalog that cannot
be exercised in CI (they only fail when compiling on the target board), so the
build-command string itself is the highest deterministic level at which the
regression can be guarded.
"""

from universalchess.managers.engine_manager import ENGINES


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

    def test_serializes_compilation(self):
        """The build must pass ``-p=1`` to serialize Go compilation.

        Why this test exists: a 415MB Pi Zero 2 W OOM-killed the build
        ("compile: signal: killed"). Go defaults build parallelism to the CPU
        count (4), and the engine package embeds the ~1.5MB NNUE net as generated
        source, so several concurrent compiles of it exceed the board's RAM.
        Empirically even -p=2 still OOMs; only -p=1 (one compile at a time) fits.
        It is passed via GOFLAGS so it lands on the go build/go run steps but not
        the unprefixed ``go clean`` (which does not accept -p).

        How the regression manifests: dropping ``-p=1`` reverts to CPU-count
        parallelism and the build is OOM-killed on low-RAM boards.
        """
        script = _build_script("zahak")
        assert "GOFLAGS=-p=1" in script

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
