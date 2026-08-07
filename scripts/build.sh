#!/usr/bin/env bash
set -euo pipefail

# Build a .deb from the repo's Debian staging root `packaging/deb-root/`.
#
# This script is intentionally repo-relative and does not assume a particular
# checkout folder name.
#
# Usage:
#   ./build.sh              - Build package (standard)
#   ./build.sh --with-lc0   - Build package with lc0/Maia engine (takes ~30 min on Pi)
#   ./build.sh clean        - Clean build artifacts

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Parse arguments
BUILD_LC0=false
for arg in "$@"; do
    case "$arg" in
        --with-lc0)
            BUILD_LC0=true
            ;;
    esac
done

# Use /var/tmp for large build artifacts (disk-backed, not tiny RAM /tmp)
BUILD_TMP="/var/tmp"
RELEASES_DIR="${SCRIPT_DIR}/releases"

function _find_control_file {
    # Prefer the new layout first, then fall back to any DEBIAN/control found.
    local preferred="${REPO_ROOT}/packaging/deb-root/DEBIAN/control"
    if [ -f "${preferred}" ]; then
        echo "${preferred}"
        return 0
    fi

    # Search for a Debian control file in a bounded depth to avoid scanning the world.
    # Note: macOS/BSD find supports -maxdepth; Debian find also supports it.
    local found
    found="$(find "${REPO_ROOT}" -maxdepth 5 -type f -path '*/DEBIAN/control' 2>/dev/null | head -n1 || true)"
    if [ -n "${found}" ] && [ -f "${found}" ]; then
        echo "${found}"
        return 0
    fi

    return 1
}

CONTROL_FILE="$(_find_control_file || true)"
if [ -z "${CONTROL_FILE}" ] || [ ! -f "${CONTROL_FILE}" ]; then
    echo "Missing Debian control file under repo root: ${REPO_ROOT}" >&2
    echo "Expected one of:" >&2
    echo "  - ${REPO_ROOT}/packaging/deb-root/DEBIAN/control" >&2
    echo "  - <any path matching */DEBIAN/control within 5 levels>" >&2
    exit 1
fi

# Debian staging root is the parent directory of DEBIAN/
DEB_ROOT="$(cd "$(dirname "${CONTROL_FILE}")/.." && pwd)"

# Debian package name (control: Package) is not the same as the install dir.
OPT_DIR_NAME="universalchess"
INSTALLDIR="/opt/${OPT_DIR_NAME}"

function detectVersion {
    echo "::: Getting version/package from ${CONTROL_FILE}"
    # CONTROL_FILE is resolved early and must exist by this point.

    # Use grep/cut to avoid awk quoting issues across environments.
    DEB_PACKAGE_NAME="$(grep -m1 '^Package:' "${CONTROL_FILE}" | cut -d':' -f2- | xargs)"
    VERSION="$(grep -m1 '^Version:' "${CONTROL_FILE}" | cut -d':' -f2- | xargs)"

    if [ -z "${DEB_PACKAGE_NAME}" ] || [ -z "${VERSION}" ]; then
        echo "Failed to parse Package/Version from ${CONTROL_FILE}" >&2
        exit 1
    fi
}

# Refuse to build a package that could never verify an update.
#
# The root install helper verifies each update's signed checksum manifest against
# this keyring. A package shipped without it installs fine and then refuses every
# subsequent OTA, leaving a board that cannot update itself and needs a .deb
# installed by hand to recover. Failing here keeps that outcome in the build
# instead of in the field.
function requireSigningKeyring {
    local keyring="${DEB_ROOT}/opt/universalchess/keys/release-signing.gpg"
    if [ ! -s "${keyring}" ]; then
        echo "::: ERROR: release signing keyring missing or empty:" >&2
        echo ":::        ${keyring}" >&2
        echo ":::" >&2
        echo "::: The update helper verifies releases against this keyring, so a" >&2
        echo "::: package built without it could not install any future update." >&2
        echo "::: See packaging/deb-root/opt/universalchess/keys/README.md for how" >&2
        echo "::: to generate the key and export the public keyring." >&2
        exit 1
    fi
}

# Collect the Python wheels the postinst installs the venv from.
#
# The board installs with --no-index, so these must travel inside the package:
# root then never runs code fetched at install time, and the wheels inherit the
# release signature instead of needing a second integrity mechanism.
#
# --require-hashes makes an index that served a different artifact for a pinned
# version fail the build rather than reach a board. --no-deps keeps the set to
# exactly what the lock names; the lock is already the full closure, minus what
# Debian supplies (see src/universalchess/setup/system-provided.txt).
function collectVendoredWheels {
    local lock="${REPO_ROOT}/src/universalchess/setup/pinned/requirements.txt"
    local wheelhouse="${STAGE_DIR}${INSTALLDIR}/wheels"

    if [ ! -s "${lock}" ]; then
        echo "::: ERROR: wheel lock missing or empty: ${lock}" >&2
        exit 1
    fi

    echo "::: Collecting vendored wheels"
    mkdir -p "${wheelhouse}"
    if ! python3 -m pip wheel \
            --require-hashes \
            --no-deps \
            --requirement "${lock}" \
            --wheel-dir "${wheelhouse}"; then
        echo "::: ERROR: could not build the vendored wheelhouse from ${lock}" >&2
        exit 1
    fi
}

# Refuse to build a package whose postinst could not install the venv.
#
# The postinst installs with --no-index and fails when the wheelhouse is absent,
# so a package without one cannot install at all. Catching that here keeps the
# failure in the build rather than on every board that takes the release.
#
# The architecture check matters just as much: the package declares
# Architecture: all, which is only honest while every wheel is pure Python. A
# lock entry that started resolving to a compiled artifact would produce a
# package that installs on the builder's architecture and fails on the boards.
function requireVendoredWheels {
    local wheelhouse="${STAGE_DIR}${INSTALLDIR}/wheels"
    local count
    count="$(find "${wheelhouse}" -maxdepth 1 -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"

    if [ "${count}" -eq 0 ]; then
        echo "::: ERROR: no wheels were collected into ${wheelhouse}" >&2
        echo "::: The postinst installs with --no-index, so this package could" >&2
        echo "::: not install its Python dependencies on any board." >&2
        exit 1
    fi

    local impure
    impure="$(find "${wheelhouse}" -maxdepth 1 -name '*.whl' \
        ! -name '*-py3-none-any.whl' ! -name '*-py2.py3-none-any.whl' 2>/dev/null || true)"
    if [ -n "${impure}" ]; then
        echo "::: ERROR: the wheelhouse contains non-universal wheels:" >&2
        echo "${impure}" >&2
        echo "::: The package is Architecture: all, so every wheel must be pure" >&2
        echo "::: Python. Move this dependency to system-provided.txt so Debian" >&2
        echo "::: supplies it instead." >&2
        exit 1
    fi

    echo "::: Vendored wheelhouse holds ${count} universal wheels"
}

function stage {
    requireSigningKeyring

    # Multi-arch package - use 'all' architecture for a pure-Python payload.
    STAGE_ARCH="all"
    STAGE="${DEB_PACKAGE_NAME}_${VERSION}_${STAGE_ARCH}"
    STAGE_DIR="${BUILD_TMP}/${STAGE}"

    echo "::: Staging build at ${STAGE_DIR}"
    rm -rf "${STAGE_DIR}"
    mkdir -p "${STAGE_DIR}"

    # Copy Debian staging root into temp stage dir (portable across GNU/BSD tar).
    (cd "${DEB_ROOT}" && tar -cf - .) | (cd "${STAGE_DIR}" && tar -xf -)

    # Ensure the installed python package is present under /opt/universalchess.
    # The canonical source lives in the repo at src/universalchess (src-layout).
    mkdir -p "${STAGE_DIR}${INSTALLDIR}"
    (
      cd "${REPO_ROOT}/src/universalchess" \
        && tar --exclude="__pycache__" --exclude="*.pyc" -cf - .
    ) | (cd "${STAGE_DIR}${INSTALLDIR}" && tar -xf -)

    # Ship the user-facing helper tools (e.g. the Centaur SD image generator the
    # web UI offers for download). They live in repo tools/ (not the python
    # package), so copy them under /opt/universalchess/tools so the web app can
    # serve them on-device.
    if [ -d "${REPO_ROOT}/tools/centaur-import" ]; then
      mkdir -p "${STAGE_DIR}${INSTALLDIR}/tools/centaur-import"
      # Ship both the macOS/Linux (.sh) and Windows (.ps1) image generators so
      # the web UI can offer each platform its own download.
      cp "${REPO_ROOT}/tools/centaur-import/"*.sh "${STAGE_DIR}${INSTALLDIR}/tools/centaur-import/" 2>/dev/null || true
      cp "${REPO_ROOT}/tools/centaur-import/"*.ps1 "${STAGE_DIR}${INSTALLDIR}/tools/centaur-import/" 2>/dev/null || true
    fi

    collectVendoredWheels
    requireVendoredWheels

    # Set Architecture to 'all' for multi-arch package
    python3 - <<PY
from pathlib import Path
p = Path("${STAGE_DIR}") / "DEBIAN" / "control"
lines = p.read_text(encoding="utf-8").splitlines(True)
out = []
replaced = False
for line in lines:
    if line.startswith("Architecture:"):
        out.append("Architecture: all\\n")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append("Architecture: all\\n")
p.write_text("".join(out), encoding="utf-8")
PY
}

function setPermissions {
    echo "::: Setting permissions"
    # Ensure maintainer scripts are executable if present
    if [ -d "${STAGE_DIR}/DEBIAN" ]; then
        chmod 0755 "${STAGE_DIR}/DEBIAN/"post* "${STAGE_DIR}/DEBIAN/"pre* 2>/dev/null || true
    fi

    # Some setups expect /opt/universalchess/engines writable for engine installs.
    if [ -d "${STAGE_DIR}${INSTALLDIR}/engines" ]; then
        chmod 0777 "${STAGE_DIR}${INSTALLDIR}/engines" || true
    fi
}

function prepareEngines {
    # Remove any compiled Stockfish binaries from staging.
    # Stockfish will be installed from system package during postinst.
    echo "::: Preparing engines directory"
    rm -f "${STAGE_DIR}${INSTALLDIR}/engines/stockfish" \
          "${STAGE_DIR}${INSTALLDIR}/engines/stockfish_pi" \
          "${STAGE_DIR}${INSTALLDIR}/engines/stockfish_pi_arm64" \
          "${STAGE_DIR}${INSTALLDIR}/engines/stockfish_pi_armhf" || true
    echo "::: Stockfish will be installed from system package during installation"
}

function buildLc0 {
    if [[ "$BUILD_LC0" != "true" ]]; then
        echo "::: Skipping lc0 build (use --with-lc0 to include)"
        return 0
    fi
    
    echo "::: Building lc0/Maia engine (this may take 20-30 minutes on Raspberry Pi)..."
    
    local lc0_build_script="${SCRIPT_DIR}/engines/build-lc0.sh"
    local lc0_output_dir="${STAGE_DIR}${INSTALLDIR}/engines"
    
    if [[ ! -x "$lc0_build_script" ]]; then
        echo "ERROR: lc0 build script not found: $lc0_build_script" >&2
        exit 1
    fi
    
    # Create engines directory if it doesn't exist
    mkdir -p "$lc0_output_dir"
    
    # Run the lc0 build script
    if ! "$lc0_build_script" "$lc0_output_dir"; then
        echo "ERROR: lc0 build failed" >&2
        exit 1
    fi
    
    echo "::: lc0/Maia engine built successfully"
    ls -la "${lc0_output_dir}/lc0" 2>/dev/null || true
    ls -la "${lc0_output_dir}/maia_weights/" 2>/dev/null || true
}

function removeDev {
    # Best-effort removal of runtime/dev artifacts that should not ship in a .deb.
    rm -f "${STAGE_DIR}${INSTALLDIR}/config/centaur.ini" || true
    rm -f "${STAGE_DIR}${INSTALLDIR}/db/centaur.db" || true
}

function createVersionFile {
    # Create VERSION file for the update checker to read. Prefer the release
    # tag (e.g. "nightly-2026-06-17-abc1234") when the build provides one via
    # RELEASE_TAG, because the checker compares the installed build against
    # GitHub release tags. Without this, every nightly carries the dpkg
    # version "2.0.0-nightly", which never matches a "nightly-..." tag, so the
    # checker always reports an update as available even when up to date.
    local version_string="${RELEASE_TAG:-${VERSION}}"
    echo "::: Creating VERSION file (${version_string})"
    echo "${version_string}" > "${STAGE_DIR}${INSTALLDIR}/VERSION"
}

function buildReactApp {
    echo "::: Building React web app"
    
    local web_app_dir="${REPO_ROOT}/src/universalchess/web-app"
    local react_dist="${STAGE_DIR}${INSTALLDIR}/web/react-app"
    local swap_created=false
    local swapfile="/tmp/buildswap"
    
    if [ ! -d "${web_app_dir}" ]; then
        echo "WARNING: React app directory not found: ${web_app_dir}" >&2
        return 0
    fi
    
    # Check if npm is available
    if ! command -v npm &> /dev/null; then
        echo "WARNING: npm not found, skipping React build" >&2
        return 0
    fi
    
    # On low-memory systems (< 1GB free), create temporary swap for the build
    # This prevents OOM during Vite bundling on Raspberry Pi
    local free_mem_mb
    free_mem_mb=$(awk '/MemAvailable/{printf "%.0f", $2/1024}' /proc/meminfo 2>/dev/null || echo "2048")
    if [ "$free_mem_mb" -lt 1024 ] && [ -w /tmp ]; then
        echo "::: Low memory detected (${free_mem_mb}MB free), creating temporary swap..."
        if sudo fallocate -l 1G "$swapfile" 2>/dev/null && \
           sudo chmod 600 "$swapfile" && \
           sudo mkswap "$swapfile" >/dev/null 2>&1 && \
           sudo swapon "$swapfile" 2>/dev/null; then
            swap_created=true
            echo "::: Temporary 1GB swap enabled at $swapfile"
        else
            echo "::: Warning: Could not create swap, build may fail on low memory"
        fi
    fi
    
    # Generate build timestamp for cache busting
    local build_timestamp
    build_timestamp="$(date +%s)"
    
    # Install dependencies and build
    local build_status=0
    (
        cd "${web_app_dir}"
        echo "::: Installing npm dependencies..."
        npm ci --silent 2>/dev/null || npm install --silent
        echo "::: Building React app for production..."
        # Limit Node.js heap size for low-memory devices (e.g., Raspberry Pi)
        export NODE_OPTIONS="--max-old-space-size=512"
        npm run build
    ) || build_status=$?
    
    # Clean up temporary swap
    if [ "$swap_created" = true ]; then
        echo "::: Removing temporary swap..."
        sudo swapoff "$swapfile" 2>/dev/null || true
        sudo rm -f "$swapfile" 2>/dev/null || true
    fi
    
    # Exit if build failed
    if [ "$build_status" -ne 0 ]; then
        echo "ERROR: React build failed" >&2
        return "$build_status"
    fi
    
    # Copy the built React app to the staging directory
    if [ -d "${web_app_dir}/dist" ]; then
        mkdir -p "${react_dist}"
        cp -r "${web_app_dir}/dist/"* "${react_dist}/"
        
        # Replace the cache version placeholder in sw.js with the build timestamp
        # This ensures each build has a unique cache name, forcing cache refresh
        if [ -f "${react_dist}/sw.js" ]; then
            # Use portable sed syntax (works on both macOS and Linux)
            if [[ "$OSTYPE" == "darwin"* ]]; then
                sed -i '' "s/__BUILD_TIMESTAMP__/${build_timestamp}/g" "${react_dist}/sw.js"
            else
                sed -i "s/__BUILD_TIMESTAMP__/${build_timestamp}/g" "${react_dist}/sw.js"
            fi
            echo "::: Service worker cache version set to ${build_timestamp}"
        fi
        
        echo "::: React app built and staged at ${react_dist}"
    else
        echo "WARNING: React build output not found at ${web_app_dir}/dist" >&2
    fi
}

function build {
    echo "::: Building ${DEB_PACKAGE_NAME} version ${VERSION}"
    mkdir -p "${RELEASES_DIR}"
    rm -f "${RELEASES_DIR}/${STAGE}.deb"

    dpkg-deb --root-owner-group -Zgzip --build "${STAGE_DIR}" "${RELEASES_DIR}/${STAGE}.deb"

    # Free staging immediately
    rm -rf "${STAGE_DIR}"
}

function clean {
    echo "::: Cleaning"
    rm -rf "${BUILD_TMP}/universal-chess_"* "${BUILD_TMP}/dgtcentaurmods_"* 2>/dev/null || true
    rm -rf "${RELEASES_DIR}" 2>/dev/null || true
}

function main {
    clean 2>/dev/null || true
    detectVersion
    stage
    removeDev
    createVersionFile
    buildReactApp
    setPermissions
    prepareEngines
    buildLc0
    build
}

case "${1:-}" in
    clean* )
        clean
        ;;
    * )
        main
        ;;
esac
