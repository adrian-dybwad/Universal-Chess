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


class TestArasanBuildCommand:
    """Guards Arasan's output name/location and arm64 NEON selection."""

    def test_binary_path_points_to_bin_not_src(self):
        """Arasan's binary_path must be ``bin/arasan``, never ``src/arasan``.

        Why this test exists: Arasan's Makefile writes the executable to
        ../bin/arasanx-<bits> (EXPORT=../bin), not to src/. The catalog previously
        declared binary_path=src/arasan, so every install compiled fine and then
        failed the post-build existence check with "Binary not found: src/arasan"
        -- and the CI ``cp arasan`` (from src/) failed too, so no prebuilt ever
        shipped. The fix pairs EXE=arasan (fixed name) with binary_path=bin/arasan.

        How the regression manifests: reverting binary_path to src/arasan (or any
        src/ path) trips this and reintroduces the silent install failure.
        """
        engine = ENGINES["arasan"]
        assert engine.binary_path == "bin/arasan"
        assert not engine.binary_path.startswith("src/")

    def test_fixes_exe_name(self):
        """The build must pass ``EXE=arasan`` so the output name is deterministic.

        Why this test exists: without EXE the Makefile emits arasanx-$(LONG_BIT)
        (arasanx-64 / arasanx-32), which neither binary_path nor the CI cp can
        predict. EXE=arasan pins ../bin/arasan on both arches.

        How the regression manifests: dropping EXE=arasan reverts to the
        arch-dependent arasanx-<bits> name and the binary is not found post-build.
        """
        assert "EXE=arasan" in _build_script("arasan")

    def test_requests_neon_only_on_64bit_arm(self):
        """NEON must be gated on the arm64/aarch64 uname, not applied blindly.

        Why this test exists: Arasan's Makefile defines NEON flags only for the
        arm64/aarch64 arch tokens (no armv7l branch) and enables them only with
        BUILD_TYPE=neon. The vectorized NNUE path should be requested on 64-bit ARM
        for speed, while 32-bit ARM must stay on the scalar fallback. The command
        therefore adds BUILD_TYPE=neon under a uname case for aarch64/arm64.

        How the regression manifests: removing the guard (applying BUILD_TYPE=neon
        unconditionally) would request a non-existent NEON build on armhf; dropping
        it entirely would leave arm64 on the slower scalar NNUE path.
        """
        script = _build_script("arasan")
        assert "BUILD_TYPE=neon" in script
        assert "uname -m" in script
        assert "aarch64" in script and "arm64" in script
