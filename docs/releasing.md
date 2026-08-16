# Releasing Universal Chess

This document describes how to create a new release of Universal Chess.

## Quick Start

```bash
./scripts/release.sh
```

This interactive script guides you through the entire release process.

## Release Process Overview

1. **Tests pass** - All tests must pass before releasing
2. **Changelog updated** - CHANGELOG.md has entry for the new version
3. **Version bumped** - DEBIAN/control updated with new version
4. **Commit and tag** - Changes committed, version tagged
5. **Push** - Push to GitHub triggers CI/CD
6. **CI builds package** - GitHub Actions builds .deb
7. **Release created** - Assets uploaded to GitHub Releases

## Version Numbering

We follow [Semantic Versioning](https://semver.org/):

- **MAJOR** (2.0.0 -> 3.0.0): Breaking changes, major rewrites
- **MINOR** (2.0.0 -> 2.1.0): New features, backwards compatible
- **PATCH** (2.0.0 -> 2.0.1): Bug fixes only

## Scripts

### Interactive Release

```bash
./scripts/release.sh
```

Walks you through:
- Checking for uncommitted changes
- Selecting version type (patch/minor/major)
- Running tests
- Updating CHANGELOG.md
- Creating commit and tag
- Pushing to GitHub

### Quick Release

```bash
./scripts/release.sh patch   # Bug fix release
./scripts/release.sh minor   # Feature release  
./scripts/release.sh major   # Breaking change
./scripts/release.sh 2.1.0   # Explicit version
```

### Manual Version Bump

```bash
./scripts/bump-version.sh patch        # 2.0.0 -> 2.0.1
./scripts/bump-version.sh minor        # 2.0.0 -> 2.1.0
./scripts/bump-version.sh major        # 2.0.0 -> 3.0.0
./scripts/bump-version.sh 2.1.0        # Set explicit version
./scripts/bump-version.sh patch --tag  # Bump + create git tag
```

## Changelog Format

CHANGELOG.md follows [Keep a Changelog](https://keepachangelog.com/) format:

```markdown
## [2.1.0] - 2025-01-15

### Added
- New feature description

### Changed
- Changed behavior description

### Fixed
- Bug fix description

### Removed
- Removed feature description
```

Use `## [X.Y.Z] - Unreleased` while developing; the release script updates
the date automatically.

## CI/CD Workflows

### On Version Tag (v*)

`.github/workflows/release.yml`:
1. Builds .deb package
2. Generates SHA256 checksums
3. Extracts release notes from CHANGELOG.md
4. Creates GitHub Release with assets

### Nightly Builds

`.github/workflows/nightly.yml`:
- Runs daily at 2 AM UTC
- Runs on push to main/UniversalChess
- Creates pre-release with `-nightly.YYYYMMDD.sha` suffix

### Tests

`.github/workflows/test.yml`:
- Runs on every push and PR
- Tests Python 3.9, 3.11, 3.13

## Manual Release (Without Script)

If you prefer manual steps:

```bash
# 1. Ensure clean working tree
git status

# 2. Run tests
./bin/pytest

# 3. Update CHANGELOG.md with release notes
# Change "Unreleased" to today's date

# 4. Bump version
./scripts/bump-version.sh 2.1.0

# 5. Commit
git add -A
git commit -m "Release v2.1.0"

# 6. Tag
git tag -a v2.1.0 -m "Release 2.1.0"

# 7. Push
git push && git push --tags
```

## Troubleshooting

### CI Build Failed

Check [GitHub Actions](https://github.com/adrian-dybwad/Universal-Chess/actions)
for build logs.

### Wrong Version Released

```bash
# Delete the tag locally and remotely
git tag -d v2.1.0
git push origin :refs/tags/v2.1.0

# Delete the GitHub release manually, then re-release
./scripts/release.sh 2.1.0
```

### Forgot to Update Changelog

The release script prompts you to add changelog entries. If you already
pushed without notes, you can:

1. Update CHANGELOG.md
2. Commit: `git commit -am "Add release notes for 2.1.0"`
3. Push: `git push`
4. Edit the GitHub Release manually to add notes

## Files Involved

| File | Purpose |
|------|---------|
| `packaging/deb-root/DEBIAN/control` | Package version (source of truth) |
| `CHANGELOG.md` | Release notes |
| `scripts/release.sh` | Interactive release script |
| `scripts/bump-version.sh` | Version bump utility |
| `.github/workflows/release.yml` | CI release workflow |
| `.github/workflows/nightly.yml` | Nightly build workflow |

