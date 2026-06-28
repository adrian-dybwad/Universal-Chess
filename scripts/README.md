# Scripts

Build, development, and utility scripts for Universal-Chess.

## Quick Reference

| Task | Command |
|------|---------|
| **Create a release** | `./release.sh` |
| **Build .deb package** | `./build.sh` |
| **Bump version** | `./bump-version.sh patch` |
| **Check for updates** | `./check-updates.sh` |
| **Run the app** | `./run.sh` |
| **Deploy to a Pi (dev)** | `./deploy-to-pi.sh --host pi@<ip> --web` |

## Release & Versioning

| Script | Purpose |
|--------|---------|
| `release.sh` | Interactive release workflow (see [docs/releasing.md](../docs/releasing.md)) |
| `bump-version.sh` | Bump version in DEBIAN/control |
| `check-updates.sh` | Check GitHub for new releases |

**Creating a release:**
```bash
./release.sh           # Interactive mode
./release.sh patch     # Quick patch release (2.0.0 -> 2.0.1)
./release.sh minor     # Quick minor release (2.0.0 -> 2.1.0)
./release.sh 2.1.0     # Explicit version
```

See **[docs/releasing.md](../docs/releasing.md)** for complete documentation.

## Build Scripts

| Script | Purpose |
|--------|---------|
| `build.sh` | Build the .deb package |
| `rebuild.sh` | Full rebuild cycle: purge, build, install, restart |

## Running

| Script | Purpose |
|--------|---------|
| `run.sh` | Run the main application |
| `run-web.sh` | Run the web UI |
| `test-web.sh` | Quick web UI smoke test |

## How to use build.sh

- Interactive build: `./build.sh`
- Headless build: `./build.sh full`
- Clean only: `./build.sh clean`

Resulting `.deb` files are in `scripts/releases/`.

## Web smoke test

Quick check that the web UI is up and serving expected endpoints.

```bash
scripts/test-web.sh
scripts/test-web.sh http://host[:port]
BASE_URL=http://host[:port] scripts/test-web.sh
```

## Rebuild on Pi (one command)

Non-interactive end-to-end rebuild and redeploy on the device:

```bash
cd ~/Universal-Chess/scripts
chmod +x rebuild.sh  # first time only
./rebuild.sh               # builds from UniversalChess branch
./rebuild.sh my-feature    # builds from branch/tag my-feature
```

## Deploying to the Pi (dev sync)

`deploy-to-pi.sh` rsyncs the local `src/universalchess/` tree to a running Pi
and restarts the `universal-chess` and `universal-chess-web` services. It is a
runtime-only deploy (tests, the React source in `web-app/`, the venv, and
engines are excluded).

```bash
./scripts/deploy-to-pi.sh                     # sync + restart (default host pi@dgt.local)
./scripts/deploy-to-pi.sh --host pi@<ip>      # target a specific board IP
./scripts/deploy-to-pi.sh --web               # also build + ship the web bundle
./scripts/deploy-to-pi.sh --dry-run           # preview by size/time, no transfer
./scripts/deploy-to-pi.sh --check             # content (checksum) diff preview
```

**The `--web` flag (and why it's needed):** Vite builds the React app into
`web-app/dist/`, but Flask serves — and this script ships — the gitignored
`web/react-app/` artifact. Nothing else keeps them in sync, so a plain deploy
ships whatever bundle was last staged there. `--web` builds the app
(`tsc + vite`), stages `dist/` into `web/react-app/` with `sw.js` cache-version
stamping, and mirrors that directory to the Pi with `--delete` (pruning the
previous build's orphaned hashed assets). Use it whenever the web app **or**
the shared `menu.json` catalog changed (the web reads the catalog via
`/api/menu-schema`). Requires `npm` locally.

> Run the test suite before deploying:
> `PYTHONPATH=src .venv/bin/python -m pytest src/universalchess/tests -q`

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `engines/` | Build-time engine scripts (build-lc0.sh prebuilds lc0 into the .deb; ci-build-engines.sh). The runtime Maia source build (build-maia.sh) lives in the packaged tree at `src/universalchess/scripts/` so the installed app can invoke it. |
| `vm-setup/` | VM development environment setup |
| `config/` | Build configuration |
| `releases/` | Built .deb artifacts (gitignored) |

## Debugging & Development

| Script | Purpose |
|--------|---------|
| `probe.sh` | Probe board hardware |
| `board_probe.sh` | Low-level board diagnostics |
| `proxy.sh` | Serial proxy for debugging |
| `monitor_centaur_serial.py` | Monitor serial communication |

## Documentation

| Document | Description |
|----------|-------------|
| [docs/releasing.md](../docs/releasing.md) | Complete release process guide |
| [docs/architecture.md](../docs/architecture.md) | System architecture overview |
| [vm-setup/README.md](vm-setup/README.md) | VM development setup |
| [build-info.md](build-info.md) | Build system details |

## CI/CD

CI/CD is handled by GitHub Actions. See `.github/workflows/`:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push, PR | Run tests (Python 3.9, 3.11, 3.13) |
| `release.yml` | Tag `v*` | Build package, create GitHub release |
| `nightly.yml` | Daily, push to main | Nightly pre-release builds |
| `build.yml` | Manual | Build package without release |
