#!/usr/bin/env bash
# This file is part of the Universal-Chess project
# https://github.com/adrian-dybwad/Universal-Chess
#
# Interactive release script - guides you through creating a new release.
#
# Usage:
#   ./scripts/release.sh           # Interactive mode
#   ./scripts/release.sh patch     # Quick patch release
#   ./scripts/release.sh minor     # Quick minor release
#   ./scripts/release.sh major     # Quick major release
#   ./scripts/release.sh 2.1.0     # Explicit version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_step() {
    echo -e "${GREEN}[${1}]${NC} ${2}"
}

print_warning() {
    echo -e "${YELLOW}WARNING:${NC} $1"
}

print_error() {
    echo -e "${RED}ERROR:${NC} $1"
}

# Get current version from control file
get_current_version() {
    grep -m1 '^Version:' "${REPO_ROOT}/packaging/deb-root/DEBIAN/control" | cut -d' ' -f2
}

# Check for uncommitted changes
check_clean_tree() {
    if ! git diff --quiet HEAD 2>/dev/null; then
        print_error "You have uncommitted changes. Please commit or stash them first."
        git status --short
        exit 1
    fi
}

# Check we're on the right branch
check_branch() {
    local branch
    branch=$(git rev-parse --abbrev-ref HEAD)
    if [[ "${branch}" != "main" && "${branch}" != "UniversalChess" ]]; then
        print_warning "You're on branch '${branch}', not 'main' or 'UniversalChess'."
        read -p "Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

# Run tests
run_tests() {
    print_step "3" "Running tests..."
    if ! "${REPO_ROOT}/bin/pytest" -q; then
        print_error "Tests failed! Fix them before releasing."
        exit 1
    fi
    echo -e "${GREEN}All tests passed!${NC}"
}

# Check changelog has entry for version
check_changelog() {
    local version="$1"
    if ! grep -q "## \[${version}\]" "${REPO_ROOT}/CHANGELOG.md"; then
        print_warning "CHANGELOG.md doesn't have an entry for version ${version}."
        echo ""
        echo "Please add release notes to CHANGELOG.md before continuing."
        echo "Example entry:"
        echo ""
        echo "## [${version}] - $(date +%Y-%m-%d)"
        echo ""
        echo "### Added"
        echo "- New feature..."
        echo ""
        read -p "Open CHANGELOG.md in editor? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            ${EDITOR:-nano} "${REPO_ROOT}/CHANGELOG.md"
        fi
        
        # Re-check after editing
        if ! grep -q "## \[${version}\]" "${REPO_ROOT}/CHANGELOG.md"; then
            print_error "CHANGELOG.md still doesn't have entry for ${version}. Aborting."
            exit 1
        fi
    fi
}

# Update changelog date if it says "Unreleased"
update_changelog_date() {
    local version="$1"
    local today
    today=$(date +%Y-%m-%d)
    
    if grep -q "## \[${version}\] - Unreleased" "${REPO_ROOT}/CHANGELOG.md"; then
        print_step "4" "Updating CHANGELOG.md date..."
        if [[ "$(uname)" == "Darwin" ]]; then
            sed -i '' "s/## \[${version}\] - Unreleased/## [${version}] - ${today}/" "${REPO_ROOT}/CHANGELOG.md"
        else
            sed -i "s/## \[${version}\] - Unreleased/## [${version}] - ${today}/" "${REPO_ROOT}/CHANGELOG.md"
        fi
    fi
}

# Main release flow
main() {
    cd "${REPO_ROOT}"
    
    print_header "Universal Chess Release Tool"
    
    local current_version
    current_version=$(get_current_version)
    echo "Current version: ${current_version}"
    echo ""
    
    # Step 1: Check prerequisites
    print_step "1" "Checking prerequisites..."
    check_clean_tree
    check_branch
    echo -e "${GREEN}Repository is clean and on correct branch.${NC}"
    
    # Step 2: Determine new version
    print_step "2" "Determining new version..."
    local new_version=""
    local bump_type="${1:-}"
    
    if [[ -n "${bump_type}" ]]; then
        # Version specified on command line
        new_version=$("${SCRIPT_DIR}/bump-version.sh" "${bump_type}" 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -1 || echo "")
        if [[ -z "${new_version}" ]]; then
            # Explicit version given
            new_version="${bump_type}"
        fi
    else
        # Interactive mode
        echo ""
        echo "What type of release is this?"
        echo "  1) patch  - Bug fixes only (${current_version} -> $(echo ${current_version} | awk -F. '{print $1"."$2"."$3+1}'))"
        echo "  2) minor  - New features, backwards compatible (${current_version} -> $(echo ${current_version} | awk -F. '{print $1"."$2+1".0"}'))"
        echo "  3) major  - Breaking changes (${current_version} -> $(echo ${current_version} | awk -F. '{print $1+1".0.0"}'))"
        echo "  4) custom - Enter version manually"
        echo ""
        read -p "Select [1-4]: " -n 1 -r
        echo
        
        case $REPLY in
            1) bump_type="patch" ;;
            2) bump_type="minor" ;;
            3) bump_type="major" ;;
            4)
                read -p "Enter version (e.g., 2.1.0): " new_version
                bump_type="${new_version}"
                ;;
            *)
                print_error "Invalid selection"
                exit 1
                ;;
        esac
    fi
    
    # Calculate new version if not explicit
    if [[ -z "${new_version}" ]]; then
        IFS='.' read -r major minor patch <<< "${current_version}"
        case "${bump_type}" in
            patch) new_version="${major}.${minor}.$((patch + 1))" ;;
            minor) new_version="${major}.$((minor + 1)).0" ;;
            major) new_version="$((major + 1)).0.0" ;;
            *) new_version="${bump_type}" ;;
        esac
    fi
    
    echo ""
    echo -e "New version will be: ${GREEN}${new_version}${NC}"
    read -p "Continue? [Y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    
    # Step 3: Run tests
    run_tests
    
    # Step 4: Check/update changelog
    check_changelog "${new_version}"
    update_changelog_date "${new_version}"
    
    # Step 5: Bump version
    print_step "5" "Bumping version in DEBIAN/control..."
    "${SCRIPT_DIR}/bump-version.sh" "${new_version}"
    
    # Step 6: Commit and tag
    print_step "6" "Creating commit and tag..."
    git add -A
    git commit -m "Release v${new_version}"
    git tag -a "v${new_version}" -m "Release ${new_version}"
    
    echo ""
    print_header "Release v${new_version} Ready!"
    echo ""
    echo "The release has been committed and tagged locally."
    echo ""
    echo "To publish the release, run:"
    echo ""
    echo -e "  ${GREEN}git push && git push --tags${NC}"
    echo ""
    echo "This will trigger the GitHub Actions workflow to:"
    echo "  1. Build the .deb package"
    echo "  2. Create a GitHub release with assets"
    echo "  3. Upload checksums"
    echo ""
    read -p "Push now? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        git push && git push --tags
        echo ""
        echo -e "${GREEN}Release pushed! Check GitHub Actions for build status.${NC}"
        echo "https://github.com/adrian-dybwad/Universal-Chess/actions"
    fi
}

main "$@"

