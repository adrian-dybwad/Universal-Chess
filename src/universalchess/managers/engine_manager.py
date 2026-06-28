"""Engine Manager - Install and manage UCI chess engines.

Provides functionality to:
- List available engines with installation status
- Install engines from source (compile on device)
- Uninstall engines
- Check if engines are installed

Supported engines (14 total):

Top Tier (~3300+ ELO):
- stockfish: World's strongest, installed from system package
- berserk: Top-3 ranked, NNUE-based
- koivisto: Top-10, fast and aggressive
- ethereal: Top-15, clean codebase

Strong Tier (~2900-3200 ELO):
- fire: Optimized for modern CPUs
- laser: Fast tactical search
- demolito: Simple and efficient
- weiss: Clean, educational
- arasan: Veteran engine since 1994
- smallbrain: Compact NNUE engine

Specialty Engines:
- rodentIV: 50+ playing personalities
- ct800: Classic chess computer style
- maia: Human-like play (makes mistakes)
- zahak: Go-based, fast development
"""

import os
import re
import signal
import subprocess
import shutil
import threading
import platform
import tarfile
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict, FrozenSet, Tuple
from pathlib import Path
from queue import Queue, Empty
from enum import Enum
import time

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.services.engine_install_state import InstallStage
from universalchess.services.engine_install_record import (
    DEFAULT_REF,
    STORE as INSTALL_RECORD_STORE,
    EngineInstallRecordStore,
)
from universalchess.services.github_tag_cache import (
    STORE as TAG_CACHE_STORE,
    GitHubTagCacheStore,
)
from universalchess.services import apt_recovery
from universalchess.services.apt_recovery import RecoveryOutcome
from universalchess.services.build_memory import build_memory
from universalchess.services.event_log import log_event

# Engine installation directory
ENGINES_DIR = "/opt/universalchess/engines"
BUILD_TMP = "/opt/universalchess/tmp/engine_build"

# Trailing build-output lines retained for the failure message (stdout+stderr are
# merged, so the real error is in the tail regardless of which stream it used).
_BUILD_TAIL_LINES = 40
# Minimum seconds between build-progress message updates. Throttled because each
# update persists install state to the SD-card-backed store and build output can
# be chatty on a slow board.
_BUILD_PROGRESS_THROTTLE_SECONDS = 2.0

# Environment variable that overrides compile parallelism (see _build_parallelism).
_BUILD_PARALLELISM_ENV = "UC_BUILD_PARALLELISM"


def _build_parallelism() -> int:
    """Number of parallel compile jobs for source builds.

    Parallelism is centralized here instead of being hard-coded per engine: the
    value is injected into every build command's environment (see _build_env), so
    each catalog entry no longer carries its own ``-jN``. This is the single knob
    for trading build speed against memory pressure. It is overridable via the
    :data:`_BUILD_PARALLELISM_ENV` environment variable so the optimum can be
    measured on a board without a code change; the default is the CPU count.

    On a RAM-constrained board a high job count is only safe because the
    temporary build-memory swap (see :mod:`services.build_memory`) is acquired
    around the build -- without it, parallel compiles of a memory-heavy engine
    (e.g. an embedded NNUE net) are OOM-killed. Swap lets the build *complete*;
    the fastest job count is still bounded by what fits in RAM, which is why the
    value is tunable rather than fixed at "unlimited".
    """
    override = os.environ.get(_BUILD_PARALLELISM_ENV)
    if override:
        try:
            n = int(override)
        except ValueError:
            log.warning("Ignoring invalid %s=%r", _BUILD_PARALLELISM_ENV, override)
        else:
            if n >= 1:
                return n
            log.warning("Ignoring %s=%r (must be >= 1)", _BUILD_PARALLELISM_ENV, override)
    return os.cpu_count() or 1


def _build_env(parallelism: Optional[int] = None) -> dict:
    """Environment for a build subprocess with centralized compile parallelism.

    Sets ``MAKEFLAGS=-jN`` (make reads -j from the environment) and
    ``GOFLAGS=-p=N`` (go applies -p=N to its compile concurrency) so make- and
    go-based builds share one parallelism value. Because an explicit ``-j`` on a
    command line overrides ``MAKEFLAGS``, the per-engine ``-jN`` flags are removed
    from the catalog -- the env is now the single source of truth.
    """
    n = parallelism if parallelism is not None else _build_parallelism()
    env = dict(os.environ)
    env["MAKEFLAGS"] = f"-j{n}"
    env["GOFLAGS"] = f"-p={n}"
    return env

# Directory of the installed universalchess package, and its bundled scripts.
# engine_manager.py lives at <pkg>/managers/engine_manager.py, so two parents
# reach <pkg>: /opt/universalchess on a board (the .deb and deploy-to-pi.sh both
# flatten src/universalchess into it) and src/universalchess in a dev checkout.
# Runtime build helpers ship under <pkg>/scripts/, so referencing them this way
# resolves in both layouts. A repo-root path (four parents) does NOT exist on a
# board -- it overshoots to "/", which is what produced the
# "sudo //scripts/engines/build-maia.sh: command not found" install failure.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

# GitHub release URL for pre-built engine binaries.
GITHUB_REPO = "adrian-dybwad/Universal-Chess"
# List endpoint (newest-first), NOT /releases/latest: the latter ignores
# prereleases and 404s when only nightly prereleases exist, which is the current
# state of this repo. Scanning the list lets prebuilt binaries attached to ANY
# release (nightly prerelease or full) be found.
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
PREBUILT_ARCHIVE_NAME_TEMPLATE = "engines-{arch}.tar.gz"  # arm64 or armhf


def get_current_arch() -> str:
    """Architecture token for the running host: 'arm64' or 'armhf'.

    Centralizes the ``platform.machine`` mapping so prebuilt selection, the
    install-time support check, and the catalog API all classify the device the
    same way. 64-bit ARM -> 'arm64'; 32-bit ARM -> 'armhf'.
    """
    machine = platform.machine().lower()
    if machine in ('aarch64', 'arm64'):
        return 'arm64'
    elif machine in ('armv7l', 'armv6l', 'arm'):
        return 'armhf'
    else:
        # Fallback - infer width from the machine string.
        return 'arm64' if '64' in machine else 'armhf'


def find_prebuilt_archive_url(releases: list, archive_name: str) -> Optional[str]:
    """Return the download URL for ``archive_name`` from the newest release that has it.

    Takes the parsed GitHub ``/releases`` list (already newest-first) so the
    selection logic is pure and unit-testable without network access. Returns the
    asset's ``browser_download_url`` from the first (newest) release whose assets
    include an exact name match, or None if no release carries the archive.

    Args:
        releases: Parsed JSON list from the GitHub releases endpoint.
        archive_name: Exact asset name to match (e.g. 'engines-arm64.tar.gz').
    """
    for release in releases:
        for asset in release.get("assets", []):
            if asset.get("name") == archive_name:
                return asset.get("browser_download_url")
    return None


@dataclass
class EngineDefinition:
    """Definition of a chess engine that can be installed."""
    name: str                    # Engine name (used as executable name)
    display_name: str            # Human-readable name for UI
    summary: str                 # Short summary for list display (~20 chars)
    description: str             # Full description for detail view
    repo_url: Optional[str]      # Git repository URL (None for system package or bundled)
    build_commands: List[str]    # Commands to build after cloning
    binary_path: str             # Path to binary after build (relative to repo)
    is_system_package: bool      # True if installed via apt
    package_name: Optional[str]  # apt package name (if system package)
    extra_files: List[str]       # Additional files/dirs to copy (relative to repo)
    dependencies: List[str]      # apt packages needed to build
    can_uninstall: bool = True   # Whether engine can be uninstalled
    clone_with_submodules: bool = False  # Use --recurse-submodules when cloning
    # Git tag/branch/commit to build. None tracks the repo's default branch (master).
    # Pin a specific release tag when the default branch is a moving target whose
    # build can regress between installs -- e.g. Arasan is pinned to a tagged release
    # because its master NEON path has regressed, so an unpinned clone would
    # intermittently fail to compile depending on when the user happens to install.
    git_ref: Optional[str] = None
    build_timeout: int = 600     # Timeout for build commands in seconds (default 10 min)
    estimated_install_minutes: int = 5  # Estimated install time in minutes for UI
    has_prebuilt: bool = False   # True if pre-built binary available from releases
    # Architectures (as returned by `_get_arch`: 'arm64' / 'armhf') this engine can
    # be installed on. None means "no architecture restriction" (the common case).
    # A non-empty set restricts to those arches -- e.g. Berserk requires `__int128`
    # and AArch64-only NEON intrinsics, so it is 64-bit-only ({'arm64'}) and listing
    # it on 32-bit ARM produces a confusing build failure. An empty set means the
    # engine builds on no architecture this project targets (e.g. Koivisto is
    # x86-SIMD only with a broken upstream ARM NEON path).
    supported_archs: Optional[FrozenSet[str]] = None


def engine_supports_arch(engine: "EngineDefinition", arch: str) -> bool:
    """Whether ``engine`` can be installed on the given architecture.

    ``supported_archs is None`` means unrestricted (supported everywhere).

    Args:
        engine: The engine definition to check.
        arch: Architecture token as produced by ``EngineManager._get_arch``
            ('arm64' or 'armhf').
    """
    return engine.supported_archs is None or arch in engine.supported_archs


def arch_unsupported_reason(engine: "EngineDefinition", arch: str) -> Optional[str]:
    """Human-readable reason an engine is unavailable on ``arch``, else None.

    Returned when the engine declares ``supported_archs`` and ``arch`` is not in
    it. Surfaced to the UI and used as the install failure message so the user
    sees an honest "not supported on this architecture" notice instead of a
    downstream compiler error (e.g. clang/`__int128`).
    """
    if engine_supports_arch(engine, arch):
        return None
    # Empty set: the engine builds on no architecture this project targets
    # (e.g. Koivisto's NNUE is x86-SIMD only; its upstream ARM NEON path is
    # incomplete and fails to compile on both armv7l and aarch64). Listing the
    # supported arches would render "Supported: ." here, so use a dedicated
    # message instead of the architecture-mismatch wording.
    if not engine.supported_archs:
        return (f"{engine.display_name} has no working ARM build (it requires "
                f"x86 SIMD); it cannot be installed on this device.")
    supported = ", ".join(sorted(engine.supported_archs))
    return (f"{engine.display_name} is not supported on this device's "
            f"architecture ({arch}). Supported: {supported}.")


# ---------------------------------------------------------------------------
# Ref selection helpers (pure)
#
# Source-built engines may be installed from a user-chosen git ref (tag/branch)
# so a newer-than-pinned release can be tried from the UI. These small pure
# functions decide which ref to build/record and whether the prebuilt archive
# (built only from the canonical ref) may satisfy a request. Kept free of I/O so
# the install flow's branching is unit-testable without compiling or networking.
# ---------------------------------------------------------------------------

def canonical_ref(engine: "EngineDefinition") -> str:
    """The ref the prebuilt archive represents and an unspecified install builds.

    For a pinned engine that is its catalog ``git_ref``; for an unpinned engine it
    is the default-branch sentinel :data:`DEFAULT_REF` (an unpinned clone, which is
    exactly what CI builds the prebuilt from).
    """
    return engine.git_ref or DEFAULT_REF


def resolve_requested_ref(engine: "EngineDefinition", requested: Optional[str]) -> str:
    """Resolve a requested ref to the concrete label to build and record.

    ``None`` (no selection / legacy client) resolves to the canonical ref so
    behavior is unchanged; an explicit ref (a tag, a branch, or
    :data:`DEFAULT_REF`) is used verbatim.
    """
    if requested is None:
        return canonical_ref(engine)
    return requested


def prebuilt_allowed_for_ref(engine: "EngineDefinition", requested: Optional[str]) -> bool:
    """Whether the prebuilt archive may satisfy ``requested``.

    The prebuilt is built solely from the canonical ref, so it may only serve an
    unspecified request or one that resolves to the canonical ref. Any other ref
    must build from source, or the install would ship the canonical binary while
    claiming a different version.
    """
    return requested is None or requested == canonical_ref(engine)


def git_ref_for_label(label: str) -> Optional[str]:
    """Map a recorded/selected ref label to a value for ``git clone --branch``.

    :data:`DEFAULT_REF` maps to ``None`` (omit ``--branch`` -> clone the default
    branch); any other label is a real tag/branch passed through unchanged.
    """
    return None if label == DEFAULT_REF else label


def parse_github_repo(repo_url: Optional[str]) -> Optional[tuple]:
    """Return ``(owner, repo)`` for a GitHub HTTPS URL, else None.

    Only github.com HTTPS URLs are supported (the only host whose tag API the refs
    endpoint queries). A non-GitHub host, a None/empty URL, or a malformed path
    yields None so callers degrade gracefully instead of crashing.
    """
    if not repo_url:
        return None
    prefix = "https://github.com/"
    if not repo_url.startswith(prefix):
        return None
    path = repo_url[len(prefix):]
    if path.endswith(".git"):
        path = path[:-len(".git")]
    parts = path.strip("/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def merge_ref_list(
    recommended: str,
    installed_ref: Optional[str],
    pin: Optional[str],
    working_refs: List[str],
    tags: List[str],
    default_branch: Optional[str],
) -> List[dict]:
    """Build the de-duplicated, flagged ref list the tag picker renders.

    The result lists each selectable ref once, recommended ref first, then the
    GitHub tags (newest-first as given), then any working-history refs not already
    present, and always includes the catalog pin, the installed ref, and a
    default-branch entry. Each entry carries display and state flags:

    * ``ref``: the value to send back to the install endpoint. The default branch
      uses the :data:`DEFAULT_REF` sentinel (an unpinned clone); tags use their
      own name.
    * ``label``: human-readable text (the branch name for the default entry,
      otherwise the ref itself).
    * ``kind``: ``"branch"`` for the default entry, else ``"tag"``.
    * ``known_working``: the catalog pin or any ref that built successfully here.
    * ``is_pin``: this is the catalog's verified pin.
    * ``installed``: this is the ref currently installed.

    Args:
        recommended: The canonical ref to surface first (pin or DEFAULT_REF).
        installed_ref: The currently-installed ref label, or None.
        pin: The catalog pin tag, or None for unpinned engines.
        working_refs: Refs that have ever built successfully on this device.
        tags: GitHub tag names, newest-first (may be empty if discovery failed).
        default_branch: The repo's default branch name for display, or None.
    """
    working_set = set(working_refs)

    # Ordered, de-duplicated ref values. DEFAULT_REF (default branch) is always
    # offered so the latest code can be tried even when no tags are discoverable.
    ordered: List[str] = []
    seen = set()

    def add(ref: Optional[str]) -> None:
        if ref is None or ref in seen:
            return
        seen.add(ref)
        ordered.append(ref)

    add(recommended)
    for tag in tags:
        add(tag)
    for ref in working_refs:
        add(ref)
    add(pin)
    add(installed_ref)
    add(DEFAULT_REF)

    entries: List[dict] = []
    for ref in ordered:
        is_default = ref == DEFAULT_REF
        entries.append({
            "ref": ref,
            "label": (default_branch or "default branch") if is_default else ref,
            "kind": "branch" if is_default else "tag",
            "known_working": ref == pin or ref in working_set,
            "is_pin": pin is not None and ref == pin,
            "installed": installed_ref is not None and ref == installed_ref,
        })
    return entries


# A selectable ref must start with an alphanumeric (so it can never be parsed as a
# ``git`` option like ``--upload-pack``) and may contain only the characters git
# refs use. ``..`` is rejected outright to bar path traversal and git revision
# ranges. The ref is passed to ``git clone --branch`` as a list argument (no shell),
# so this is defense-in-depth against malformed/abusive input, not the sole barrier.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def is_valid_ref(ref) -> bool:
    """Whether ``ref`` is an acceptable git ref to install.

    Accepts the :data:`DEFAULT_REF` sentinel and ordinary tag/branch names; rejects
    non-strings, empty values, anything containing ``..``, and names that do not
    match :data:`_REF_RE` (e.g. a leading ``-`` that could be read as a git option).
    """
    if not isinstance(ref, str) or not ref:
        return False
    if ".." in ref:
        return False
    return bool(_REF_RE.fullmatch(ref))


def expand_extra_files(base_dir: Path, patterns: List[str]) -> List[Path]:
    """Resolve ``extra_files`` entries against ``base_dir``.

    An entry containing a glob metacharacter (``*``, ``?`` or ``[``) is expanded
    with :meth:`Path.glob` so version-specific files can be matched without
    hardcoding a name (e.g. Arasan's ``*.nnue`` network, whose filename changes
    per release). A literal entry resolves to ``base_dir/entry`` when it exists.
    Missing literals are skipped (the caller logs them); only existing paths are
    returned.

    Args:
        base_dir: Directory the entries are relative to (a repo checkout, an
            extracted prebuilt archive's arch dir, or the engines dir).
        patterns: ``extra_files`` entries (literal names or glob patterns).

    Returns:
        Existing matched paths, glob matches sorted for determinism.
    """
    results: List[Path] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            results.extend(sorted(base_dir.glob(pattern)))
        else:
            candidate = base_dir / pattern
            if candidate.exists():
                results.append(candidate)
    return results


class InstallStatus(Enum):
    """Status of an engine in the install queue."""
    QUEUED = "queued"
    INSTALLING = "installing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueuedEngine:
    """An engine in the install queue."""
    name: str
    status: InstallStatus = InstallStatus.QUEUED
    progress: str = ""
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


# Engine definitions
# Ordered roughly by strength/popularity
ENGINES = {
    # === TOP TIER - World class engines ===
    "stockfish": EngineDefinition(
        name="stockfish",
        display_name="Stockfish",
        summary="~3500 ELO, #1 engine",
        description="World's strongest open-source chess engine. Uses NNUE neural network evaluation. The gold standard for computer chess analysis and play. Installed from system package - always available.",
        repo_url=None,
        build_commands=[],
        binary_path="",
        is_system_package=True,
        package_name="stockfish",
        extra_files=[],
        dependencies=[],
        can_uninstall=False,
        estimated_install_minutes=0,  # Pre-installed
    ),
    "berserk": EngineDefinition(
        name="berserk",
        display_name="Berserk",
        summary="~3400 ELO, top-3",
        description="Top-3 ranked open-source engine. Uses NNUE neural network for evaluation. Known for very strong tactical play and aggressive style. Excellent alternative to Stockfish.",
        repo_url="https://github.com/jhonnold/berserk.git",
        build_commands=[
            # Berserk's makefile default goal is `openbench`, which forces
            # ARCH=avx2 (x86 -m64 -mavx2 ...) and CC=clang -- neither valid on
            # ARM. Use the `build` target with ARCH=native so it compiles for the
            # host (-march=native, which enables AArch64 NEON on arm64), and
            # CC=gcc so no clang dependency is needed. `build` also pulls in the
            # download-network prerequisite, which fetches and embeds the NNUE
            # file. Berserk is 64-bit-only (see supported_archs), so this path is
            # only reached on arm64.
            "cd src && make build ARCH=native CC=gcc EXE=berserk",
        ],
        binary_path="src/berserk",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        build_timeout=1200,
        estimated_install_minutes=15,  # NNUE engine with limited parallelism
        has_prebuilt=True,
        # 64-bit ARM only. Berserk uses `__int128` (unsupported on 32-bit targets)
        # and its NEON path uses AArch64-only intrinsics (vmull_high_s8, vaddvq_*,
        # vpaddq_*), so it cannot build on armv7l/armhf.
        supported_archs=frozenset({"arm64"}),
    ),
    "koivisto": EngineDefinition(
        name="koivisto",
        display_name="Koivisto",
        summary="~3350 ELO, fast",
        description="Top-10 ranked engine with NNUE support. Known for fast search speed and aggressive playing style. Good for blitz and bullet games where speed matters.",
        repo_url="https://github.com/Luecx/Koivisto.git",
        build_commands=[
            # Parallelism comes from MAKEFLAGS (see _build_env); the temporary
            # build-memory swap covers the NNUE compile's memory use.
            "cd src_files && make EXE=koivisto",
        ],
        binary_path="src_files/koivisto",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        build_timeout=1200,
        estimated_install_minutes=15,  # NNUE engine with limited parallelism
        # No prebuilt: Koivisto cannot be built for ARM at all (see below), so
        # the CI archive never contains a working binary for it.
        has_prebuilt=False,
        # x86-only: Koivisto's NNUE layer (src_files/nn/defs.h) implements SIMD
        # for AVX512/AVX2/AVX/SSE2 plus an incomplete ARM NEON branch. That NEON
        # branch is broken upstream -- the store op is a stub (`#define
        # avx_store_reg exit(-1)`) and the load (`vldrq_p128`) type-mismatches the
        # register type -- and there is no scalar fallback. Both armv7l and
        # aarch64 select that same branch, so it fails to compile on every ARM
        # target. An empty set means "no supported architecture in this project".
        supported_archs=frozenset(),
    ),
    "ethereal": EngineDefinition(
        name="ethereal",
        display_name="Ethereal",
        summary="~3300 ELO, clean",
        description="Top-15 engine with NNUE. Known for clean, well-documented codebase. Great for those interested in chess programming. Solid positional play.",
        repo_url="https://github.com/AndyGrant/Ethereal.git",
        build_commands=[
            # Ethereal's Makefile declares `CC = clang` and its first (default)
            # target is `pgo`, so a bare `make` shells out to clang. clang is not
            # in this engine's dependencies (build-essential provides gcc, not
            # clang), so the build aborts with "clang: not found". Pin CC=gcc --
            # the same fix used for Berserk, which shares this Makefile family --
            # so the source build uses the compiler the dependencies actually
            # install and no heavyweight clang package is pulled onto the device.
            # On ARM the Makefile's x86 feature detection (POPCNT/AVX/PEXT) simply
            # finds none of those macros and adds no x86 flags, so gcc -march=native
            # produces a valid native binary.
            # Parallelism comes from MAKEFLAGS (see _build_env); the temporary
            # build-memory swap covers the NNUE compile's memory use.
            "cd src && make CC=gcc EXE=ethereal",
        ],
        binary_path="src/ethereal",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        build_timeout=1200,
        estimated_install_minutes=15,  # NNUE engine with limited parallelism
        has_prebuilt=True,
    ),
    
    # === STRONG TIER - Tournament-level engines ===
    # NOTE: Fire engine removed - uses Windows-specific <intrin.h> header, doesn't compile on ARM/Linux
    # NOTE: Laser engine removed - uses x86-specific flags (-msse3, -mpopcnt), doesn't compile on ARM
    "demolito": EngineDefinition(
        name="demolito",
        display_name="Demolito",
        summary="~2900 ELO, simple",
        description="Simple, efficient engine with clean C code. Fast to compile and run. Good for lower-powered devices. Solid but straightforward play.",
        repo_url="https://github.com/lucasart/Demolito.git",
        build_commands=[
            # Demolito's makefile declares `CC = clang` (its `default` target runs
            # `$(CC) -march=native ...`). Pin CC=gcc -- the same approach used for
            # Berserk and Ethereal -- so the build uses the compiler build-essential
            # provides instead of pulling the heavyweight LLVM toolchain onto the
            # device. The makefile's clang-only warning flags are gated behind
            # `ifeq ($(CC),clang)`, so gcc skips them; the source is portable C and
            # builds on both arm64 and armhf with gcc -march=native. Installing
            # clang on a 32-bit Pi Zero pulls hundreds of MB of LLVM-19 and was
            # timing out/failing, which then surfaced as a cryptic "clang: not
            # found" because a failed dependency install is non-fatal.
            "cd src && make CC=gcc",
        ],
        binary_path="src/demolito",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        estimated_install_minutes=3,  # Simple C engine
        has_prebuilt=True,
    ),
    "weiss": EngineDefinition(
        name="weiss",
        display_name="Weiss",
        summary="~2900 ELO, educational",
        description="Clean, educational engine great for learning chess programming. Well-commented source code. Solid playing strength despite simplicity.",
        repo_url="https://github.com/TerjeKir/weiss.git",
        build_commands=[
            # Weiss builds from src directory
            "cd src && make EXE=weiss",
        ],
        binary_path="src/weiss",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        estimated_install_minutes=5,  # Clean C engine
        has_prebuilt=True,
        # 64-bit ARM only. Weiss's TTIndex (transposition.h) uses the Lemire
        # fast-reduction trick `((unsigned __int128)key * count) >> 64`, and
        # `__int128` does not exist on 32-bit targets, so the build fails on
        # armv7l/armhf with an "unsupported integer type" error in transposition.h.
        # arm64 builds and runs fine (verified). Same constraint as Berserk.
        supported_archs=frozenset({"arm64"}),
    ),
    "arasan": EngineDefinition(
        name="arasan",
        display_name="Arasan",
        summary="~2900 ELO, veteran",
        description="Veteran engine in development since 1994. Very stable and reliable. NNUE support added recently. Great for consistent, predictable play.",
        repo_url="https://github.com/jdart1/arasan-chess.git",
        # Pin to a tagged release. Arasan's master NEON path has regressed (the
        # nnue/simd.h calcNnzData rewrite fails to compile), so an unpinned clone
        # builds or fails depending on the day. v25.4 (2026-04-14) is the latest
        # release verified to build and run on aarch64 with the recipe below. The
        # NNUE network is handled by a glob (build_commands/extra_files use *.nnue),
        # so bumping this tag -- or trying a newer tag from the UI picker -- needs no
        # filename change here.
        git_ref="v25.4",
        # The NNUE network and the Syzygy probing code (syzygy/src/tbprobe.h) are git
        # submodules, not part of the main tree. Without this the build dies at
        # "syzygy/src/tbprobe.h: No such file or directory".
        clone_with_submodules=True,
        build_commands=[
            # Arasan on 64-bit ARM (this engine is arm64-only; see supported_archs):
            #
            # * CC=clang++ -- the Makefile defaults to g++, but Arasan's NEON
            #   intrinsics (nnue/simddefs.h) rely on implicit conversions between NEON
            #   vector types (int16x8_t/int32x4_t/uint8x16_t) that clang accepts and
            #   g++ rejects ("cannot convert ..."). doc/BUILD.md states clang is the
            #   required/recommended compiler. g++ cannot build this engine.
            # * BUILD_TYPE=neon -- mandatory, not an optimization: the NNUE
            #   SparseLinear layer has `static_assert(0, "requires SIMD")`, so a
            #   non-SIMD build does not compile at all. neon is the only ARM SIMD
            #   path the Makefile defines.
            # * LDFLAGS overrides the Makefile's hardcoded `-fuse-ld=gold`; the gold
            #   linker was removed from binutils 2.44 (Raspberry Pi OS Trixie), so the
            #   default bfd linker must be used. A command-line LDFLAGS assignment wins
            #   over the Makefile's `:=` appends, dropping the gold flag. Objects are
            #   compiled without -flto, so using bfd costs nothing.
            # * EXE=arasan -- the Makefile otherwise emits ../bin/arasanx-64; EXE fixes
            #   the output name to ../bin/arasan matching binary_path.
            # Parallelism is no longer pinned here (-j1 was for OOM avoidance);
            # MAKEFLAGS (see _build_env) sets it, and the temporary build-memory
            # swap covers the memory the parallel NNUE compile uses.
            'cd src && make CC=clang++ EXE=arasan BUILD_TYPE=neon '
            'LDFLAGS="-O3 -fno-rtti -DNDEBUG"',
            # Stage the embedded NNUE network next to where the binary is installed.
            # Arasan loads the network from the executable's own directory and embeds
            # its filename, which is version-specific (changes per release). A glob
            # copies whichever network the checked-out ref ships -- without this the
            # tag picker could not build any ref other than the one whose exact
            # filename was hardcoded. Each release's network/ holds exactly the one
            # network its binary expects, so the glob installs the correct file.
            'cp -f network/*.nnue ./',
        ],
        binary_path="bin/arasan",
        is_system_package=False,
        package_name=None,
        # Installed alongside the binary in engines_dir; Arasan refuses to evaluate
        # ("failed to open network file") without it. Glob (not a fixed name) so any
        # release's network installs -- see the build cp step above.
        extra_files=["*.nnue"],
        # clang is required (see build_commands). bc/gawk are NOT needed: the Makefile
        # only invokes them in its g++ branch to compute the gcc version.
        dependencies=["build-essential", "git", "clang"],
        build_timeout=1800,
        estimated_install_minutes=25,  # NNUE engine, single-threaded build
        has_prebuilt=True,
        # arm64-only. 32-bit ARM (armhf) has no SIMD path in Arasan -- the Makefile
        # defines NEON flags solely for the arm64/aarch64 arch tokens, and the
        # non-SIMD build is blocked by the SparseLinear static_assert -- so it cannot
        # be built on armhf at all (mirrors the gating used for Berserk/Weiss).
        supported_archs=frozenset({"arm64"}),
    ),
    
    # === SPECIALTY ENGINES ===
    "rodentIV": EngineDefinition(
        name="rodentIV",
        display_name="Rodent IV",
        summary="~2800 ELO, 50+ styles",
        description="Personality engine with 50+ playing styles from beginner to GM level. Can emulate famous players or specific playing styles. Great for practice and entertainment.",
        repo_url="https://github.com/nescitus/rodent-iv.git",
        build_commands=[
            # Makefile is in sources/ directory; override EXENAME to output to repo root.
            #
            # 32-bit ARM link fix (-latomic): Rodent IV uses 64-bit std::atomic.
            # On armhf/armel the compiler lowers 8-byte atomics to libatomic calls
            # (__atomic_load_8/__atomic_store_8/...), so the link fails with
            # "undefined reference to __atomic_store_8" unless libatomic is linked.
            # The Makefile recipe places $(LDFLAGS) *before* the source objects, and
            # the toolchain links --as-needed by default, which drops a -latomic that
            # appears before any reference to it -- so it must be forced in with
            # -Wl,--no-as-needed. The override also preserves the Makefile's original
            # LDFLAGS (-s -lm). 64-bit targets inline these atomics and need no
            # libatomic, so the flag is added only for 32-bit ARM to avoid forcing an
            # unused libatomic dependency there.
            "cd sources && LDFLAGS='-s -lm'; "
            'case "$(uname -m)" in arm|armv*) '
            'LDFLAGS="$LDFLAGS -Wl,--no-as-needed -latomic";; esac; '
            'make EXENAME=../rodentIV LDFLAGS="$LDFLAGS"',
        ],
        binary_path="rodentIV",
        is_system_package=False,
        package_name=None,
        extra_files=["personalities", "books"],
        dependencies=["build-essential", "git"],
        estimated_install_minutes=8,  # Medium complexity with extra files
        has_prebuilt=True,
    ),
    "ct800": EngineDefinition(
        name="ct800",
        display_name="CT800",
        summary="~2300 ELO, retro",
        description="Emulates a dedicated chess computer. Classic playing style reminiscent of 1980s chess computers. Good for casual play with a nostalgic feel.",
        repo_url="https://github.com/bcm314/CT800.git",
        build_commands=[
            # Use the raspi build script, then rename output to fixed name
            "cd source/application-uci && mkdir -p output && bash make_ct800_raspi.sh && mv output/CT800_* output/ct800",
        ],
        # Binary renamed to fixed name ct800
        binary_path="source/application-uci/output/ct800",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        estimated_install_minutes=3,  # Simple C engine
        has_prebuilt=True,
    ),
    
    # === NEURAL NETWORK - HUMAN-LIKE ===
    # Maia uses a custom build script because:
    # 1. lc0 compilation is very memory-intensive (needs swap on Pi)
    # 2. Requires -j1 to avoid OOM kills during abseil compilation
    # 3. Complex meson options needed for ARM with BLAS-only backend
    # The build script handles all of this automatically.
    "maia": EngineDefinition(
        name="maia",
        display_name="Maia",
        summary="Human-like play",
        description="Neural network engine with two weight options: Maia (human-like play at ELO 1100-1900) and Leela (maximum strength). Uses lc0 backend. Pre-built binary available, or build takes 45-60 minutes on Pi.",
        repo_url=None,  # Using custom build script instead of git clone
        build_commands=[
            # Use the standalone build script that handles:
            # - Swap file creation if memory is low
            # - Single-threaded build to avoid OOM
            # - Correct meson options for ARM
            # - Weight downloads
            f"sudo {SCRIPTS_DIR}/build-maia.sh {ENGINES_DIR}/maia",
        ],
        binary_path="lc0",  # Script installs to ENGINES_DIR/maia/lc0
        is_system_package=False,
        package_name=None,
        extra_files=["maia_weights"],
        dependencies=[],  # Build script handles dependencies
        clone_with_submodules=False,  # Build script handles cloning
        build_timeout=7200,  # 2 hours - may need swap which is slow
        estimated_install_minutes=60,
        has_prebuilt=True,  # Pre-built includes lc0 binary + all Maia weights
    ),
    
    # === LIGHTWEIGHT/FAST COMPILE ===
    "zahak": EngineDefinition(
        name="zahak",
        display_name="Zahak",
        summary="~2700 ELO, Go-based",
        description="Written in Go programming language. Clean, modern codebase under active development. Good strength with fast compilation. Interesting alternative architecture.",
        repo_url="https://github.com/amanjpro/zahak.git",
        # Zahak is a Go module whose `main` package lives in the zahak/ subdir, so
        # a bare `go build` in the repo root fails with "no Go files in ...". The
        # build must also run the Makefile's `netgen` step first, which generates
        # engine/nn.go from default.nn (it is not committed) -- without it the
        # engine package does not compile. `make` (default goal `build`) does both
        # and emits bin/zahak. FLAGS overrides the Makefile's `CC=cc CGO_ENABLED=1`
        # default with CGO_ENABLED=0: CGO would pull in fathom's Syzygy C code and
        # require a C compiler not in our dependencies; disabling it selects
        # fathom_stub.go (no tablebase probing, irrelevant on-device) and keeps the
        # build to just golang+git.
        # Compile parallelism is NOT pinned here -- it comes from GOFLAGS=-p=N in
        # the build environment (see _build_env), the same single knob the
        # make-based engines use. The engine package embeds the ~1.5MB NNUE net as
        # generated source, so on a 512MB board parallel compiles of it would OOM
        # ("compile: signal: killed"); the temporary build-memory swap
        # (services.build_memory), acquired around the build, is what lets a
        # parallel build complete there instead of being killed.
        # `make` honors `git tag` for version stamping, so the shallow clone is fine.
        build_commands=[
            "make FLAGS='CGO_ENABLED=0'",
        ],
        binary_path="bin/zahak",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        # golang package name varies: 'golang' on older Debian, 'golang-go' on newer
        dependencies=["golang", "git"],
        # Compiling the NNUE-embedding package on a RAM-constrained board (even in
        # parallel, much of it spilling through swap) runs well past Go's usual
        # speed; the estimate paces the progress creep (which caps, so it never
        # shows "done" early).
        estimated_install_minutes=10,
        has_prebuilt=True,
    ),
    "smallbrain": EngineDefinition(
        name="smallbrain",
        display_name="Smallbrain",
        summary="~3000 ELO, compact",
        description="Compact NNUE engine with small binary size. Efficient code optimized for resource-constrained devices. Surprisingly strong for its size.",
        repo_url="https://github.com/Disservin/Smallbrain.git",
        build_commands=[
            # Parallelism comes from MAKEFLAGS (see _build_env); the temporary
            # build-memory swap covers the NNUE compile's memory use.
            "cd src && make EXE=smallbrain",
        ],
        binary_path="src/smallbrain",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        build_timeout=1200,
        estimated_install_minutes=12,  # Compact NNUE, faster than full NNUE engines
        has_prebuilt=True,
    ),
}


class EngineManager:
    """Manages installation and removal of chess engines.
    
    Supports queueing multiple engines for sequential installation.
    """
    
    def __init__(
        self,
        engines_dir: str = ENGINES_DIR,
        record_store: Optional[EngineInstallRecordStore] = None,
        tag_cache: Optional[GitHubTagCacheStore] = None,
    ):
        """Initialize the engine manager.
        
        Args:
            engines_dir: Directory where engines are installed
            record_store: Store for the durable installed-ref / working-ref
                history. Defaults to the module-level singleton; injectable so
                tests run against a temp file.
            tag_cache: Cache of GitHub tag lists for the release picker, used as a
                fallback when a fresh fetch fails. Defaults to the module-level
                singleton; injectable so tests run against a temp file.
        """
        self.engines_dir = Path(engines_dir)
        self.build_tmp = Path(BUILD_TMP)
        self._record_store = record_store if record_store is not None else INSTALL_RECORD_STORE
        self._tag_cache = tag_cache if tag_cache is not None else TAG_CACHE_STORE
        self._install_thread: Optional[threading.Thread] = None
        self._install_progress: str = ""
        self._install_error: Optional[str] = None
        self._installing_engine: Optional[str] = None
        
        # Install queue
        self._queue: List[QueuedEngine] = []
        self._queue_lock = threading.Lock()
        self._queue_worker_thread: Optional[threading.Thread] = None
        self._queue_running = False
        self._progress_callbacks: List[Callable[[str, str, str], None]] = []
        
        log.info(f"[EngineManager] Initialized with engines_dir={engines_dir}")
        log.debug(f"[EngineManager] Build temp directory: {BUILD_TMP}")
        log.debug(f"[EngineManager] Available engines: {list(ENGINES.keys())}")
    
    def is_installed(self, engine_name: str) -> bool:
        """Check if an engine is installed.
        
        Args:
            engine_name: Name of the engine to check
            
        Returns:
            True if the engine executable exists
        """
        if engine_name not in ENGINES:
            log.warning(f"[EngineManager] is_installed: Unknown engine '{engine_name}'")
            return False
        
        engine = ENGINES[engine_name]
        
        if engine.is_system_package:
            # Check if system command exists
            system_path = shutil.which(engine_name)
            is_installed = system_path is not None
            log.debug(f"[EngineManager] is_installed: {engine_name} (system package) = {is_installed}, path={system_path}")
            return is_installed
        else:
            # Check if binary exists in engines directory
            # Most engines: engines_dir/engine_name
            # Engines with custom scripts (repo_url=None): engines_dir/engine_name/binary_path
            if engine.repo_url is None and engine.binary_path:
                # Custom script installs to subdirectory
                engine_path = self.engines_dir / engine_name / engine.binary_path
            else:
                engine_path = self.engines_dir / engine_name
            exists = engine_path.exists()
            executable = os.access(engine_path, os.X_OK) if exists else False
            is_installed = exists and executable
            log.debug(f"[EngineManager] is_installed: {engine_name} = {is_installed} (exists={exists}, executable={executable}, path={engine_path})")
            return is_installed
    
    def get_engine_list(self) -> List[dict]:
        """Get list of all engines with installation status.
        
        Returns:
            List of dicts with engine info and installed status
        """
        log.debug("[EngineManager] get_engine_list: Building engine list")
        result = []
        installed_count = 0
        for name, engine in ENGINES.items():
            is_installed = self.is_installed(name)
            if is_installed:
                installed_count += 1
            
            result.append({
                "name": name,
                "display_name": engine.display_name,
                "summary": engine.summary,
                "description": engine.description,
                "installed": is_installed,
                "is_system_package": engine.is_system_package,
                "can_uninstall": engine.can_uninstall,
                "estimated_install_minutes": engine.estimated_install_minutes,
            })
        log.info(f"[EngineManager] get_engine_list: {installed_count}/{len(ENGINES)} engines installed")
        return result

    @staticmethod
    def _fetch_github_tags(repo_url: Optional[str], limit: int = 30) -> tuple:
        """Best-effort fetch of a GitHub repo's tags and default branch.

        Returns ``(tag_names, default_branch)``. On any failure -- no ``requests``,
        a non-GitHub URL, a network error, a rate-limit, or a non-200 -- returns
        ``([], None)`` so the refs endpoint degrades to locally-known refs (the pin,
        the working history, the default-branch option) instead of erroring. Tags
        are returned newest-first as GitHub orders them, capped at ``limit``.
        """
        parsed = parse_github_repo(repo_url)
        if parsed is None or not HAS_REQUESTS:
            return [], None
        owner, repo = parsed
        api = f"https://api.github.com/repos/{owner}/{repo}"
        tags: List[str] = []
        default_branch: Optional[str] = None
        try:
            tags_resp = requests.get(f"{api}/tags", params={"per_page": limit}, timeout=15)
            if tags_resp.status_code == 200:
                tags = [t.get("name") for t in tags_resp.json() if t.get("name")]
            else:
                log.info(f"[EngineManager] _fetch_github_tags: tags request returned {tags_resp.status_code} for {owner}/{repo}")
            repo_resp = requests.get(api, timeout=15)
            if repo_resp.status_code == 200:
                default_branch = repo_resp.json().get("default_branch")
        except requests.RequestException as e:
            log.warning(f"[EngineManager] _fetch_github_tags: network error for {owner}/{repo}: {e}")
            # Keep whatever was gathered before the failure (possibly nothing).
        return tags, default_branch

    def get_installed_ref(self, engine_name: str) -> Optional[str]:
        """Return the ref the engine is currently recorded as installed from.

        None when not recorded (never installed via this path, or uninstalled).
        Thin accessor over the record store so the web layer needs no direct
        dependency on it.
        """
        return self._record_store.installed_ref(engine_name)

    def _tags_with_cache(self, repo_url: Optional[str]) -> tuple:
        """Fetch GitHub tags, caching success and falling back to the cache on failure.

        A successful, non-empty fetch is cached (keyed by ``owner/repo``) and
        returned. When the fetch fails or yields nothing -- a rate-limit, an outage,
        or a fresh-boot network gap -- the last cached list is returned instead of
        degrading the picker to only locally-known refs. A genuinely tag-less repo
        (successful but empty) simply has nothing to cache or fall back to.

        Returns ``(tags, default_branch)``.
        """
        parsed = parse_github_repo(repo_url)
        repo_key = f"{parsed[0]}/{parsed[1]}" if parsed else None

        tags, default_branch = self._fetch_github_tags(repo_url)
        if tags:
            if repo_key:
                self._tag_cache.put(repo_key, tags, default_branch)
            return tags, default_branch

        if repo_key:
            cached = self._tag_cache.get(repo_key)
            if cached and cached.get("tags"):
                log.info(f"[EngineManager] _tags_with_cache: fetch failed for {repo_key}; using cached tags")
                # Prefer the freshly-fetched default branch if we got one, else the
                # cached value (a fetch can fail after the tags request succeeds).
                return cached["tags"], default_branch or cached.get("default_branch")
        return tags, default_branch

    def get_engine_refs(self, engine_name: str) -> dict:
        """Return the selectable git refs for an engine and their state flags.

        Combines live GitHub tags (best-effort) with the locally-known refs -- the
        catalog pin, the working-ref history, the installed ref -- so the tag picker
        can offer future releases while still marking which refs are known-working
        and which is installed. System packages and bundled engines (no repo_url)
        report ``source_installable=False`` and an empty ref list; the UI then omits
        the picker.

        Returns a JSON-serializable dict; see :func:`merge_ref_list` for the per-ref
        entry shape.
        """
        if engine_name not in ENGINES:
            return {"engine": engine_name, "source_installable": False, "refs": []}
        engine = ENGINES[engine_name]
        source_installable = not engine.is_system_package and engine.repo_url is not None
        if not source_installable:
            return {
                "engine": engine_name,
                "source_installable": False,
                "installed_ref": None,
                "recommended_ref": None,
                "default_branch": None,
                "refs": [],
            }

        installed_ref = self._record_store.installed_ref(engine_name)
        working = self._record_store.working_refs(engine_name)
        pin = engine.git_ref
        recommended = canonical_ref(engine)
        tags, default_branch = self._tags_with_cache(engine.repo_url)
        refs = merge_ref_list(
            recommended=recommended,
            installed_ref=installed_ref,
            pin=pin,
            working_refs=working,
            tags=tags,
            default_branch=default_branch,
        )
        return {
            "engine": engine_name,
            "source_installable": True,
            "installed_ref": installed_ref,
            "recommended_ref": recommended,
            "default_branch": default_branch,
            "refs": refs,
        }

    def install_engine(
        self,
        engine_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        stage_callback: Optional[Callable[[InstallStage, str, Optional[float]], None]] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Install an engine.
        
        For system packages, uses apt-get.
        For source engines, clones repo and builds.
        
        Args:
            engine_name: Name of the engine to install
            progress_callback: Optional callback for free-text progress messages
                (legacy contract, retained for the queue path).
            stage_callback: Optional callback invoked with (stage, message,
                download_fraction) on each progress update. ``download_fraction``
                is 0..1 during a download (real byte progress) and None otherwise.
                Used by the web layer to drive the structured progress bar.
            ref: Optional git ref (tag/branch, or the :data:`DEFAULT_REF`
                sentinel for the default branch) to install for a source-built
                engine. None means the canonical ref (the catalog pin, or the
                default branch for unpinned engines) and preserves the prior
                behavior. A non-canonical ref forces a source build because the
                prebuilt archive only carries the canonical build. Ignored for
                system packages.
            
        Returns:
            True if installation succeeded
        """
        log.info(f"[EngineManager] install_engine: Starting installation of '{engine_name}'")
        
        if engine_name not in ENGINES:
            log.error(f"[EngineManager] install_engine: Unknown engine '{engine_name}' - not in ENGINES dict")
            self._install_error = f"Unknown engine: {engine_name}"
            return False
        
        engine = ENGINES[engine_name]
        
        self._installing_engine = engine_name
        self._install_error = None
        
        log.info(f"[EngineManager] install_engine: Engine details - display_name='{engine.display_name}', "
                 f"is_system_package={engine.is_system_package}, repo_url={engine.repo_url}")

        # Reject unsupported architectures up front. Without this, an engine that
        # cannot build on this CPU (e.g. Berserk on 32-bit ARM) would fall through
        # to a source build and fail with a confusing downstream compiler error.
        arch = self._get_arch()
        unsupported = arch_unsupported_reason(engine, arch)
        if unsupported is not None:
            self._install_error = unsupported
            log.warning(f"[EngineManager] install_engine: {unsupported}")
            self._installing_engine = None
            return False

        current_stage = InstallStage.STARTING

        def update_progress(msg: str, stage: Optional[InstallStage] = None,
                            fraction: Optional[float] = None):
            nonlocal current_stage
            if stage is not None:
                current_stage = stage
            self._install_progress = msg
            log.info(f"[EngineManager] [Progress] {msg}")
            if progress_callback:
                progress_callback(msg)
            if stage_callback:
                stage_callback(current_stage, msg, fraction)
        
        # Resolve which ref to build and record. ``ref`` is the user's selection
        # (or None for the canonical ref). The prebuilt archive carries only the
        # canonical build, so it can satisfy the request only when the request is
        # canonical; any other ref must build from source.
        resolved_ref = resolve_requested_ref(engine, ref)
        use_prebuilt = prebuilt_allowed_for_ref(engine, ref)
        log.info(f"[EngineManager] install_engine: requested ref={ref!r}, resolved={resolved_ref!r}, "
                 f"prebuilt_allowed={use_prebuilt}")

        # Time the whole attempt for the event log (the operator-facing "how long
        # did installing X take" record). `success` is initialized here so the
        # `finally` can report the outcome even if an exception skips its
        # assignment in the try body.
        install_started_at = time.monotonic()
        success = False

        try:
            if engine.is_system_package:
                log.info(f"[EngineManager] install_engine: Using system package installation for '{engine_name}'")
                success = self._install_system_package(engine, update_progress)
            elif engine.has_prebuilt and use_prebuilt and self._try_install_prebuilt(engine, update_progress):
                # Pre-built binary downloaded and installed successfully
                log.info(f"[EngineManager] install_engine: Installed pre-built binary for '{engine_name}'")
                success = True
            else:
                log.info(f"[EngineManager] install_engine: Using source build installation for '{engine_name}'")
                # A source build is the only install path that compiles, so it is
                # the only one that can OOM on a constrained board. Hold extra swap
                # (zram + temporary SD backstop) just for its duration; the apt and
                # prebuilt-download paths above need no extra memory and so do not
                # acquire it (avoiding pointless swap setup/teardown). This also
                # covers the prebuilt-fetch-failed fallback, which reaches here.
                #
                # The per-engine compile-memory guards (-j1/-j2) were removed in
                # favor of this swap, so a build now REQUIRES the extra memory. If
                # it cannot be reserved (helper missing, sudo grant absent, zram/
                # swapon failed), abort before compiling with a visible message
                # rather than running unguarded at full parallelism and OOM-ing --
                # failing loudly is preferable to a silent crash mid-build.
                with build_memory() as memory_ready:
                    if not memory_ready:
                        self._install_error = (
                            f"Could not reserve the extra memory needed to build "
                            f"{engine.display_name}. The install was stopped before "
                            f"compiling to avoid running out of memory. Update or "
                            f"reinstall Universal Chess so the build can reserve "
                            f"memory, then try again."
                        )
                        log.error(
                            "[EngineManager] install_engine: aborting source build for "
                            f"'{engine_name}': build memory could not be acquired"
                        )
                        success = False
                    else:
                        success = self._install_from_source(engine, update_progress, ref_label=resolved_ref)

            # Record the installed ref on success for source-built engines so the
            # UI can show what is installed and mark refs that have ever built
            # here as known-working. System packages have no ref concept.
            if success and not engine.is_system_package:
                self._record_store.record_install(engine_name, resolved_ref)

            if success:
                log.info(f"[EngineManager] install_engine: Successfully installed '{engine_name}' (ref={resolved_ref})")
            else:
                log.error(f"[EngineManager] install_engine: Failed to install '{engine_name}' - error: {self._install_error}")
            
            return success
        except subprocess.TimeoutExpired as e:
            self._install_error = f"Command timed out: {e.cmd}"
            log.error(f"[EngineManager] install_engine: Timeout during installation of '{engine_name}': {e}")
            return False
        except subprocess.SubprocessError as e:
            self._install_error = f"Subprocess error: {e}"
            log.error(f"[EngineManager] install_engine: Subprocess error during installation of '{engine_name}': {e}")
            return False
        except OSError as e:
            self._install_error = f"OS error: {e}"
            log.error(f"[EngineManager] install_engine: OS error during installation of '{engine_name}': {e}")
            return False
        except Exception as e:
            self._install_error = str(e)
            log.error(f"[EngineManager] install_engine: Unexpected exception during installation of '{engine_name}': {type(e).__name__}: {e}")
            import traceback
            log.error(f"[EngineManager] install_engine: Traceback:\n{traceback.format_exc()}")
            return False
        finally:
            self._installing_engine = None
            # One persistent, timed record per install attempt (success or
            # failure), so the Settings event-log viewer can show what was
            # installed and how long it took. Ref is meaningful only for
            # source-built engines.
            elapsed_ms = int((time.monotonic() - install_started_at) * 1000)
            ref_suffix = (
                f" ({resolved_ref})" if (not engine.is_system_package and resolved_ref) else ""
            )
            if success:
                log_event(
                    "engine_install",
                    f"Installed {engine.display_name}{ref_suffix}",
                    level="info",
                    duration_ms=elapsed_ms,
                )
            else:
                detail = self._install_error or "unknown error"
                log_event(
                    "engine_install",
                    f"Install failed: {engine.display_name}{ref_suffix} - {detail}",
                    level="error",
                    duration_ms=elapsed_ms,
                )
    
    def _install_system_package(
        self,
        engine: EngineDefinition,
        update_progress: Callable[..., None]
    ) -> bool:
        """Install engine from system package.
        
        Args:
            engine: Engine definition
            update_progress: Callback for progress messages (msg, stage, fraction)
            
        Returns:
            True if installation succeeded
        """
        log.info(f"[EngineManager] _install_system_package: Installing '{engine.name}' via apt package '{engine.package_name}'")
        update_progress(f"Installing {engine.display_name} from system package...", InstallStage.INSTALLING_DEPS)

        # Heal any interrupted dpkg transaction first; otherwise both the update
        # and install below abort on "dpkg was interrupted".
        if not self._recover_dpkg_or_abort(f"install {engine.display_name}"):
            return False

        # Update package list
        log.debug("[EngineManager] _install_system_package: Running apt-get update")
        result = subprocess.run(
            ["sudo", "apt-get", "update", "-qq"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            log.warning(f"[EngineManager] _install_system_package: apt-get update returned non-zero ({result.returncode})")
            log.warning(f"[EngineManager] _install_system_package: apt-get update stderr: {result.stderr.strip()}")
        else:
            log.debug("[EngineManager] _install_system_package: apt-get update completed successfully")
        
        # Install package
        update_progress(f"Installing {engine.package_name}...")
        log.info(f"[EngineManager] _install_system_package: Running apt-get install -y {engine.package_name}")
        result = subprocess.run(
            ["sudo", "apt-get", "install", "-y", engine.package_name],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            self._install_error = result.stderr.strip() or f"apt-get install failed with code {result.returncode}"
            log.error(f"[EngineManager] _install_system_package: apt-get install failed with code {result.returncode}")
            log.error(f"[EngineManager] _install_system_package: stdout: {result.stdout.strip()}")
            log.error(f"[EngineManager] _install_system_package: stderr: {result.stderr.strip()}")
            return False
        
        log.info(f"[EngineManager] _install_system_package: apt-get install completed successfully")
        if result.stdout.strip():
            log.debug(f"[EngineManager] _install_system_package: stdout: {result.stdout.strip()[:200]}")
        
        # Create symlink in engines directory
        system_path = shutil.which(engine.name)
        if system_path:
            log.info(f"[EngineManager] _install_system_package: Found system binary at {system_path}")
            link_path = self.engines_dir / engine.name
            
            # Ensure engines directory exists
            if not self.engines_dir.exists():
                log.info(f"[EngineManager] _install_system_package: Creating engines directory {self.engines_dir}")
                self.engines_dir.mkdir(parents=True, exist_ok=True)
            
            if link_path.exists() or link_path.is_symlink():
                log.debug(f"[EngineManager] _install_system_package: Removing existing file/symlink at {link_path}")
                link_path.unlink(missing_ok=True)
            
            link_path.symlink_to(system_path)
            log.info(f"[EngineManager] _install_system_package: Created symlink {link_path} -> {system_path}")
            update_progress(f"Created symlink: {link_path} -> {system_path}", InstallStage.INSTALLING_FILES)
        else:
            log.warning(f"[EngineManager] _install_system_package: Could not find '{engine.name}' in PATH after installation")
        
        update_progress(f"{engine.display_name} installed successfully", InstallStage.INSTALLING_FILES)
        log.info(f"[EngineManager] _install_system_package: Successfully installed '{engine.name}'")
        return True
    
    def _get_arch(self) -> str:
        """Get the current architecture for pre-built binary selection.

        Thin instance wrapper over the module-level :func:`get_current_arch` so
        existing call sites keep working while the mapping lives in one place.

        Returns:
            'arm64' for 64-bit ARM, 'armhf' for 32-bit ARM
        """
        return get_current_arch()

    @staticmethod
    def _missing_packages(packages: List[str]) -> List[str]:
        """Return the subset of apt packages that are not currently installed.

        Uses ``dpkg-query`` (the local package database) rather than re-running apt,
        so it reports the actual installed state regardless of why an install failed.
        A package is considered present only when dpkg reports it ``install ok
        installed``; ``half-configured``/``unpacked``/absent all count as missing,
        because the build genuinely cannot rely on it.

        Args:
            packages: apt package names the build declared as dependencies.

        Returns:
            The names that are not fully installed, preserving input order.
        """
        missing: List[str] = []
        for pkg in packages:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or "install ok installed" not in result.stdout:
                missing.append(pkg)
        return missing

    def _recover_dpkg_or_abort(self, action: str) -> bool:
        """Finish any interrupted dpkg transaction before an apt step.

        A prior killed apt/dpkg run can leave the database half-configured, after
        which every apt operation aborts with "dpkg was interrupted, you must
        manually run 'dpkg --configure -a'" -- the failure that blocked the Zahak
        ``golang`` install. :func:`apt_recovery.recover_interrupted_dpkg` finishes
        that transaction in one shared place.

        Returns True if the caller may proceed with its apt command. Returns False
        only when recovery had to restart this service (our own package was
        half-configured, so configuring it re-runs our postinst). In that case the
        repair now runs out-of-process and will restart us, so the install cannot
        complete: a user-facing message is set and the caller must abort. A failed
        recovery still returns True so the subsequent apt step surfaces the genuine
        error instead of masking it.

        Args:
            action: what the user was doing, phrased to complete "You will need to
                <action> after the service restarts" (e.g. "install Arasan").
        """
        outcome = apt_recovery.recover_interrupted_dpkg()
        if outcome is RecoveryOutcome.DEFERRED_RESTART:
            self._install_error = (
                "Fixing incomplete install of Universal Chess. "
                f"You will need to {action} after the service restarts."
            )
            log.warning(
                f"[EngineManager] dpkg recovery deferred to a service restart; aborting '{action}'"
            )
            return False
        return True

    def _copy_extra_files(self, src_base: Path, patterns: List[str]) -> None:
        """Install an engine's ``extra_files`` from ``src_base`` into engines_dir.

        Each declared entry is resolved against ``src_base`` (a repo checkout or an
        extracted prebuilt archive's arch dir) via :func:`expand_extra_files`, which
        supports both literal names (personalities, books, weights) and glob
        patterns. Globs let version-specific files install without hardcoding a name
        -- Arasan's NNUE network filename changes per release, so a ``*.nnue`` glob
        installs whatever ref's network is present and lets non-pinned tags build.

        Matches are installed flat at ``engines_dir/<name>``, replacing any existing
        destination for directories. A literal entry that does not resolve is logged
        (likely a build that did not produce an expected file); a glob with no match
        is silently skipped (not necessarily an error).
        """
        for pattern in patterns:
            is_glob = any(ch in pattern for ch in "*?[")
            if not is_glob and not (src_base / pattern).exists():
                log.warning(f"[EngineManager] _copy_extra_files: Declared extra not found: {src_base / pattern}")
        for match in expand_extra_files(src_base, patterns):
            dst = self.engines_dir / match.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if match.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(match, dst)
            else:
                shutil.copy2(match, dst)
            log.info(f"[EngineManager] _copy_extra_files: Installed extra '{match.name}' from {match}")

    def _try_install_prebuilt(
        self,
        engine: EngineDefinition,
        update_progress: Callable[..., None]
    ) -> bool:
        """Try to install engine from pre-built binary.
        
        Downloads the engine binary from the latest GitHub release if available.
        Falls back to building from source if download fails.
        
        Args:
            engine: Engine definition
            update_progress: Callback for progress messages (msg, stage, fraction)
            
        Returns:
            True if pre-built binary was installed successfully
        """
        if not engine.has_prebuilt:
            log.debug(f"[EngineManager] _try_install_prebuilt: Engine '{engine.name}' has no pre-built binary")
            return False
        
        if not HAS_REQUESTS:
            log.warning("[EngineManager] _try_install_prebuilt: 'requests' module not available, cannot download pre-built")
            return False
        
        arch = self._get_arch()
        archive_name = PREBUILT_ARCHIVE_NAME_TEMPLATE.format(arch=arch)
        
        log.info(f"[EngineManager] _try_install_prebuilt: Attempting to download pre-built '{engine.name}' for {arch}")
        update_progress(f"Checking for pre-built {engine.display_name}...", InstallStage.CHECKING_PREBUILT)
        
        try:
            # Scan the releases list (newest-first), not /releases/latest: the
            # latter 404s when only prereleases exist. Find the newest release
            # that actually carries this arch's engine archive.
            response = requests.get(GITHUB_RELEASES_URL, timeout=30)
            if response.status_code != 200:
                log.warning(f"[EngineManager] _try_install_prebuilt: GitHub API returned {response.status_code}")
                return False

            releases = response.json()
            download_url = find_prebuilt_archive_url(releases, archive_name)

            if not download_url:
                log.info(f"[EngineManager] _try_install_prebuilt: No pre-built archive '{archive_name}' in any release")
                return False
            
            # Download the archive
            update_progress(f"Downloading {engine.display_name}...", InstallStage.DOWNLOADING, 0.0)
            log.info(f"[EngineManager] _try_install_prebuilt: Downloading from {download_url}")
            
            download_response = requests.get(download_url, stream=True, timeout=300)
            if download_response.status_code != 200:
                log.warning(f"[EngineManager] _try_install_prebuilt: Download returned {download_response.status_code}")
                return False
            
            # Save to temp file
            tmp_archive = Path(BUILD_TMP) / archive_name
            tmp_archive.parent.mkdir(parents=True, exist_ok=True)
            
            total_size = int(download_response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(tmp_archive, 'wb') as f:
                for chunk in download_response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = (downloaded * 100) // total_size
                        update_progress(
                            f"Downloading {engine.display_name}... {pct}%",
                            InstallStage.DOWNLOADING, downloaded / total_size,
                        )
            
            # Extract the archive
            update_progress(f"Extracting {engine.display_name}...", InstallStage.INSTALLING_FILES)
            log.info(f"[EngineManager] _try_install_prebuilt: Extracting {tmp_archive}")
            
            extract_dir = Path(BUILD_TMP) / "prebuilt"
            extract_dir.mkdir(parents=True, exist_ok=True)
            
            with tarfile.open(tmp_archive, 'r:gz') as tar:
                tar.extractall(extract_dir)
            
            # Find and copy the engine binary
            # For most engines: arch/engine_name (single binary)
            # For maia: arch/maia/ (directory with lc0 + maia_weights/)
            source_path = extract_dir / arch / engine.name
            
            if source_path.is_dir():
                # Engine is a directory (e.g., maia with lc0 + weights)
                dest_path = Path(self.engines_dir) / engine.name
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                update_progress(f"Installing {engine.display_name}...", InstallStage.INSTALLING_FILES)
                
                # Copy entire directory
                if dest_path.exists():
                    shutil.rmtree(dest_path)
                shutil.copytree(source_path, dest_path)
                
                # Make binaries executable
                for binary in dest_path.glob('*'):
                    if binary.is_file() and not binary.suffix:
                        os.chmod(binary, 0o755)
                
                log.info(f"[EngineManager] _try_install_prebuilt: Installed directory '{engine.name}'")
            elif source_path.exists():
                # Single binary file
                dest_path = Path(self.engines_dir) / engine.name
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                update_progress(f"Installing {engine.display_name}...", InstallStage.INSTALLING_FILES)
                shutil.copy2(source_path, dest_path)
                os.chmod(dest_path, 0o755)

                # Install any extra files shipped alongside the binary in the archive
                # (e.g. Arasan's NNUE network, Rodent IV's personalities/books). The
                # source build copies these from the repo; the prebuilt path copies the
                # equivalents staged at <arch>/<extra> or the engine is installed
                # incomplete (Arasan would fail at runtime with "failed to open network
                # file"). The same glob-aware helper is used as the source path so
                # version-specific names (e.g. *.nnue) resolve identically.
                self._copy_extra_files(extract_dir / arch, engine.extra_files)
            else:
                log.warning(f"[EngineManager] _try_install_prebuilt: Binary not found at {source_path}")
                return False
            
            # Cleanup
            shutil.rmtree(extract_dir, ignore_errors=True)
            tmp_archive.unlink(missing_ok=True)
            
            log.info(f"[EngineManager] _try_install_prebuilt: Successfully installed pre-built '{engine.name}'")
            update_progress(f"{engine.display_name} installed successfully (pre-built)", InstallStage.INSTALLING_FILES)
            return True
            
        except requests.RequestException as e:
            log.warning(f"[EngineManager] _try_install_prebuilt: Network error: {e}")
            return False
        except (tarfile.TarError, OSError) as e:
            log.warning(f"[EngineManager] _try_install_prebuilt: Extract/install error: {e}")
            return False
        except Exception as e:
            log.warning(f"[EngineManager] _try_install_prebuilt: Unexpected error: {e}")
            return False

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """SIGKILL the process group led by ``proc`` and reap it.

        Build commands spawn children (``make`` -> compiler/linker). The build is
        launched with ``start_new_session=True`` so the whole tree shares a process
        group; killing the group on timeout avoids orphaning a compiler that would
        keep consuming a constrained board's CPU/RAM after the install gave up.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()

    def _run_build_command(
        self,
        cmd: str,
        cwd: Path,
        timeout: int,
        on_line: Callable[[str], None],
    ) -> Tuple[int, str]:
        """Run one build command, streaming combined stdout+stderr line-by-line.

        A build on a constrained board can run for many minutes producing no
        captured output until it finishes, so the UI cannot distinguish a slow
        build from a hung one. Streaming each line to ``on_line`` lets the caller
        surface live progress.

        A reader thread drains the pipe so the main loop can enforce ``timeout``
        even when the build is silent (no newline to unblock a plain ``readline``);
        on timeout the whole process group is killed and ``TimeoutExpired`` is
        raised. Returns ``(returncode, tail)`` where ``tail`` is the last
        :data:`_BUILD_TAIL_LINES` lines of output, used to build the failure
        message (stdout and stderr are merged so the real error -- e.g. the OOM
        "compile: signal: killed" line -- is captured regardless of stream).
        """
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            # Centralized compile parallelism (MAKEFLAGS/GOFLAGS); see _build_env.
            env=_build_env(),
        )
        tail: "deque[str]" = deque(maxlen=_BUILD_TAIL_LINES)
        line_q: Queue = Queue()

        def _reader() -> None:
            # proc.stdout is line-buffered text; iteration yields whole lines.
            for raw in proc.stdout:  # type: ignore[union-attr]
                line_q.put(raw.rstrip("\n"))
            line_q.put(None)  # EOF sentinel

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_process_group(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                item = line_q.get(timeout=min(remaining, 1.0))
            except Empty:
                continue
            if item is None:
                break
            tail.append(item)
            on_line(item)

        proc.wait()
        if proc.stdout:
            proc.stdout.close()
        return proc.returncode, "\n".join(tail)

    def _make_build_progress_updater(
        self,
        display_name: str,
        update_progress: Callable[..., None],
    ) -> Callable[[str], None]:
        """Return an ``on_line`` callback that surfaces throttled build progress.

        Each non-empty build-output line becomes the live install message (still in
        the BUILDING stage, so the time-based percent creep keeps running). Updates
        are throttled to :data:`_BUILD_PROGRESS_THROTTLE_SECONDS` because every
        update persists the install state to the (SD-card) store and build output
        can be chatty; the throttle keeps the message current without hammering the
        disk.
        """
        last_update = {"at": 0.0}

        def on_line(line: str) -> None:
            text = line.strip()
            if not text:
                return
            now = time.monotonic()
            if now - last_update["at"] < _BUILD_PROGRESS_THROTTLE_SECONDS:
                return
            last_update["at"] = now
            update_progress(f"Building {display_name}: {text[:80]}", InstallStage.BUILDING)

        return on_line

    def _install_from_source(
        self,
        engine: EngineDefinition,
        update_progress: Callable[..., None],
        ref_label: Optional[str] = None,
    ) -> bool:
        """Install engine by building from source.
        
        Args:
            engine: Engine definition
            update_progress: Callback for progress messages (msg, stage, fraction)
            ref_label: Resolved ref label to build (a tag/branch, or the
                :data:`DEFAULT_REF` sentinel for the default branch). None falls
                back to the catalog ``git_ref`` (the legacy behavior). The
                :data:`DEFAULT_REF` sentinel maps to an unpinned clone even for a
                pinned engine, so the latest code can be tried from the UI.
            
        Returns:
            True if installation succeeded
        """
        # The effective git ref drives the clone: a real tag/branch is passed to
        # ``--branch``; None clones the default branch. When the caller requests a
        # ref it overrides the catalog pin (that is the point of the tag picker).
        effective_git_ref = git_ref_for_label(ref_label) if ref_label is not None else engine.git_ref
        log.info(f"[EngineManager] _install_from_source: ref_label={ref_label!r} -> effective_git_ref={effective_git_ref!r}")
        log.info(f"[EngineManager] _install_from_source: Starting source build for '{engine.name}'")
        log.info(f"[EngineManager] _install_from_source: Repo URL: {engine.repo_url}")
        log.info(f"[EngineManager] _install_from_source: Build commands: {engine.build_commands}")
        log.info(f"[EngineManager] _install_from_source: Binary path: {engine.binary_path}")
        
        # Ensure build directory exists
        log.debug(f"[EngineManager] _install_from_source: Creating build temp directory {self.build_tmp}")
        self.build_tmp.mkdir(parents=True, exist_ok=True)
        repo_dir = self.build_tmp / engine.name
        log.debug(f"[EngineManager] _install_from_source: Repo directory: {repo_dir}")
        
        # Install build dependencies.
        #
        # A failed dependency install must abort the build. Previously this only
        # logged a warning and continued, so a missing build tool surfaced much later
        # as a cryptic compiler/Makefile error from the *build* step (e.g. Ethereal
        # and Demolito died with "clang: not found", Arasan with "No 'bc' found")
        # rather than an actionable "could not install <pkg>". The apt failure was
        # usually environmental -- e.g. a half-configured package wedging apt, or the
        # dpkg lock held -- which the cryptic build error completely hid. Verify the
        # packages are actually present after the install and stop with a clear
        # message naming what is missing if not.
        if engine.dependencies:
            update_progress(f"Installing build dependencies...", InstallStage.INSTALLING_DEPS)
            # Heal any interrupted dpkg transaction first; otherwise the apt-get
            # below aborts on "dpkg was interrupted" (the Zahak golang failure).
            if not self._recover_dpkg_or_abort(f"install {engine.display_name}"):
                return False
            deps = " ".join(engine.dependencies)
            log.info(f"[EngineManager] _install_from_source: Installing dependencies: {deps}")
            result = subprocess.run(
                f"sudo apt-get install -y {deps}",
                shell=True, capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                log.warning(f"[EngineManager] _install_from_source: Dependency install returned non-zero ({result.returncode})")
                log.warning(f"[EngineManager] _install_from_source: Dependency stderr: {result.stderr.strip()}")
            else:
                log.info(f"[EngineManager] _install_from_source: Dependencies installed successfully")

            missing = self._missing_packages(engine.dependencies)
            if missing:
                # Surface the apt error (truncated) so the real cause -- not just the
                # symptom -- is visible to the user in the install-error UI.
                apt_err = (result.stderr.strip() or result.stdout.strip())[:200]
                self._install_error = (
                    f"Could not install required build dependencies: {', '.join(missing)}. "
                    f"apt error: {apt_err}" if apt_err else
                    f"Could not install required build dependencies: {', '.join(missing)}."
                )
                log.error(f"[EngineManager] _install_from_source: Missing dependencies after install: {missing}")
                return False
        else:
            log.debug(f"[EngineManager] _install_from_source: No dependencies to install")
        
        # A build tied to a specific ref must build from exactly that ref. Reusing a
        # leftover checkout from a previous install -- possibly a different ref, or a
        # tree with stale object files from another BUILD_TYPE -- would silently build
        # the wrong thing. Shallow tag clones also cannot be `git pull`-ed onto a new
        # tag cleanly. So start from a clean clone whenever a specific ref is targeted
        # (a catalog pin, or any ref the user picked -- including explicitly choosing
        # the default branch after a previous tagged install).
        must_clean_clone = ref_label is not None or effective_git_ref is not None
        if must_clean_clone and engine.repo_url is not None and repo_dir.exists():
            log.info(f"[EngineManager] _install_from_source: Targeting ref {effective_git_ref or 'default branch'}; removing stale checkout {repo_dir}")
            shutil.rmtree(repo_dir, ignore_errors=True)

        # Clone or update repository (skip if repo_url is None - engine uses custom build script)
        if engine.repo_url is None:
            log.info(f"[EngineManager] _install_from_source: No repo_url - engine uses custom build script")
            # Ensure repo_dir exists for build commands that might need a working directory
            repo_dir.mkdir(parents=True, exist_ok=True)
        elif repo_dir.exists():
            update_progress(f"Updating {engine.display_name} source...", InstallStage.CLONING)
            log.info(f"[EngineManager] _install_from_source: Repo exists, running git pull in {repo_dir}")
            result = subprocess.run(
                ["git", "pull"],
                cwd=repo_dir, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                log.warning(f"[EngineManager] _install_from_source: git pull failed ({result.returncode}): {result.stderr.strip()}")
                # Try to continue anyway - maybe just network issue
            else:
                log.info(f"[EngineManager] _install_from_source: git pull successful")
            
            # Update submodules if needed
            if engine.clone_with_submodules:
                update_progress(f"Updating submodules...", InstallStage.CLONING)
                log.info(f"[EngineManager] _install_from_source: Updating submodules")
                result = subprocess.run(
                    ["git", "submodule", "update", "--init", "--recursive"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=300
                )
                if result.returncode != 0:
                    log.warning(f"[EngineManager] _install_from_source: submodule update failed: {result.stderr.strip()}")
        else:
            update_progress(f"Cloning {engine.display_name} repository...", InstallStage.CLONING)
            log.info(f"[EngineManager] _install_from_source: Cloning {engine.repo_url} to {repo_dir}")
            
            # Build clone command. Always shallow (--depth 1): we never need history,
            # only a buildable tree, and shallow keeps low-memory devices from cloning
            # large repos (Arasan's book PGNs are sizeable). --branch accepts a tag or
            # branch name, so a pinned git_ref clones exactly that release.
            clone_cmd = ["git", "clone", "--depth", "1"]
            if effective_git_ref:
                clone_cmd.extend(["--branch", effective_git_ref])
            if engine.clone_with_submodules:
                clone_cmd.extend(["--recurse-submodules", "--shallow-submodules"])
            clone_cmd.extend([engine.repo_url, str(repo_dir)])
            
            log.info(f"[EngineManager] _install_from_source: Clone command: {' '.join(clone_cmd)}")
            result = subprocess.run(
                clone_cmd,
                capture_output=True, text=True, timeout=600  # Longer timeout for submodules
            )
            if result.returncode != 0:
                self._install_error = f"Clone failed: {result.stderr.strip()}"
                log.error(f"[EngineManager] _install_from_source: git clone failed ({result.returncode})")
                log.error(f"[EngineManager] _install_from_source: git clone stdout: {result.stdout.strip()}")
                log.error(f"[EngineManager] _install_from_source: git clone stderr: {result.stderr.strip()}")
                return False
            log.info(f"[EngineManager] _install_from_source: git clone successful")
        
        # Build. Output is streamed line-by-line (not captured all-at-once) so the
        # install message reflects live build activity -- on a constrained board a
        # source build runs for minutes and a static message is indistinguishable
        # from a hang. The percent continues to creep over the BUILDING band by
        # elapsed time; the streamed lines only refresh the message.
        update_progress(f"Building {engine.display_name}...", InstallStage.BUILDING)
        on_build_line = self._make_build_progress_updater(engine.display_name, update_progress)
        for i, cmd in enumerate(engine.build_commands):
            log.info(f"[EngineManager] _install_from_source: Running build command {i+1}/{len(engine.build_commands)}: {cmd}")
            log.info(f"[EngineManager] _install_from_source: Build timeout: {engine.build_timeout}s")
            try:
                returncode, tail = self._run_build_command(
                    cmd, repo_dir, engine.build_timeout, on_build_line
                )
            except subprocess.TimeoutExpired:
                self._install_error = f"Build timed out after {engine.build_timeout}s"
                log.error(f"[EngineManager] _install_from_source: Build command timed out after {engine.build_timeout}s: {cmd}")
                return False
            if returncode != 0:
                # The merged tail holds the real cause (e.g. the compiler's
                # "signal: killed" OOM line); surface its end in the UI message.
                detail = tail.strip()[-160:] or f"exit code {returncode}"
                self._install_error = f"Build failed: {detail}"
                log.error(f"[EngineManager] _install_from_source: Build command failed ({returncode}): {cmd}")
                log.error(f"[EngineManager] _install_from_source: Build output (last {_BUILD_TAIL_LINES} lines):\n{tail[-1000:]}")
                return False
            log.debug(f"[EngineManager] _install_from_source: Build command {i+1} completed successfully")
        
        log.info(f"[EngineManager] _install_from_source: All build commands completed successfully")
        
        # Ensure engines directory exists
        if not self.engines_dir.exists():
            log.info(f"[EngineManager] _install_from_source: Creating engines directory {self.engines_dir}")
            self.engines_dir.mkdir(parents=True, exist_ok=True)
        
        # For engines with repo_url=None (custom build scripts), the script handles installation
        # Check if the binary already exists in the expected final location
        if engine.repo_url is None:
            # Custom build script installs directly to engines_dir/engine.name/
            dst_dir = self.engines_dir / engine.name
            dst_binary = dst_dir / engine.binary_path
            if dst_binary.exists() and os.access(dst_binary, os.X_OK):
                log.info(f"[EngineManager] _install_from_source: Custom script installed binary to {dst_binary}")
                update_progress(f"Verifying {engine.display_name} installation...", InstallStage.INSTALLING_FILES)
                return True
            else:
                log.error(f"[EngineManager] _install_from_source: Custom script did not produce binary at {dst_binary}")
                self._install_error = f"Binary not found after build: {dst_binary}"
                return False
        
        # Copy binary to engines directory
        update_progress(f"Installing {engine.display_name}...", InstallStage.INSTALLING_FILES)
        src_binary = repo_dir / engine.binary_path
        log.debug(f"[EngineManager] _install_from_source: Looking for binary at {src_binary}")
        
        if not src_binary.exists():
            # Try to find the binary
            log.warning(f"[EngineManager] _install_from_source: Binary not found at expected path {src_binary}")
            log.info(f"[EngineManager] _install_from_source: Searching for binary named '{engine.name}' in repo")
            possible_paths = list(repo_dir.glob(f"**/{engine.name}"))
            log.debug(f"[EngineManager] _install_from_source: Found {len(possible_paths)} potential matches: {possible_paths}")
            
            if possible_paths:
                src_binary = possible_paths[0]
                log.info(f"[EngineManager] _install_from_source: Using found binary at {src_binary}")
            else:
                # List directory contents for debugging
                log.error(f"[EngineManager] _install_from_source: Binary not found anywhere in repo")
                try:
                    all_files = list(repo_dir.rglob("*"))
                    executables = [f for f in all_files if f.is_file() and os.access(f, os.X_OK)]
                    log.error(f"[EngineManager] _install_from_source: Executable files in repo: {executables[:20]}")
                except Exception as e:
                    log.error(f"[EngineManager] _install_from_source: Could not list repo files: {e}")
                
                self._install_error = f"Binary not found: {engine.binary_path}"
                return False
        
        dst_binary = self.engines_dir / engine.name
        log.info(f"[EngineManager] _install_from_source: Copying binary {src_binary} -> {dst_binary}")
        shutil.copy2(src_binary, dst_binary)
        os.chmod(dst_binary, 0o755)
        log.info(f"[EngineManager] _install_from_source: Binary installed and made executable")
        
        # Copy extra files (personalities, books, weights, NNUE networks, etc.).
        # Shared glob-aware helper so version-specific names (e.g. Arasan's *.nnue)
        # resolve without hardcoding, matching the prebuilt path exactly.
        if engine.extra_files:
            log.info(f"[EngineManager] _install_from_source: Copying {len(engine.extra_files)} extra files/directories")
            self._copy_extra_files(repo_dir, engine.extra_files)
        
        # Set ownership
        log.debug(f"[EngineManager] _install_from_source: Setting ownership to pi:pi on {self.engines_dir}")
        result = subprocess.run(
            ["sudo", "chown", "-R", "pi:pi", str(self.engines_dir)],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            log.warning(f"[EngineManager] _install_from_source: chown failed ({result.returncode})")
        
        update_progress(f"{engine.display_name} installed successfully", InstallStage.INSTALLING_FILES)
        log.info(f"[EngineManager] _install_from_source: Successfully installed '{engine.name}'")
        return True
    
    def uninstall_engine(self, engine_name: str) -> bool:
        """Uninstall an engine.
        
        Args:
            engine_name: Name of the engine to uninstall
            
        Returns:
            True if uninstallation succeeded
        """
        log.info(f"[EngineManager] uninstall_engine: Starting uninstallation of '{engine_name}'")
        
        if engine_name not in ENGINES:
            log.error(f"[EngineManager] uninstall_engine: Unknown engine '{engine_name}' - not in ENGINES dict")
            return False
        
        engine = ENGINES[engine_name]
        
        if not engine.can_uninstall:
            log.warning(f"[EngineManager] uninstall_engine: Engine '{engine_name}' cannot be uninstalled (can_uninstall=False)")
            return False
        
        if engine.is_system_package:
            # Don't uninstall system packages - just remove symlink
            log.info(f"[EngineManager] uninstall_engine: '{engine_name}' is system package, only removing symlink")
            link_path = self.engines_dir / engine.name
            if link_path.is_symlink():
                link_path.unlink()
                log.info(f"[EngineManager] uninstall_engine: Removed symlink {link_path}")
            elif link_path.exists():
                log.warning(f"[EngineManager] uninstall_engine: {link_path} exists but is not a symlink")
            else:
                log.debug(f"[EngineManager] uninstall_engine: No symlink found at {link_path}")
            log_event("engine_uninstall", f"Uninstalled {engine.display_name}", level="info")
            return True
        
        # Remove binary
        binary_path = self.engines_dir / engine.name
        if binary_path.exists():
            try:
                binary_path.unlink()
                log.info(f"[EngineManager] uninstall_engine: Removed binary {binary_path}")
            except OSError as e:
                log.error(f"[EngineManager] uninstall_engine: Failed to remove binary {binary_path}: {e}")
        else:
            log.debug(f"[EngineManager] uninstall_engine: Binary not found at {binary_path}")
        
        # Remove extra files. Resolved with the same glob-aware helper as install so
        # version-specific names (e.g. *.nnue) are matched in engines_dir.
        for extra_path in expand_extra_files(self.engines_dir, engine.extra_files):
            try:
                if extra_path.is_dir():
                    shutil.rmtree(extra_path)
                    log.info(f"[EngineManager] uninstall_engine: Removed directory {extra_path}")
                else:
                    extra_path.unlink()
                    log.info(f"[EngineManager] uninstall_engine: Removed file {extra_path}")
            except OSError as e:
                log.error(f"[EngineManager] uninstall_engine: Failed to remove {extra_path}: {e}")
        
        # Clean build directory
        build_dir = self.build_tmp / engine.name
        if build_dir.exists():
            try:
                shutil.rmtree(build_dir)
                log.info(f"[EngineManager] uninstall_engine: Cleaned build directory {build_dir}")
            except OSError as e:
                log.warning(f"[EngineManager] uninstall_engine: Failed to clean build directory {build_dir}: {e}")
        else:
            log.debug(f"[EngineManager] uninstall_engine: No build directory at {build_dir}")
        
        # Clear the current installed ref but keep the working-ref history: an
        # uninstall does not erase the fact that those refs once built here.
        self._record_store.record_uninstall(engine_name)

        log.info(f"[EngineManager] uninstall_engine: Successfully uninstalled '{engine_name}'")
        log_event("engine_uninstall", f"Uninstalled {engine.display_name}", level="info")
        return True
    
    def install_async(
        self,
        engine_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        completion_callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """Install an engine asynchronously.
        
        Args:
            engine_name: Name of the engine to install
            progress_callback: Called with progress messages
            completion_callback: Called with success status when done
        """
        log.info(f"[EngineManager] install_async: Starting async installation of '{engine_name}'")
        
        if self.is_installing():
            log.warning(f"[EngineManager] install_async: Another installation is already in progress "
                       f"(installing: {self._installing_engine})")
            if completion_callback:
                completion_callback(False)
            return
        
        def _install_thread():
            log.debug(f"[EngineManager] install_async: Install thread started for '{engine_name}'")
            try:
                success = self.install_engine(engine_name, progress_callback)
                log.info(f"[EngineManager] install_async: Install thread completed for '{engine_name}', success={success}")
                if completion_callback:
                    completion_callback(success)
            except Exception as e:
                log.error(f"[EngineManager] install_async: Install thread crashed for '{engine_name}': {type(e).__name__}: {e}")
                import traceback
                log.error(f"[EngineManager] install_async: Traceback:\n{traceback.format_exc()}")
                self._install_error = str(e)
                if completion_callback:
                    completion_callback(False)
        
        self._install_thread = threading.Thread(
            target=_install_thread,
            name=f"install-{engine_name}",
            daemon=True
        )
        self._install_thread.start()
        log.debug(f"[EngineManager] install_async: Install thread spawned for '{engine_name}'")
    
    def is_installing(self) -> bool:
        """Check if an installation is in progress."""
        is_running = self._install_thread is not None and self._install_thread.is_alive()
        return is_running
    
    def get_installing_engine(self) -> Optional[str]:
        """Get the name of the engine currently being installed, if any."""
        if self.is_installing():
            return self._installing_engine
        return None
    
    def get_install_progress(self) -> str:
        """Get the current installation progress message."""
        return self._install_progress
    
    def get_install_error(self) -> Optional[str]:
        """Get the last installation error, if any."""
        return self._install_error
    
    # =========================================================================
    # Install Queue Methods
    # =========================================================================
    
    def add_progress_listener(self, callback: Callable[[str, str, str], None]) -> None:
        """Add a listener for install progress events.
        
        Args:
            callback: Function called with (engine_name, status, message)
                      status is one of: "queued", "installing", "completed", "failed", "cancelled"
        """
        self._progress_callbacks.append(callback)
        log.debug(f"[EngineManager] Added progress listener, total: {len(self._progress_callbacks)}")
    
    def remove_progress_listener(self, callback: Callable[[str, str, str], None]) -> None:
        """Remove a progress listener."""
        if callback in self._progress_callbacks:
            self._progress_callbacks.remove(callback)
            log.debug(f"[EngineManager] Removed progress listener, remaining: {len(self._progress_callbacks)}")
    
    def _notify_progress(self, engine_name: str, status: str, message: str) -> None:
        """Notify all listeners of progress."""
        for callback in self._progress_callbacks:
            try:
                callback(engine_name, status, message)
            except Exception as e:
                log.error(f"[EngineManager] Progress callback error: {e}")
    
    def queue_engine(self, engine_name: str) -> bool:
        """Add an engine to the install queue.
        
        Args:
            engine_name: Name of the engine to queue
            
        Returns:
            True if engine was queued (False if already queued/installing or unknown)
        """
        if engine_name not in ENGINES:
            log.warning(f"[EngineManager] queue_engine: Unknown engine '{engine_name}'")
            return False
        
        if self.is_installed(engine_name):
            log.info(f"[EngineManager] queue_engine: '{engine_name}' already installed")
            return False
        
        with self._queue_lock:
            # Check if already in queue
            for item in self._queue:
                if item.name == engine_name and item.status in (InstallStatus.QUEUED, InstallStatus.INSTALLING):
                    log.info(f"[EngineManager] queue_engine: '{engine_name}' already in queue")
                    return False
            
            # Add to queue
            queued = QueuedEngine(name=engine_name)
            self._queue.append(queued)
            log.info(f"[EngineManager] queue_engine: Added '{engine_name}' to queue (position {len(self._queue)})")
        
        self._notify_progress(engine_name, "queued", f"Queued for installation")
        
        # Start queue worker if not running
        self._start_queue_worker()
        
        return True
    
    def queue_engines(self, engine_names: List[str]) -> int:
        """Add multiple engines to the install queue.
        
        Args:
            engine_names: List of engine names to queue
            
        Returns:
            Number of engines successfully queued
        """
        count = 0
        for name in engine_names:
            if self.queue_engine(name):
                count += 1
        log.info(f"[EngineManager] queue_engines: Queued {count}/{len(engine_names)} engines")
        return count
    
    def queue_recommended(self) -> int:
        """Queue recommended engines for a fresh install.
        
        Queues a balanced set of engines covering different strengths and styles.
        
        Returns:
            Number of engines queued
        """
        # Recommended set: one top-tier, one specialty, one lightweight
        recommended = ["berserk", "rodentIV", "ct800", "zahak"]
        log.info(f"[EngineManager] queue_recommended: Queueing recommended engines: {recommended}")
        return self.queue_engines(recommended)
    
    def cancel_queued(self, engine_name: str) -> bool:
        """Cancel a queued (not yet installing) engine.
        
        Args:
            engine_name: Engine to cancel
            
        Returns:
            True if cancelled (False if not found or already installing)
        """
        with self._queue_lock:
            for item in self._queue:
                if item.name == engine_name and item.status == InstallStatus.QUEUED:
                    item.status = InstallStatus.CANCELLED
                    log.info(f"[EngineManager] cancel_queued: Cancelled '{engine_name}'")
                    self._notify_progress(engine_name, "cancelled", "Installation cancelled")
                    return True
        return False
    
    def clear_queue(self) -> int:
        """Cancel all queued (not yet installing) engines.
        
        Returns:
            Number of engines cancelled
        """
        count = 0
        with self._queue_lock:
            for item in self._queue:
                if item.status == InstallStatus.QUEUED:
                    item.status = InstallStatus.CANCELLED
                    count += 1
                    self._notify_progress(item.name, "cancelled", "Installation cancelled")
        log.info(f"[EngineManager] clear_queue: Cancelled {count} queued engines")
        return count
    
    def get_queue_status(self) -> List[Dict]:
        """Get the current queue status.
        
        Returns:
            List of dicts with queue item info
        """
        with self._queue_lock:
            return [
                {
                    "name": item.name,
                    "display_name": ENGINES[item.name].display_name if item.name in ENGINES else item.name,
                    "status": item.status.value,
                    "progress": item.progress,
                    "error": item.error,
                    "estimated_minutes": ENGINES[item.name].estimated_install_minutes if item.name in ENGINES else 0,
                }
                for item in self._queue
                if item.status in (InstallStatus.QUEUED, InstallStatus.INSTALLING)
            ]
    
    def get_queue_history(self, limit: int = 10) -> List[Dict]:
        """Get recent completed/failed installations.
        
        Args:
            limit: Maximum number of items to return
            
        Returns:
            List of dicts with completed install info
        """
        with self._queue_lock:
            completed = [
                {
                    "name": item.name,
                    "display_name": ENGINES[item.name].display_name if item.name in ENGINES else item.name,
                    "status": item.status.value,
                    "error": item.error,
                    "duration_seconds": (item.completed_at - item.started_at) if item.started_at and item.completed_at else None,
                }
                for item in self._queue
                if item.status in (InstallStatus.COMPLETED, InstallStatus.FAILED, InstallStatus.CANCELLED)
            ]
            return completed[-limit:]
    
    def is_queue_active(self) -> bool:
        """Check if the queue is actively processing."""
        return self._queue_running and self._queue_worker_thread is not None and self._queue_worker_thread.is_alive()
    
    def _start_queue_worker(self) -> None:
        """Start the queue worker thread if not already running."""
        if self._queue_worker_thread is not None and self._queue_worker_thread.is_alive():
            return
        
        self._queue_running = True
        self._queue_worker_thread = threading.Thread(
            target=self._queue_worker,
            name="engine-install-queue",
            daemon=True
        )
        self._queue_worker_thread.start()
        log.info("[EngineManager] Queue worker thread started")
    
    def _queue_worker(self) -> None:
        """Background worker that processes the install queue."""
        log.info("[EngineManager] Queue worker: Starting")
        
        while self._queue_running:
            # Find next queued item
            next_item: Optional[QueuedEngine] = None
            with self._queue_lock:
                for item in self._queue:
                    if item.status == InstallStatus.QUEUED:
                        next_item = item
                        break
            
            if next_item is None:
                # No more items, exit worker
                log.info("[EngineManager] Queue worker: No more items, exiting")
                break
            
            # Install this engine
            engine_name = next_item.name
            log.info(f"[EngineManager] Queue worker: Processing '{engine_name}'")
            
            with self._queue_lock:
                next_item.status = InstallStatus.INSTALLING
                next_item.started_at = time.time()
            
            self._notify_progress(engine_name, "installing", "Starting installation...")
            
            def progress_callback(msg: str):
                with self._queue_lock:
                    next_item.progress = msg
                self._notify_progress(engine_name, "installing", msg)
            
            try:
                success = self.install_engine(engine_name, progress_callback)
                
                with self._queue_lock:
                    next_item.completed_at = time.time()
                    if success:
                        next_item.status = InstallStatus.COMPLETED
                        log.info(f"[EngineManager] Queue worker: '{engine_name}' completed successfully")
                        self._notify_progress(engine_name, "completed", "Installation complete")
                    else:
                        next_item.status = InstallStatus.FAILED
                        next_item.error = self._install_error
                        log.error(f"[EngineManager] Queue worker: '{engine_name}' failed: {self._install_error}")
                        self._notify_progress(engine_name, "failed", self._install_error or "Installation failed")
            
            except Exception as e:
                log.error(f"[EngineManager] Queue worker: '{engine_name}' exception: {e}")
                with self._queue_lock:
                    next_item.completed_at = time.time()
                    next_item.status = InstallStatus.FAILED
                    next_item.error = str(e)
                self._notify_progress(engine_name, "failed", str(e))
        
        self._queue_running = False
        log.info("[EngineManager] Queue worker: Stopped")


# Module-level singleton
_engine_manager: Optional[EngineManager] = None


def get_engine_manager() -> EngineManager:
    """Get the engine manager singleton."""
    global _engine_manager
    if _engine_manager is None:
        _engine_manager = EngineManager()
    return _engine_manager
