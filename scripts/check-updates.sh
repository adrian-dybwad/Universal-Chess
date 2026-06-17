#!/usr/bin/env bash
# Check for Universal-Chess updates from GitHub releases
#
# Usage:
#   ./check-updates.sh              # Check only, print if update available
#   ./check-updates.sh --download   # Check and download if available
#   ./check-updates.sh --install    # Check, download, and install
#   ./check-updates.sh --nightly    # Check nightly channel
#
# Exit codes:
#   0 - Update available (or installed successfully)
#   1 - Already up to date
#   2 - Error

set -euo pipefail

GITHUB_OWNER="adrian-dybwad"
GITHUB_REPO="Universal-Chess"
API_URL="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases"

# Parse arguments
ACTION="check"
CHANNEL="stable"
for arg in "$@"; do
    case "$arg" in
        --download) ACTION="download" ;;
        --install) ACTION="install" ;;
        --nightly) CHANNEL="nightly" ;;
        --help|-h)
            echo "Usage: $0 [--download|--install] [--nightly]"
            exit 0
            ;;
    esac
done

# Get current version
get_current_version() {
    if [[ -f /opt/universalchess/VERSION ]]; then
        cat /opt/universalchess/VERSION
    elif command -v dpkg-query &>/dev/null; then
        dpkg-query -W -f='${Version}' universal-chess 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

# Fetch latest release info
fetch_latest_release() {
    local filter_prerelease="$1"
    
    local releases
    releases=$(curl -s -H "Accept: application/vnd.github+json" "$API_URL" 2>/dev/null)
    
    if [[ -z "$releases" ]] || echo "$releases" | grep -q '"message"'; then
        echo "ERROR: Could not fetch releases" >&2
        return 1
    fi
    
    # Parse with jq if available, otherwise use grep/sed
    if command -v jq &>/dev/null; then
        if [[ "$filter_prerelease" == "true" ]]; then
            echo "$releases" | jq -r '[.[] | select(.prerelease == false)][0]'
        else
            echo "$releases" | jq -r '.[0]'
        fi
    else
        # Fallback: extract first release with grep/sed (less reliable)
        echo "$releases" | grep -o '"tag_name":"[^"]*"' | head -1 | sed 's/"tag_name":"//;s/"//'
    fi
}

# Compare versions (returns 0 if $1 > $2)
version_greater() {
    local new="$1"
    local current="$2"
    
    [[ "$current" == "unknown" ]] && return 0
    
    # Strip 'v' prefix
    new="${new#v}"
    current="${current#v}"
    
    # Simple numeric comparison (works for X.Y.Z format)
    # For more complex versions, use sort -V if available
    if command -v sort &>/dev/null && sort --version-sort /dev/null 2>/dev/null; then
        [[ "$new" != "$current" ]] && [[ "$new" == $(printf '%s\n' "$new" "$current" | sort -V | tail -1) ]]
    else
        # Fallback: compare dot-separated parts
        IFS='.' read -ra NEW_PARTS <<< "${new%%-*}"
        IFS='.' read -ra CUR_PARTS <<< "${current%%-*}"
        
        for i in 0 1 2; do
            local n="${NEW_PARTS[$i]:-0}"
            local c="${CUR_PARTS[$i]:-0}"
            [[ "$n" -gt "$c" ]] && return 0
            [[ "$n" -lt "$c" ]] && return 1
        done
        return 1
    fi
}

main() {
    local current_version
    current_version=$(get_current_version)
    echo "Current version: $current_version"
    
    # Determine if we filter prereleases
    local filter_prerelease="true"
    [[ "$CHANNEL" == "nightly" ]] && filter_prerelease="false"
    
    echo "Checking for updates (channel: $CHANNEL)..."
    
    local release_json
    release_json=$(fetch_latest_release "$filter_prerelease") || exit 2
    
    if command -v jq &>/dev/null; then
        local tag version download_url
        tag=$(echo "$release_json" | jq -r '.tag_name // empty')
        version="${tag#v}"
        download_url=$(echo "$release_json" | jq -r '.assets[] | select(.name | endswith(".deb")) | .browser_download_url' | head -1)
    else
        # Fallback without jq
        version="${release_json#v}"
        download_url=""
    fi
    
    if [[ -z "$version" ]]; then
        echo "ERROR: Could not determine latest version" >&2
        exit 2
    fi
    
    echo "Latest version: $version"
    
    if ! version_greater "$version" "$current_version"; then
        echo "Already up to date."
        exit 1
    fi
    
    echo "Update available: $current_version -> $version"
    
    if [[ "$ACTION" == "check" ]]; then
        exit 0
    fi
    
    # Download
    if [[ -z "$download_url" ]]; then
        echo "ERROR: No download URL found" >&2
        exit 2
    fi
    
    local tmp_dir
    tmp_dir=$(mktemp -d)
    local deb_path="${tmp_dir}/universal-chess_${version}_all.deb"
    
    echo "Downloading to $deb_path..."
    if ! wget -q -O "$deb_path" "$download_url"; then
        echo "ERROR: Download failed" >&2
        rm -rf "$tmp_dir"
        exit 2
    fi
    
    echo "Downloaded successfully."
    
    if [[ "$ACTION" == "download" ]]; then
        echo "Package saved to: $deb_path"
        exit 0
    fi
    
    # Install via apt so dependencies are resolved in one atomic transaction.
    # -f repairs a broken-dependency state from a prior failed install;
    # --reinstall forces the install even though every nightly shares the dpkg
    # version "2.0.0-nightly"; --allow-downgrades covers channel switches.
    echo "Installing..."
    sudo apt-get update || echo "apt-get update failed (continuing)"
    if ! sudo apt-get install -y -f --reinstall --allow-downgrades "$deb_path"; then
        echo "ERROR: Installation failed" >&2
        rm -rf "$tmp_dir"
        exit 2
    fi
    
    rm -rf "$tmp_dir"
    echo "Update installed successfully. Restart services to apply."
    exit 0
}

main

