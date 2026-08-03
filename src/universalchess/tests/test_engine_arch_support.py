"""Tests for engine architecture support and prebuilt-release resolution.

Background / why these tests exist
----------------------------------
Some engines cannot build on every CPU the project runs on. Berserk, in
particular, is 64-bit-only: it uses ``__int128`` (absent on 32-bit targets) and
its NEON path uses AArch64-only intrinsics, so a source build on 32-bit ARM
(armhf) fails with a confusing compiler error. The catalog therefore declares
``supported_archs`` and the installer must refuse unsupported architectures up
front with a clear message instead of falling through to a doomed build.

Separately, prebuilt binaries are published as GitHub release assets. The client
previously queried ``/releases/latest``, which ignores prereleases and 404s when
only nightly prereleases exist (the repo's current state), silently disabling the
prebuilt fast path. The resolver now scans the releases list newest-first.

These tests pin both behaviors at the unit level (pure helpers + the installer's
fail-fast branch) so they hold without network access or a real build.
"""

import pytest

from universalchess.managers.engine_manager import (
    EngineDefinition,
    EngineManager,
    arch_unsupported_reason,
    engine_supports_arch,
    find_prebuilt_archive_url,
    get_current_arch,
)


def _engine(supported_archs):
    """Build a minimal EngineDefinition carrying only the arch restriction.

    The other fields are irrelevant to arch logic; they are filled with inert
    values so the dataclass constructs.
    """
    return EngineDefinition(
        name="x",
        display_name="X Engine",
        summary="",
        description="",
        repo_url=None,
        build_commands=[],
        binary_path="",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=[],
        supported_archs=supported_archs,
    )


# A board reporting the armhf token whose CPU has no NEON unit: the Pi Zero W.
_NEON_LESS_ARMHF = {"arch": "armhf", "has_neon": False}


# ---------------------------------------------------------------------------
# Arch support predicate + reason
# ---------------------------------------------------------------------------

def test_unrestricted_engine_supported_on_any_arch():
    """supported_archs=None means no restriction, so every arch is supported.

    Why: the vast majority of engines build everywhere; None must not be treated
    as "supports nothing". Regression: an inverted check would make every engine
    unsupported and block all installs.
    """
    engine = _engine(None)
    assert engine_supports_arch(engine, "armhf") is True
    assert engine_supports_arch(engine, "arm64") is True
    assert arch_unsupported_reason(engine, "armhf") is None


def test_restricted_engine_unsupported_off_list_with_reason():
    """A 64-bit-only engine is unsupported on armhf and the reason names both archs.

    Why: the reason string is shown to the user and used as the install failure
    message; it must state the current arch and the supported one. Regression: a
    missing/!-formatted reason would surface a blank or misleading notice.
    """
    engine = _engine(frozenset({"arm64"}))
    assert engine_supports_arch(engine, "armhf") is False
    reason = arch_unsupported_reason(engine, "armhf")
    assert reason is not None
    assert "armhf" in reason
    assert "arm64" in reason
    assert engine.display_name in reason


def test_restricted_engine_supported_on_listed_arch():
    """The same engine is supported on a listed arch (no reason).

    Why: the gate must allow the arches it lists, or it would block legitimate
    installs on supported hardware. Regression: an off-by-one in membership would
    return a spurious reason here.
    """
    engine = _engine(frozenset({"arm64"}))
    assert engine_supports_arch(engine, "arm64") is True
    assert arch_unsupported_reason(engine, "arm64") is None


def test_berserk_catalog_entry_is_arm64_only():
    """The real Berserk catalog entry declares arm64-only support.

    Why: this is the concrete classification the feature exists for. Pinning it
    catches an accidental revert that would re-offer Berserk on 32-bit ARM.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["berserk"].supported_archs == frozenset({"arm64"})


def test_weiss_catalog_entry_is_arm64_only():
    """The real Weiss catalog entry declares arm64-only support.

    Why: Weiss's TTIndex uses ``(unsigned __int128)key * count >> 64`` (Lemire
    reduction), and __int128 does not exist on 32-bit ARM, so the build fails in
    transposition.h on armhf. The catalog must gate it to arm64 so the install
    button is disabled on 32-bit rather than offering a doomed build. Regression:
    re-offering Weiss on armhf reintroduces the transposition.h compile failure.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["weiss"].supported_archs == frozenset({"arm64"})


def test_arasan_catalog_entry_is_arm64_only():
    """The real Arasan catalog entry declares arm64-only support.

    Why: Arasan's NNUE SparseLinear layer is gated by
    ``static_assert(0, "requires SIMD")`` (so a non-SIMD build does not compile),
    and the Makefile defines a SIMD path only for the arm64/aarch64 arch tokens
    (NEON). 32-bit ARM (armhf) therefore has no buildable configuration. The
    catalog must gate it to arm64 so the install button is disabled on 32-bit
    rather than offering a doomed build. Regression: re-offering Arasan on armhf
    reintroduces the static_assert / missing-SIMD compile failure on the board.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["arasan"].supported_archs == frozenset({"arm64"})


@pytest.mark.parametrize("arch", ["armhf", "arm64"])
def test_empty_supported_archs_unsupported_everywhere(arch):
    """supported_archs=frozenset() means unsupported on every architecture.

    Why: an engine with no working ARM build (e.g. Koivisto, x86-SIMD only) must
    be refused on both armhf and arm64. Regression: treating an empty set like
    None ("unrestricted") would re-offer an unbuildable engine on all hardware.
    """
    engine = _engine(frozenset())
    assert engine_supports_arch(engine, arch) is False
    assert arch_unsupported_reason(engine, arch) is not None


def test_empty_supported_archs_reason_is_x86_only_not_blank_list():
    """The empty-set reason names the engine and says "ARM"/"x86", never "Supported: .".

    Why: the generic message joins supported_archs into "Supported: <list>"; an
    empty set would render the broken "Supported: ." fragment. This guards the
    dedicated x86-only wording. Regression: falling through to the generic branch
    produces a trailing-empty list the user cannot act on.
    """
    engine = _engine(frozenset())
    reason = arch_unsupported_reason(engine, "armhf")
    assert reason is not None
    assert engine.display_name in reason
    assert "ARM" in reason and "x86" in reason
    # The generic "Supported: <arches>." wording must not leak an empty list.
    assert "Supported: ." not in reason


def test_koivisto_catalog_entry_supports_both_arm_archs():
    """The real Koivisto catalog entry builds on both arm64 and 32-bit ARM (armhf).

    Why: Koivisto's upstream NEON path used three AArch64-only intrinsics
    (``vmull_high_s16`` + ``vpaddq_s32`` in the ``avx_madd_epi16`` macro and
    ``vaddvq_s32`` for the horizontal sum) plus broken load/store placeholders
    (``vldrq_p128`` / ``exit(-1)``). The build rewrites all of them to intrinsics
    available on ARMv7 NEON as well (see the build-command test below), after which
    both the aarch64 and armv7 builds compile and produce a bit-identical bench
    (3661572 nodes) -- validated on an arm64 host and on a real armv7l board
    (dgt-32). The NNUE math is integer, so results are platform-independent.

    Regression: dropping either arch re-hides a working engine; if the armv7 build
    regressed (e.g. an AArch64-only intrinsic crept back), armhf would fail to
    compile again, which this pairs with the build-command test to catch early.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["koivisto"].supported_archs == frozenset({"arm64", "armhf"})
    assert arch_unsupported_reason(ENGINES["koivisto"], "arm64") is None
    assert arch_unsupported_reason(ENGINES["koivisto"], "armhf") is None


def test_koivisto_build_patches_all_aarch64_only_neon_before_compiling():
    """The Koivisto build rewrites every AArch64-only NEON construct before ``make``.

    Why: upstream's NEON path does not build on 32-bit ARM (armhf) for three
    reasons, all patched here so a single portable NEON path serves both arm64 and
    armv7:
      - ``avx_load_reg vldrq_p128`` (wrong type) / ``avx_store_reg exit(-1)`` (a
        stub) -> ``vld1q_s16`` / ``vst1q_s16``.
      - ``avx_madd_epi16`` uses ``vmull_high_s16`` + ``vpaddq_s32`` (AArch64-only)
        -> ``vmull_s16(vget_high_s16(...))`` + ``vpadd_s32`` / ``vcombine_s32``.
      - the horizontal sum uses ``vaddvq_s32`` (AArch64-only) -> ``vadd_s32`` /
        ``vpadd_s32`` / ``vget_lane_s32`` in nn/eval.cpp.
    All replacements are bit-identical (validated: bench 3661572 on x86, arm64 and
    armv7l).

    A second, independent failure on armv7 is the makefile's PGO step: the default
    ``openbench`` goal builds with ``-fprofile-generate``, which under ``-pthread``
    selects atomic value-profiler gcov symbols that Raspbian's armv7 libgcov does
    not provide, so the link fails with ``undefined reference to
    __gcov_*_profiler_atomic``. ``-fprofile-update=single`` selects the non-atomic
    counters and links on both arches, so the build overrides ``PGO_PRE_FLAGS``.

    How a regression manifests: dropping any patch makes ``make`` compile the
    unmodified upstream construct and the armv7 (and, for the AArch64-only
    intrinsics that also mis-store, the arm64) build fails -- with no early signal
    until a real device/CI build. Asserting each replacement is present and ordered
    before the compile catches that revert at unit-test time.
    """
    from universalchess.managers.engine_manager import ENGINES

    build = "\n".join(ENGINES["koivisto"].build_commands)
    # Load/store macros.
    assert "nn/defs.h" in build
    assert "vld1q_s16" in build   # replacement for avx_load_reg vldrq_p128
    assert "vst1q_s16" in build   # replacement for avx_store_reg exit(-1)
    # avx_madd_epi16: portable replacement for vmull_high_s16 + vpaddq_s32.
    assert "vpadd_s32" in build
    assert "vcombine_s32" in build
    # Horizontal sum in eval.cpp: portable replacement for vaddvq_s32.
    assert "nn/eval.cpp" in build
    assert "vget_lane_s32" in build
    # PGO fix so the armv7 profile-generate link finds non-atomic gcov counters.
    assert "-fprofile-update=single" in build
    # Every patch must run before the compile; otherwise make hits the broken source.
    assert build.index("vld1q_s16") < build.index("make")
    assert build.index("vpadd_s32") < build.index("make")
    assert build.index("vget_lane_s32") < build.index("make")


# ---------------------------------------------------------------------------
# NEON capability, which the arch token cannot express
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("machine,features,expected", [
    # The Pi Zero W: ARM1176JZF-S has a VFPv2 FPU and no NEON unit at all.
    ("armv6l", "half thumb fastmult vfp edsp java tls", False),
    # The Pi 2/3 and Zero 2 W in 32-bit mode: same 'armhf' token, but NEON present.
    ("armv7l", "half thumb fastmult vfp edsp neon vfpv3 tls", True),
    # AArch64 spells it 'asimd', and every ARMv8-A part the project runs on has it.
    ("aarch64", "fp asimd evtstrm crc32 cpuid", True),
    # Not an ARM host: the engine's x86 SIMD path applies, so NEON is irrelevant.
    ("x86_64", "", True),
])
def test_neon_detection_distinguishes_boards_sharing_the_armhf_token(
    machine, features, expected
):
    """NEON presence is read from the CPU, not inferred from the arch token.

    Why this test exists: 'armhf' is a Debian port name covering every 32-bit
    hard-float ARM, so a Pi Zero W (ARMv6, no NEON) and a Pi 2 (ARMv7, NEON) carry
    the same token. Koivisto was gated on that token, was therefore offered on the
    Zero W, and failed after cloning and starting a compile with ``#include
    <immintrin.h> ... compilation terminated`` -- its source falls back to the x86
    header when ``__ARM_NEON`` is undefined.

    How a regression manifests: inferring from the token returns True for armv6l
    and the unbuildable engine is offered again.
    """
    from universalchess.managers.engine_manager import cpu_has_neon

    assert cpu_has_neon(machine, features) is expected


def test_an_engine_needing_neon_is_refused_on_an_arm_cpu_without_it():
    """The gate blocks a NEON-dependent engine on a NEON-less ARM board.

    Why this test exists: this is the reported failure. The engine's declared
    architecture matches the host exactly, so nothing but the CPU capability can
    tell the two apart.

    How a regression manifests: dropping the capability check returns None here
    and the install proceeds to a compile that cannot succeed.
    """
    engine = _engine(frozenset({"arm64", "armhf"}))
    engine.requires_neon = True

    reason = arch_unsupported_reason(engine, "armhf", has_neon=False)

    assert reason is not None
    assert engine.display_name in reason
    # Naming the architecture alone would be wrong and confusing: other armhf
    # boards run this engine. The missing hardware is what the user needs told.
    assert "NEON" in reason


def test_an_engine_needing_neon_is_still_offered_where_neon_exists():
    """A Pi 2/3/Zero 2 W keeps the engine it can actually build.

    Why this test exists: the narrow fix must stay narrow. Koivisto's 32-bit
    support was validated on a real armv7l board, and blocking all of armhf to
    solve the Zero W would remove a working engine from every other 32-bit board.

    How a regression manifests: gating on the arch token instead of the capability
    returns a reason here and silently drops support for hardware that works.
    """
    engine = _engine(frozenset({"arm64", "armhf"}))
    engine.requires_neon = True

    assert arch_unsupported_reason(engine, "armhf", has_neon=True) is None


def test_engines_that_do_not_need_neon_are_unaffected_by_its_absence():
    """The capability check applies only to engines that declare the requirement.

    Why this test exists: most of the catalog compiles fine without SIMD, and the
    Zero W is a supported board. A check applied to every engine would empty the
    engine list on exactly the hardware this project targets.

    How a regression manifests: testing the capability unconditionally blocks
    Rodent IV, CT800 and the rest on the Zero W.
    """
    engine = _engine(None)

    assert arch_unsupported_reason(engine, "armhf", has_neon=False) is None


def test_koivisto_declares_the_neon_requirement_its_source_imposes():
    """The real Koivisto entry is marked as needing NEON, and still lists armhf.

    Why this test exists: Koivisto's nn/defs.h selects ``<arm_neon.h>`` only when
    ``__ARM_NEON`` is defined and otherwise includes ``<immintrin.h>``; its NNUE
    has AVX512, AVX2/AVX, SSE2 and NEON paths and no scalar fallback, so a
    NEON-less ARM board has no buildable configuration. Both halves are asserted
    together because dropping either one reintroduces a field failure: without the
    flag the Zero W is offered a doomed build, and without 'armhf' every 32-bit
    board loses a working engine.

    How a regression manifests: removing the flag re-offers Koivisto on the Zero W
    and it fails with the immintrin.h error again.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["koivisto"].requires_neon is True
    assert ENGINES["koivisto"].supported_archs == frozenset({"arm64", "armhf"})


def test_install_refuses_a_neon_engine_on_a_neon_less_board_without_building(
    monkeypatch, tmp_path
):
    """install_engine consults the real CPU, not just the architecture token.

    Why this test exists: the pure gate can be correct while the installer never
    passes it the capability, which is precisely how this bug reached a board --
    the gate existed and the arch matched. This drives the real entry point and
    fails if any build path is reached.

    How a regression manifests: an installer that omits the capability clones the
    repo and starts a compile, raising the sentinel here instead of refusing.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.get_current_arch",
        lambda: "armhf",
    )
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.host_has_neon",
        lambda: False,
    )

    def _should_not_run(*args, **kwargs):
        raise AssertionError("build path must not run without the required SIMD")

    manager = EngineManager(engines_dir=str(tmp_path))
    monkeypatch.setattr(manager, "_try_install_prebuilt", _should_not_run)
    monkeypatch.setattr(manager, "_install_from_source", _should_not_run)

    result = manager.install_engine("koivisto")

    assert result is False
    assert manager._install_error is not None
    assert "NEON" in manager._install_error
    assert manager._installing_engine is None


# ---------------------------------------------------------------------------
# Architecture detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("machine,expected", [
    ("aarch64", "arm64"),
    ("arm64", "arm64"),
    ("armv7l", "armhf"),
    ("armv6l", "armhf"),
    ("arm", "armhf"),
])
def test_get_current_arch_maps_machine(monkeypatch, machine, expected):
    """platform.machine values map to the 'arm64'/'armhf' tokens the catalog uses.

    Why: prebuilt selection, the install gate, and the catalog API all key off
    this single mapping; a wrong token (e.g. armv7l -> arm64) would let an
    unbuildable engine through. Regression: a missing branch falls to the width
    heuristic and would misclassify these explicit values.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.platform.machine",
        lambda: machine,
    )
    assert get_current_arch() == expected


# ---------------------------------------------------------------------------
# Installer fail-fast on unsupported architecture
# ---------------------------------------------------------------------------

def test_install_engine_rejects_unsupported_arch_without_building(monkeypatch, tmp_path):
    """install_engine('berserk') on armhf returns False and never starts a build.

    Why: the whole point of the gate is to replace the cryptic downstream build
    error with an honest refusal. This forces arch to 'armhf' and fails the test
    if either build path is reached.

    How a regression manifests: if the gate is removed, _try_install_prebuilt or
    _install_from_source runs (raising the sentinel here) instead of an early
    False with a populated _install_error.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.get_current_arch",
        lambda: "armhf",
    )

    def _should_not_run(*args, **kwargs):
        raise AssertionError("build path must not run for an unsupported arch")

    manager = EngineManager(engines_dir=str(tmp_path))
    monkeypatch.setattr(manager, "_try_install_prebuilt", _should_not_run)
    monkeypatch.setattr(manager, "_install_from_source", _should_not_run)

    result = manager.install_engine("berserk")

    assert result is False
    assert manager._install_error is not None
    assert "armhf" in manager._install_error
    # The "currently installing" flag must be cleared on the early return so the
    # UI/queue is not left thinking an install is in progress.
    assert manager._installing_engine is None


# ---------------------------------------------------------------------------
# Prebuilt release-asset resolution
# ---------------------------------------------------------------------------

def test_find_prebuilt_archive_url_picks_newest_release_with_asset():
    """Returns the asset URL from the first (newest) release that carries it.

    Why: releases come newest-first; the resolver must prefer the most recent
    build of the archive. Regression: scanning in the wrong order (or stopping at
    the first release regardless of assets) returns a stale or missing URL.
    """
    releases = [
        {"tag_name": "newest", "assets": [
            {"name": "other.tar.gz", "browser_download_url": "u-other"},
        ]},
        {"tag_name": "has-it", "assets": [
            {"name": "engines-arm64.tar.gz", "browser_download_url": "u-arm64-new"},
        ]},
        {"tag_name": "older", "assets": [
            {"name": "engines-arm64.tar.gz", "browser_download_url": "u-arm64-old"},
        ]},
    ]
    url = find_prebuilt_archive_url(releases, "engines-arm64.tar.gz")
    # First release lacks the asset; the next one that has it wins (not the oldest).
    assert url == "u-arm64-new"


def test_find_prebuilt_archive_url_returns_none_when_absent():
    """Returns None when no release carries the requested archive.

    Why: a missing archive must make the caller fall back to a source build, not
    crash or return a bogus URL. Regression: returning a truthy value here would
    drive a download of the wrong/nonexistent asset.
    """
    releases = [
        {"tag_name": "nightly", "assets": [
            {"name": "universal-chess.deb", "browser_download_url": "u-deb"},
        ]},
    ]
    assert find_prebuilt_archive_url(releases, "engines-armhf.tar.gz") is None
