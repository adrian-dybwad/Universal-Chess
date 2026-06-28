#!/usr/bin/env bash
# =============================================================================
# Build script for Maia (lc0 with human-like neural network weights)
# Designed for Raspberry Pi ARM64 with limited RAM
#
# This script builds lc0 with BLAS backend for CPU-only operation.
# It handles memory constraints by using single-threaded compilation
# and adding swap space if needed.
#
# Usage: ./build-maia.sh [install_dir]
#   install_dir: Where to place the built binary (default: /opt/universalchess/engines/maia)
#
# The script will:
#   1. Install build dependencies
#   2. Add swap if system has < 4GB total memory
#   3. Clone lc0 and configure for ARM with BLAS backend
#   4. Build with -j1 to avoid OOM kills
#   5. Download Maia weights
#   6. Install binary and weights to install_dir
#
# Run as root or with sudo for swap and apt operations.
# =============================================================================

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOG_PREFIX="[Maia Build]"
LC0_VERSION="v0.32.1"
INSTALL_DIR="${1:-/opt/universalchess/engines/maia}"
BUILD_DIR="/tmp/maia-build-$$"
SWAP_FILE="/tmp/maia-build-swap"
SWAP_SIZE_MB=2048

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
    
    # Remove swap if we created it
    if [[ -f "$SWAP_FILE" ]]; then
        log "Removing temporary swap file..."
        swapoff "$SWAP_FILE" 2>/dev/null || true
        rm -f "$SWAP_FILE"
    fi
    
    # Remove build directory
    if [[ -d "$BUILD_DIR" ]]; then
        log "Removing build directory..."
        rm -rf "$BUILD_DIR"
    fi
    
    if [[ $exit_code -eq 0 ]]; then
        log "Build completed successfully!"
    else
        log_error "Build failed with exit code $exit_code"
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

check_memory() {
    log_step "Checking system memory"
    
    local total_mem_kb
    total_mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    local total_mem_mb=$((total_mem_kb / 1024))
    local total_swap_kb
    total_swap_kb=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
    local total_swap_mb=$((total_swap_kb / 1024))
    local total_available=$((total_mem_mb + total_swap_mb))
    
    log "RAM: ${total_mem_mb}MB"
    log "Swap: ${total_swap_mb}MB"
    log "Total available: ${total_available}MB"
    
    # lc0 compilation needs at least 2GB to compile safely with -j1
    if [[ $total_available -lt 2048 ]]; then
        log_warn "Less than 2GB total memory available"
        log "Adding ${SWAP_SIZE_MB}MB temporary swap file..."
        add_swap
    else
        log "Memory appears sufficient for build"
    fi
}

add_swap() {
    if [[ -f "$SWAP_FILE" ]]; then
        log "Swap file already exists, removing old one..."
        swapoff "$SWAP_FILE" 2>/dev/null || true
        rm -f "$SWAP_FILE"
    fi
    
    log "Creating ${SWAP_SIZE_MB}MB swap file at $SWAP_FILE..."
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$SWAP_SIZE_MB status=progress
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
    swapon "$SWAP_FILE"
    
    local new_swap_kb
    new_swap_kb=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
    local new_swap_mb=$((new_swap_kb / 1024))
    log "Swap now: ${new_swap_mb}MB"
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
    
    if [[ -d "lc0" ]]; then
        log "lc0 directory exists, removing..."
        rm -rf lc0
    fi
    
    log "Cloning lc0 ${LC0_VERSION}..."
    git clone --depth 1 --branch "$LC0_VERSION" --recurse-submodules \
        https://github.com/LeelaChessZero/lc0.git
    
    cd lc0
    log "Clone complete. Working directory: $(pwd)"
}

configure_build() {
    log_step "Configuring meson build"
    
    cd "$BUILD_DIR/lc0"
    
    # Remove any existing build directory to ensure clean configuration
    if [[ -d "build/release" ]]; then
        log "Removing existing build directory..."
        rm -rf build/release
    fi
    
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
        "-Dnvcc=false"
        "-Dpython_bindings=false"
    )
    
    log "Meson options:"
    for opt in "${meson_opts[@]}"; do
        log "  $opt"
    done
    
    log "Running meson setup..."
    meson setup build/release "${meson_opts[@]}"
    
    log "Configuration complete"
}

build_lc0() {
    log_step "Building lc0 (this will take 30-60 minutes)"
    
    cd "$BUILD_DIR/lc0"
    
    # Get number of compilation units
    local total_units
    total_units=$(ninja -C build/release -t targets all 2>/dev/null | wc -l || echo "unknown")
    log "Total compilation units: ~$total_units"
    
    # Build with -j1 to minimize memory usage
    # The Pi has limited RAM and lc0+abseil compilation is memory-intensive
    log "Starting build with -j1 (single-threaded to avoid OOM)..."
    log "Progress will be shown as [current/total]"
    
    # Run ninja with verbose output to show progress
    if ! ninja -C build/release -j1 -v 2>&1 | while IFS= read -r line; do
        # Extract progress from ninja output like [123/456]
        if [[ "$line" =~ ^\[([0-9]+)/([0-9]+)\] ]]; then
            local current="${BASH_REMATCH[1]}"
            local total="${BASH_REMATCH[2]}"
            local percent=$((current * 100 / total))
            echo -ne "\r$LOG_PREFIX Progress: [$current/$total] ($percent%)     "
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
    check_memory
    install_dependencies
    clone_lc0
    configure_build
    build_lc0
    install_binary
    download_weights
    show_summary
}

main "$@"

