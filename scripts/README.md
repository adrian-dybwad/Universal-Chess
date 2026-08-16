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
| `changelog-audit.sh` | List commits that changed something observable without a `CHANGELOG.md` entry |

**Auditing the changelog before a push:**
```bash
./scripts/changelog-audit.sh              # origin/main..HEAD
./scripts/changelog-audit.sh v2.0.0       # since a tag
./scripts/changelog-audit.sh --strict      # exit non-zero if anything is undescribed
```

The changelog is the release notes for the unreleased version, so a missing entry
ships a change undescribed — which happened three times before this existed.
Findings are split by what can be known mechanically: **Undescribed** means no
changelog commit follows the change at all, so an entry is owed unless the change
is unobservable; **Possibly described** means one does follow and needs a read to
confirm it covers the change. Advisory by default, because whether a candidate
really needs an entry is a judgement call and a check that usually fails gets
bypassed. `--strict` gates on the Undescribed group alone.

A commit that genuinely owes no entry — developer process tooling, tests, a pure
refactor — says so with a git trailer, optionally with a reason:

```
Changelog: none -- developer tooling, no user-visible change
Co-authored-by: Cursor <cursoragent@cursor.com>
```

It has to sit in the last paragraph beside any other trailers, with no blank line
between them, or git does not parse it as a trailer and it does not count — the
audit says so when it spots one out of place.

Those appear under **Declared exempt** rather than being hidden, since an
exemption nobody sees is indistinguishable from the audit not looking. The trailer
is what makes `--strict` wirable to a hook without blocking legitimate work.

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

`deploy-to-pi.sh` rsyncs the local `src/universalchess/` tree to a running Pi,
restarts the `universal-chess` and `universal-chess-web` services, and verifies
the board is serving before reporting success. It is a runtime-only deploy
(tests, the React source in `web-app/`, the venv, and engines are excluded).

```bash
./scripts/deploy-to-pi.sh                     # sync + restart + verify (default host pi@dgt.local)
./scripts/deploy-to-pi.sh --host pi@<ip>      # target a specific board IP
./scripts/deploy-to-pi.sh --web               # also build + ship the web bundle
./scripts/deploy-to-pi.sh --dry-run           # preview by size/time, no transfer
./scripts/deploy-to-pi.sh --check             # content (checksum) diff preview
```

**Post-deploy verification:** `lib/remote-restart-and-verify.sh` is piped to the
board on ssh's stdin (so it always matches the checkout being deployed) and
decides whether the deploy succeeded. It polls `127.0.0.1:5000/api/system/activity`
until the web app answers, and fails if either unit auto-restarts while it waits.
Both checks are necessary: importing the Flask app takes roughly 70 seconds on
the board's ARMv6 core, so a short fixed wait passes before the app has even
finished starting, and because both units set `Restart=always`,
`systemctl is-active` reports `active` moments after every crash — a crash loop
is indistinguishable from health unless the restart counter is compared. A
deploy that printed "Deploy complete" over a web app crash-looping at import is
what prompted this. Exit codes are distinct: `2` a unit is not running, `3` a
unit crashed on the deployed code, `4` the web interface never served within
`--verify-timeout` (default 240s; verification returns as soon as the app
answers, so the generous default costs a healthy board nothing).

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
> `PYTHONPATH=src .venv/bin/python -m pytest -q`

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `engines/` | Build-time engine scripts (build-lc0.sh prebuilds lc0 into the .deb; ci-build-engines.sh). The runtime Maia source build (build-maia.sh) lives in the packaged tree at `src/universalchess/scripts/` so the installed app can invoke it. |
| `vm-setup/` | VM development environment setup |
| `config/` | Build configuration |
| `releases/` | Built .deb artifacts (gitignored) |

## Asset Generation

One-off generators for checked-in binary assets. Re-run only when the source
artwork changes; each writes its output straight into the tree.

| Script | Purpose |
|--------|---------|
| `make-maskable-icons.py` | Build the PWA maskable icons (`icon-<size>-maskable.png`) from the full-bleed logo, scaling the artwork into the Android adaptive-icon safe zone |
| `make-split-sprite-sheet.py` | Build a SPLIT chess sprite sheet (`chesssprites_<id>.bmp`, 1-bit ink + mask) from 12 piece PNGs |
| `make-svg-sprite-sheet.py` | Build a COLORWAY chess sprite sheet (`chesssprites_<id>.png`, RGBA) by rasterising a packed piece SVG (needs `cairosvg`) |

```bash
python scripts/make-maskable-icons.py
```

Icon sizes come from `public/manifest.json`; adding a size there means passing
it to `--sizes` and adding the file to the service worker's precache list.
`src/manifest.test.ts` and `src/serviceWorkerPrecache.test.ts` fail until both
are updated.

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
