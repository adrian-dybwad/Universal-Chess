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


def test_koivisto_catalog_entry_unsupported_on_all_arm():
    """The real Koivisto catalog entry builds on no ARM arch and ships no prebuilt.

    Why: Koivisto's NNUE is x86-SIMD only with a broken upstream ARM NEON path, so
    it fails to compile on both armv7l and aarch64. Pinning supported_archs=empty
    and has_prebuilt=False catches a revert that would re-enable a doomed install
    or advertise a prebuilt that the CI archive never contains.
    """
    from universalchess.managers.engine_manager import ENGINES

    assert ENGINES["koivisto"].supported_archs == frozenset()
    assert ENGINES["koivisto"].has_prebuilt is False
    assert arch_unsupported_reason(ENGINES["koivisto"], "arm64") is not None
    assert arch_unsupported_reason(ENGINES["koivisto"], "armhf") is not None


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
