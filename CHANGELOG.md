# Changelog

All notable changes to Universal Chess will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

### Overview

Universal Chess 2.0 is a major rewrite of the DGTCentaurMods project, focusing on
code quality, maintainability, and extensibility. The codebase has been completely
reorganized with proper module structure, comprehensive tests, and modern CI/CD.

### Added

- **Three-color (red/white/black) e-paper mode**: Opt-in `[display] three_color`
  switch for tri-color BWR panels, implemented in both the UC8151D (V2) and
  SSD1680 (V1) drivers (tri-color is a property of the panel, not the controller)
  - Fixes the black-into-red bleed by routing the B/W plane to the controller's
    B/W RAM and a parallel red plane to its red RAM (UC8151D `0x10`/`0x13`,
    SSD1680 `0x24`/`0x26`); the mono partial path had written B/W into the red RAM
  - Hybrid refresh: fast black/white updates during normal play; the slow full
    tri-color refresh (~12–15 s) only when red appears, changes, or clears
  - Highlights the checked king and checker, a threatened queen and its attacker,
    the game result line, and losing-side evaluation-graph bars in red
  - Live toggle in the Display tuning settings card and a red web-mirror preview
    (no reboot required)

- **Engine Registry**: Centralized management of UCI chess engine instances
  - Prevents duplicate engine processes
  - Automatic lifecycle management
  - Shared engine access across features

- **Engine Install Queue**: Background installation system for chess engines
  - Queue multiple engines for installation
  - Progress tracking with UI feedback
  - Cancel/clear queue operations
  - Install history

- **Update Checker**: Pull-based update system
  - Checks GitHub releases for new versions
  - Supports stable and nightly channels
  - Download and install from the device

- **Maia Engine Support**: Human-like neural network chess engine
  - Specialized ARM build script with memory management
  - Downloads all 9 ELO-rated weight files (1100-1900)
  - Single-threaded compilation for Raspberry Pi

- **Modern CI/CD**: GitHub Actions workflows
  - Automated testing on Python 3.9, 3.11, 3.13
  - Automated package builds on release tags
  - Nightly builds from main branch
  - Automatic release creation

- **Version Management**
  - VERSION file created during package build
  - `scripts/bump-version.sh` for semantic versioning
  - Proper version comparison for updates

### Changed

- **Project Structure**: Complete reorganization
  - Source code moved to `src/universalchess/`
  - Build scripts moved to `scripts/`
  - Packaging files in `packaging/`
  - Development tools in `tools/`

- **Package Architecture**: Changed to `all` (architecture-independent)
  - Python code works on both armhf and arm64
  - Engine binaries handled separately

- **Entry Point**: Renamed from `universal.py` to `main.py`

- **Board Controller**: Explicit initialization instead of import-time
  - Better test isolation
  - Cleaner startup sequence

### Removed

- **Deprecated Engines**: Fire, Laser (x86-only, incompatible with ARM)
- **Legacy CI**: Docker-based cron CI system (moved to `.github/legacy-ci/`)
- **Obsolete Tests**: Removed outdated promotion hardware tests

### Fixed

- Engine timeout issues on Raspberry Pi (removed default timeouts)
- Multiple engine instance conflicts (via EngineRegistry)
- dpkg lock conflicts during installation
- Various build script path issues

### Security

- No security issues addressed in this release

### Notes

- Minimum Python version: 3.9 (Debian Bullseye)
- Maximum tested Python version: 3.13 (Debian Trixie)
- Requires Raspberry Pi with DGT Centaur board

---

## [1.3.3] - Previous Release (DGTCentaurMods)

See the [DGTCentaurMods repository](https://github.com/EdNekebno/DGTCentaurMods)
for historical release notes.

