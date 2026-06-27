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
import subprocess
import shutil
import threading
import platform
import tarfile
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict, FrozenSet
from pathlib import Path
from queue import Queue
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

# Engine installation directory
ENGINES_DIR = "/opt/universalchess/engines"
BUILD_TMP = "/opt/universalchess/tmp/engine_build"

# Repository root (for build scripts)
# Detect from this file's location: src/universalchess/managers/engine_manager.py -> repo root
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)

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
            # Use -j2 to limit memory usage (NNUE compilation is memory-intensive)
            "cd src_files && make -j2 EXE=koivisto",
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
            # Use -j2 to limit memory usage (NNUE compilation is memory-intensive).
            "cd src && make -j2 CC=gcc EXE=ethereal",
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
            # Demolito builds from src directory where makefile is located
            # The makefile uses clang by default
            "cd src && make -j$(nproc)",
        ],
        binary_path="src/demolito",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git", "clang"],  # Makefile uses clang
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
            "cd src && make -j$(nproc) EXE=weiss",
        ],
        binary_path="src/weiss",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git"],
        estimated_install_minutes=5,  # Clean C engine
        has_prebuilt=True,
    ),
    "arasan": EngineDefinition(
        name="arasan",
        display_name="Arasan",
        summary="~2900 ELO, veteran",
        description="Veteran engine in development since 1994. Very stable and reliable. NNUE support added recently. Great for consistent, predictable play.",
        repo_url="https://github.com/jdart1/arasan-chess.git",
        build_commands=[
            # Arasan's Makefile builds into ../bin (EXPORT=../bin), NOT src/, and
            # names the binary arasanx-<bits> (e.g. arasanx-64) unless EXE is given.
            # Pass EXE=arasan so the output is a fixed ../bin/arasan that matches
            # binary_path below -- without this the post-build "binary not found:
            # src/arasan" check fails even though compilation succeeded.
            #
            # SIMD: the Makefile enables NEON only with BUILD_TYPE=neon, and it
            # defines the NEON flags solely for the arm64/aarch64 arch tokens (its
            # arch switch has no armv7l branch). Request NEON on 64-bit ARM for the
            # vectorized NNUE path; on 32-bit ARM there is no NEON build, so the
            # default scalar NNUE fallback (nnue/*.h #ifndef SIMD) is used -- correct,
            # just slower. The bundled network (network/arasanv8-*.nnue) ships in the
            # repo, so no network fetch is needed at build time.
            #
            # -j1 to avoid OOM on low-memory devices (NNUE + LTO is memory-intensive).
            'cd src && ARASAN_ARGS="EXE=arasan"; '
            'case "$(uname -m)" in aarch64|arm64) '
            'ARASAN_ARGS="$ARASAN_ARGS BUILD_TYPE=neon";; esac; '
            'make -j1 $ARASAN_ARGS',
        ],
        binary_path="bin/arasan",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=["build-essential", "git", "bc", "gawk"],
        build_timeout=1800,
        estimated_install_minutes=25,  # NNUE engine, single-threaded build
        has_prebuilt=True,
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
            'make -j$(nproc) EXENAME=../rodentIV LDFLAGS="$LDFLAGS"',
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
            f"sudo {REPO_ROOT}/scripts/engines/build-maia.sh {ENGINES_DIR}/maia",
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
        build_commands=[
            "go build -o zahak",
        ],
        binary_path="zahak",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        # golang package name varies: 'golang' on older Debian, 'golang-go' on newer
        dependencies=["golang", "git"],
        estimated_install_minutes=5,  # Go compiles quickly
        has_prebuilt=True,
    ),
    "smallbrain": EngineDefinition(
        name="smallbrain",
        display_name="Smallbrain",
        summary="~3000 ELO, compact",
        description="Compact NNUE engine with small binary size. Efficient code optimized for resource-constrained devices. Surprisingly strong for its size.",
        repo_url="https://github.com/Disservin/Smallbrain.git",
        build_commands=[
            # Use -j2 to limit memory usage (NNUE compilation is memory-intensive)
            "cd src && make -j2 EXE=smallbrain",
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
    
    def __init__(self, engines_dir: str = ENGINES_DIR):
        """Initialize the engine manager.
        
        Args:
            engines_dir: Directory where engines are installed
        """
        self.engines_dir = Path(engines_dir)
        self.build_tmp = Path(BUILD_TMP)
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
    
    def install_engine(
        self,
        engine_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        stage_callback: Optional[Callable[[InstallStage, str, Optional[float]], None]] = None
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
        
        try:
            if engine.is_system_package:
                log.info(f"[EngineManager] install_engine: Using system package installation for '{engine_name}'")
                success = self._install_system_package(engine, update_progress)
            elif engine.has_prebuilt and self._try_install_prebuilt(engine, update_progress):
                # Pre-built binary downloaded and installed successfully
                log.info(f"[EngineManager] install_engine: Installed pre-built binary for '{engine_name}'")
                success = True
            else:
                log.info(f"[EngineManager] install_engine: Using source build installation for '{engine_name}'")
                success = self._install_from_source(engine, update_progress)
            
            if success:
                log.info(f"[EngineManager] install_engine: Successfully installed '{engine_name}'")
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
    
    def _install_from_source(
        self,
        engine: EngineDefinition,
        update_progress: Callable[..., None]
    ) -> bool:
        """Install engine by building from source.
        
        Args:
            engine: Engine definition
            update_progress: Callback for progress messages (msg, stage, fraction)
            
        Returns:
            True if installation succeeded
        """
        log.info(f"[EngineManager] _install_from_source: Starting source build for '{engine.name}'")
        log.info(f"[EngineManager] _install_from_source: Repo URL: {engine.repo_url}")
        log.info(f"[EngineManager] _install_from_source: Build commands: {engine.build_commands}")
        log.info(f"[EngineManager] _install_from_source: Binary path: {engine.binary_path}")
        
        # Ensure build directory exists
        log.debug(f"[EngineManager] _install_from_source: Creating build temp directory {self.build_tmp}")
        self.build_tmp.mkdir(parents=True, exist_ok=True)
        repo_dir = self.build_tmp / engine.name
        log.debug(f"[EngineManager] _install_from_source: Repo directory: {repo_dir}")
        
        # Install build dependencies
        if engine.dependencies:
            update_progress(f"Installing build dependencies...", InstallStage.INSTALLING_DEPS)
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
        else:
            log.debug(f"[EngineManager] _install_from_source: No dependencies to install")
        
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
            
            # Build clone command - use submodules if needed
            clone_cmd = ["git", "clone"]
            if engine.clone_with_submodules:
                clone_cmd.extend(["--recurse-submodules"])
            else:
                clone_cmd.extend(["--depth", "1"])
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
        
        # Build
        update_progress(f"Building {engine.display_name}...", InstallStage.BUILDING)
        for i, cmd in enumerate(engine.build_commands):
            log.info(f"[EngineManager] _install_from_source: Running build command {i+1}/{len(engine.build_commands)}: {cmd}")
            log.info(f"[EngineManager] _install_from_source: Build timeout: {engine.build_timeout}s")
            result = subprocess.run(
                cmd,
                shell=True, cwd=repo_dir,
                capture_output=True, text=True, timeout=engine.build_timeout
            )
            if result.returncode != 0:
                self._install_error = f"Build failed: {result.stderr.strip()[:100]}"
                log.error(f"[EngineManager] _install_from_source: Build command failed ({result.returncode}): {cmd}")
                log.error(f"[EngineManager] _install_from_source: Build stdout (last 500 chars): {result.stdout.strip()[-500:]}")
                log.error(f"[EngineManager] _install_from_source: Build stderr (last 500 chars): {result.stderr.strip()[-500:]}")
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
        
        # Copy extra files (personalities, books, weights, etc.)
        if engine.extra_files:
            log.info(f"[EngineManager] _install_from_source: Copying {len(engine.extra_files)} extra files/directories")
        for extra in engine.extra_files:
            src_extra = repo_dir / extra
            if src_extra.exists():
                dst_extra = self.engines_dir / extra
                log.debug(f"[EngineManager] _install_from_source: Copying extra '{extra}': {src_extra} -> {dst_extra}")
                if src_extra.is_dir():
                    if dst_extra.exists():
                        log.debug(f"[EngineManager] _install_from_source: Removing existing directory {dst_extra}")
                        shutil.rmtree(dst_extra)
                    shutil.copytree(src_extra, dst_extra)
                    log.debug(f"[EngineManager] _install_from_source: Copied directory {extra}")
                else:
                    shutil.copy2(src_extra, dst_extra)
                    log.debug(f"[EngineManager] _install_from_source: Copied file {extra}")
            else:
                log.warning(f"[EngineManager] _install_from_source: Extra file/dir not found: {src_extra}")
        
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
        
        # Remove extra files
        for extra in engine.extra_files:
            extra_path = self.engines_dir / extra
            if extra_path.exists():
                try:
                    if extra_path.is_dir():
                        shutil.rmtree(extra_path)
                        log.info(f"[EngineManager] uninstall_engine: Removed directory {extra_path}")
                    else:
                        extra_path.unlink()
                        log.info(f"[EngineManager] uninstall_engine: Removed file {extra_path}")
                except OSError as e:
                    log.error(f"[EngineManager] uninstall_engine: Failed to remove {extra_path}: {e}")
            else:
                log.debug(f"[EngineManager] uninstall_engine: Extra file/dir not found: {extra_path}")
        
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
        
        log.info(f"[EngineManager] uninstall_engine: Successfully uninstalled '{engine_name}'")
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
