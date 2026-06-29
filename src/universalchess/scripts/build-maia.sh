#!/usr/bin/env bash
# =============================================================================
# Build script for Maia (lc0 with human-like neural network weights)
# Designed for Raspberry Pi ARM64 with limited RAM
#
# This script builds lc0 with BLAS backend for CPU-only operation, using
# single-threaded compilation to keep peak memory low on RAM-constrained boards.
#
# Usage: ./build-maia.sh [install_dir]
#   install_dir: Where to place the built binary (default: /opt/universalchess/engines/maia)
#
# The script will:
#   1. Install build dependencies
#   2. Clone lc0 and configure for ARM with BLAS backend
#   3. Build with -j1 to avoid OOM kills
#   4. Download Maia weights
#   5. Install binary and weights to install_dir
#
# Memory: this script does NOT provision swap. The caller (the app's engine
# installer) brings up a zram + SD-card swap tier via uc-build-memory around the
# whole build. The build directory is placed on disk beside the install dir,
# never under /tmp -- on a Pi /tmp is a small RAM-backed tmpfs that cannot hold
# the lc0 checkout/build tree (the build previously failed there with "No space
# left on device").
#
# Run as root or with sudo for apt operations and writing to the install dir.
# =============================================================================

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOG_PREFIX="[Maia Build]"
LC0_VERSION="v0.32.1"
INSTALL_DIR="${1:-/opt/universalchess/engines/maia}"
# Build on disk, never under /tmp. On a Pi /tmp is a small tmpfs (e.g. 208 MB on
# a 512 MB board) and RAM-backed, so it can hold neither the lc0 checkout+build
# tree nor any useful swap. Place the build beside the install dir on the SD
# card: <install_dir>/../../tmp/maia-build (e.g. /opt/universalchess/tmp).
#
# The path is STABLE (no PID suffix) and the tree is preserved on failure so a
# re-run resumes the incremental ninja build instead of recompiling all ~259
# units from scratch. lc0 takes 30-60 min single-threaded on a Pi Zero 2 W, so a
# near-complete build that is interrupted must not be thrown away.
BUILD_DIR="$(dirname "$(dirname "$INSTALL_DIR")")/tmp/maia-build"

# Maia weights to download
MAIA_WEIGHTS=(
    "maia-1100.pb.gz"
    "maia-1200.pb.gz"
    "maia-1300.pb.gz"
    "maia-1400.pb.gz"
    "maia-1500.pb.gz"
    "maia-1600.pb.gz"
    "maia-1700.pb.gz"
    "maia-1800.pb.gz"
    "maia-1900.pb.gz"
)
MAIA_WEIGHTS_URL="https://github.com/CSSLab/maia-chess/raw/main/maia_weights"

# =============================================================================
# Logging functions
# =============================================================================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $LOG_PREFIX $*"
}

log_step() {
    echo ""
    echo "============================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $LOG_PREFIX STEP: $*"
    echo "============================================================"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $LOG_PREFIX ERROR: $*" >&2
}

log_warn() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $LOG_PREFIX WARNING: $*" >&2
}

# =============================================================================
# Cleanup function
# =============================================================================

cleanup() {
    local exit_code=$?

    log "Cleaning up..."

    if [[ $exit_code -eq 0 ]]; then
        # Success: the binary and weights are now installed, so the build tree is
        # dead weight -- reclaim the disk.
        if [[ -d "$BUILD_DIR" ]]; then
            log "Removing build directory..."
            rm -rf "$BUILD_DIR"
        fi
        log "Build completed successfully!"
    else
        # Failure or interruption: KEEP the build tree. ninja only records a target
        # as done after it completes, so a re-run rebuilds just the interrupted
        # target and the remainder -- the previously compiled units are reused. A
        # fresh restart from 0/259 would otherwise waste the 30-60 min already
        # spent. The tree is removed on the next successful build (above).
        log_error "Build failed with exit code $exit_code"
        log "Build directory kept for resume: $BUILD_DIR"
    fi

    exit $exit_code
}

trap cleanup EXIT

# =============================================================================
# System checks
# =============================================================================

check_architecture() {
    log_step "Checking system architecture"
    
    local arch
    arch=$(uname -m)
    log "Architecture: $arch"
    
    case "$arch" in
        aarch64|arm64)
            log "Detected 64-bit ARM - OK"
            ;;
        armv7l|armhf)
            log "Detected 32-bit ARM - OK (may be slower)"
            ;;
        x86_64)
            log_warn "Detected x86_64 - this script is optimized for ARM"
            log_warn "Consider using official lc0 releases instead"
            ;;
        *)
            log_error "Unsupported architecture: $arch"
            exit 1
            ;;
    esac
}

report_memory() {
    log_step "Checking system memory"

    local total_mem_kb total_swap_kb
    total_mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    total_swap_kb=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
    local total_mem_mb=$((total_mem_kb / 1024))
    local total_swap_mb=$((total_swap_kb / 1024))
    local total_available=$((total_mem_mb + total_swap_mb))

    log "RAM: ${total_mem_mb}MB"
    log "Swap: ${total_swap_mb}MB"
    log "Total available: ${total_available}MB"

    # This script intentionally does NOT add its own swap. The caller provisions a
    # zram + SD-card swap tier via uc-build-memory around the whole build, so the
    # headroom is already in place. The previous self-managed 2 GB swapfile wrote
    # to /tmp (a small RAM-backed tmpfs), which failed with "No space left on
    # device" and could not have added real headroom regardless.
    if [[ $total_available -lt 2048 ]]; then
        log_warn "Only ${total_available}MB RAM+swap visible; lc0 wants ~2GB to build"
        log_warn "with -j1. If the build is OOM-killed, ensure the caller acquired"
        log_warn "build swap (uc-build-memory) before invoking this script."
    else
        log "Memory appears sufficient for build"
    fi
}

# =============================================================================
# Dependencies
# =============================================================================

install_dependencies() {
    log_step "Installing build dependencies"
    
    if ! command -v apt-get &>/dev/null; then
        log_error "apt-get not found. This script requires a Debian-based system."
        exit 1
    fi
    
    log "Updating package lists..."
    apt-get update
    
    log "Installing required packages..."
    apt-get install -y \
        build-essential \
        git \
        clang \
        meson \
        ninja-build \
        pkg-config \
        libopenblas-dev \
        zlib1g-dev \
        wget
    
    log "Dependencies installed successfully"
}

# =============================================================================
# Build lc0
# =============================================================================

clone_lc0() {
    log_step "Cloning lc0 repository"
    
    mkdir -p "$BUILD_DIR"
    cd "$BUILD_DIR"

    # Reuse an existing checkout of the pinned version so a resumed build skips
    # re-cloning (and, more importantly, keeps the compiled build/ tree intact for
    # ninja). Only our own prior clone of LC0_VERSION is trusted; anything else
    # (missing, wrong tag, or a clone interrupted before the tag was fetched) is
    # wiped and re-cloned to avoid building stale or partial source.
    local existing_tag=""
    if [[ -d "lc0/.git" ]]; then
        existing_tag="$(git -C lc0 describe --tags --always 2>/dev/null || true)"
    fi
    if [[ "$existing_tag" == "$LC0_VERSION" ]]; then
        log "Reusing existing lc0 checkout at ${LC0_VERSION}"
        # A previous run may have been interrupted mid-submodule-fetch; make sure
        # submodules are complete before building. This is a no-op when they are.
        git -C lc0 submodule update --init --recursive
    else
        if [[ -d "lc0" ]]; then
            log "Existing checkout is '${existing_tag:-not a git repo}', need ${LC0_VERSION}; removing..."
            rm -rf lc0
        fi
        log "Cloning lc0 ${LC0_VERSION}..."
        git clone --depth 1 --branch "$LC0_VERSION" --recurse-submodules \
            https://github.com/LeelaChessZero/lc0.git
    fi

    cd lc0
    log "Source ready. Working directory: $(pwd)"
}

configure_build() {
    log_step "Configuring meson build"
    
    cd "$BUILD_DIR/lc0"

    # Use clang for better ARM optimization
    export CC=clang
    export CXX=clang++
    log "Compiler: CC=$CC, CXX=$CXX"

    # Build options for ARM with BLAS-only backend (CPU)
    # Disable all GPU backends and x86-specific features
    local meson_opts=(
        "--buildtype=release"
        "-Ddefault_library=static"
        # Enable BLAS backend
        "-Dblas=true"
        "-Dopenblas=true"
        # Disable GPU backends
        "-Dplain_cuda=false"
        "-Dcudnn=false"
        "-Dopencl=false"
        "-Ddx=false"
        "-Donednn=false"
        "-Dmetal=disabled"
        # Disable x86-specific features
        "-Dispc=false"
        "-Dpopcnt=false"
        "-Df16c=false"
        "-Dpext=false"
        # Disable optional features
        "-Dgtest=false"
        "-Donnx=false"
        "-Dpython_bindings=false"
    )

    # Disable LTO on 32-bit ARM.
    #
    # lc0's meson.build pins b_lto=true, so the release build does a whole-program
    # LTO link of lc0 + abseil. On 32-bit (arm-linux-gnueabihf) that link exhausts
    # the process's ~3 GB virtual address-space ceiling and clang's linker dies
    # with a Segmentation fault -- NOT an OOM kill, so swap cannot help (the limit
    # is address space, not RAM). Every translation unit compiles fine; only the
    # final link fails. 64-bit (aarch64) has the address space, so LTO stays on
    # there. abseil inherits the global b_lto, but set it explicitly too so no LTO
    # bitcode survives in its archives to re-trigger LTO codegen at link time.
    if [[ "$(uname -m)" != "aarch64" ]]; then
        log "32-bit ARM ($(uname -m)) detected; disabling LTO to avoid linker segfault"
        meson_opts+=("-Db_lto=false" "-Dabseil-cpp:b_lto=false")
    fi

    log "Meson options:"
    for opt in "${meson_opts[@]}"; do
        log "  $opt"
    done

    # Apply options with --reconfigure when a build dir already exists so a resumed
    # build picks up any option change (e.g. the LTO toggle above) instead of
    # silently keeping the old configuration. For unchanged options --reconfigure
    # only regenerates build.ninja; ninja then sees identical commands and keeps
    # every already-compiled object, so a plain resume loses no progress. (An LTO
    # change does invalidate all objects -- that is correct, the flag affects every
    # compile -- but the build remains resumable from then on.)
    if [[ -f "build/release/build.ninja" ]]; then
        log "Reconfiguring existing meson build (applies current options, keeps unaffected objects)"
        meson setup --reconfigure build/release "${meson_opts[@]}"
    else
        log "Running meson setup..."
        meson setup build/release "${meson_opts[@]}"
    fi
    
    log "Configuration complete"
}

build_lc0() {
    log_step "Building lc0 (this will take 30-60 minutes)"
    
    cd "$BUILD_DIR/lc0"

    # Cumulative progress across resumes.
    #
    # ninja's "[current/total]" is per-invocation: "total" is the number of edges
    # it still needs to build *this run*, and "current" restarts at 0. So a resume
    # that already compiled 14 of 259 units reports "[x/245]" starting at 0/245 --
    # correct work, but it looks like a restart. Persist the full total (captured
    # on the first clean build, where remaining == full) so resumes can render the
    # cumulative count: already_done = full_total - remaining_this_run, displayed
    # as [already_done + current / full_total]. The file lives with the build tree,
    # so it is created once, reused by every resume, and removed on success when
    # the tree is cleaned.
    local total_file="$BUILD_DIR/lc0/.uc-build-total"
    local full_total=""
    if [[ -f "$total_file" ]]; then
        full_total="$(cat "$total_file" 2>/dev/null || true)"
    fi

    # Build with -j1 to minimize memory usage
    # The Pi has limited RAM and lc0+abseil compilation is memory-intensive
    log "Starting build with -j1 (single-threaded to avoid OOM)..."
    log "Progress will be shown as [completed/total], continuing across resumes"
    
    # Run ninja with verbose output to show progress
    if ! ninja -C build/release -j1 -v 2>&1 | while IFS= read -r line; do
        # Extract progress from ninja output like [123/456]
        if [[ "$line" =~ ^\[([0-9]+)/([0-9]+)\] ]]; then
            local current="${BASH_REMATCH[1]}"
            local remaining="${BASH_REMATCH[2]}"
            # First clean build (no persisted total): remaining == the full total.
            # Capture and persist it for future resumes. Pre-loading from the file
            # above prevents a resume from mistaking its smaller "remaining" for the
            # full total.
            if [[ -z "$full_total" ]]; then
                full_total="$remaining"
                echo "$full_total" > "$total_file"
            fi
            local already=$((full_total - remaining))
            if ((already < 0)); then already=0; fi
            local completed=$((already + current))
            if ((completed > full_total)); then completed=$full_total; fi
            local percent=$((completed * 100 / full_total))
            echo -ne "\r$LOG_PREFIX Progress: [$completed/$full_total] ($percent%)     "
        fi
    done; then
        echo ""
        log_error "Build failed!"
        return 1
    fi
    
    echo ""
    log "Build completed!"
    
    # Verify binary was created
    if [[ ! -f "build/release/lc0" ]]; then
        log_error "lc0 binary not found after build"
        return 1
    fi
    
    log "Binary created: build/release/lc0"
    file build/release/lc0
    ls -lh build/release/lc0
}

# =============================================================================
# Install
# =============================================================================

install_binary() {
    log_step "Installing lc0 binary"
    
    mkdir -p "$INSTALL_DIR"
    
    log "Copying binary to $INSTALL_DIR/lc0..."
    cp "$BUILD_DIR/lc0/build/release/lc0" "$INSTALL_DIR/lc0"
    chmod +x "$INSTALL_DIR/lc0"
    
    log "Stripping binary..."
    strip "$INSTALL_DIR/lc0" 2>/dev/null || true
    
    log "Installed binary:"
    ls -lh "$INSTALL_DIR/lc0"
    
    # Test the binary
    log "Testing binary..."
    if "$INSTALL_DIR/lc0" --help &>/dev/null; then
        log "Binary test: OK"
    else
        log_warn "Binary test failed - may need Maia weights to run"
    fi
}

download_weights() {
    log_step "Downloading Maia neural network weights"
    
    local weights_dir="$INSTALL_DIR/maia_weights"
    mkdir -p "$weights_dir"
    
    local count=0
    local total=${#MAIA_WEIGHTS[@]}
    
    for weight in "${MAIA_WEIGHTS[@]}"; do
        count=$((count + 1))
        local url="$MAIA_WEIGHTS_URL/$weight"
        local dest="$weights_dir/$weight"
        
        if [[ -f "$dest" ]]; then
            log "[$count/$total] $weight - already exists, skipping"
        else
            log "[$count/$total] Downloading $weight..."
            if wget -q --show-progress -O "$dest" "$url"; then
                log "[$count/$total] $weight - OK"
            else
                log_warn "[$count/$total] $weight - FAILED (continuing)"
                rm -f "$dest"
            fi
        fi
    done
    
    log "Weights downloaded to $weights_dir"
    ls -lh "$weights_dir"
    
    # Download Leela weights (stronger networks)
    log_step "Downloading Leela neural network weights"
    
    local leela_dir="$INSTALL_DIR/leela_weights"
    mkdir -p "$leela_dir"
    
    # T1-256x10 - Small/fast network optimized for CPU/low-power devices (~25MB)
    local t1_dest="$leela_dir/t1-256x10.pb.gz"
    if [[ -f "$t1_dest" ]]; then
        log "t1-256x10.pb.gz - already exists, skipping"
    else
        log "Downloading T1-256x10 (small/fast, ~25MB)..."
        if wget -q --show-progress -O "$t1_dest" \
            "https://training.lczero.org/get_network?sha=00af53b081e80147172e6f281c01571016924e9aac89cdf6666a1cc3a4ecf5bf"; then
            log "t1-256x10.pb.gz - OK"
        else
            log_warn "t1-256x10.pb.gz - FAILED (continuing)"
            rm -f "$t1_dest"
        fi
    fi
    
    log "Leela weights downloaded to $leela_dir"
    ls -lh "$leela_dir" 2>/dev/null || true
}

# =============================================================================
# Main
# =============================================================================

show_summary() {
    log_step "Installation Summary"
    
    echo ""
    echo "Maia (lc0) has been installed successfully!"
    echo ""
    echo "Binary:  $INSTALL_DIR/lc0"
    echo ""
    echo "=== Maia Weights (human-like play) ==="
    echo "Location: $INSTALL_DIR/maia_weights/"
    for weight in "${MAIA_WEIGHTS[@]}"; do
        if [[ -f "$INSTALL_DIR/maia_weights/$weight" ]]; then
            local level
            level=$(echo "$weight" | sed 's/maia-\([0-9]*\).*/\1/')
            echo "  - ELO $level: maia_weights/$weight"
        fi
    done
    echo ""
    echo "=== Leela Weights (maximum strength) ==="
    echo "Location: $INSTALL_DIR/leela_weights/"
    if [[ -f "$INSTALL_DIR/leela_weights/t1-256x10.pb.gz" ]]; then
        echo "  - T1-256x10 (small/fast): leela_weights/t1-256x10.pb.gz"
    fi
    echo ""
    echo "Usage examples:"
    echo "  # Human-like play at 1500 ELO:"
    echo "  $INSTALL_DIR/lc0 --weights=$INSTALL_DIR/maia_weights/maia-1500.pb.gz"
    echo ""
    echo "  # Maximum strength (fast network for Pi):"
    echo "  $INSTALL_DIR/lc0 --weights=$INSTALL_DIR/leela_weights/t1-256x10.pb.gz"
    echo ""
}

main() {
    log "=========================================="
    log "Maia (lc0) Build Script for Raspberry Pi"
    log "=========================================="
    log "Install directory: $INSTALL_DIR"
    log "Build directory: $BUILD_DIR"
    log "lc0 version: $LC0_VERSION"
    
    check_architecture
    report_memory
    install_dependencies
    clone_lc0
    configure_build
    build_lc0
    install_binary
    download_weights
    show_summary
}

main "$@"

