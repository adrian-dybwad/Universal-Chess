# BlueZ advertising self-heal — applying a pre-release fix without freezing the OS

## TL;DR

Some kernel/BlueZ combinations ship a released BlueZ that is broken against a
newer kernel. The concrete case: kernel 6.18 added length validation for the
`Add Extended Advertising Data` MGMT command, and released BlueZ 5.82 builds that
command with the wrong struct size, so the kernel rejects it and the board
becomes undiscoverable over BLE. The fix exists upstream (BlueZ `2a6968b4`) but
is **not in any released version**. See
[le-advertising-ext-adv-data-regression.md](./le-advertising-ext-adv-data-regression.md)
for the byte-level root cause.

Rather than freeze the OS, we **self-heal**: at install/upgrade we check whether
advertising actually works and, only if it does not, substitute a minimally
patched `bluetoothd` built from *this machine's own* BlueZ source. The patch
**auto-retires** the moment the distribution ships a BlueZ that works. The board
keeps receiving OS/kernel security updates throughout, and both the web and the
device screen warn whenever a patched (non-stock) stack is active.

This document explains the why, the how, and — importantly — how to reuse the
same pattern any time you must run a **pre-release** patch to a distribution
binary without giving up future updates.

## Why not the obvious approaches

| Approach | Why we rejected it |
| --- | --- |
| `apt-mark hold bluez` on a forked, patched package | Freezes BlueZ at one version and **silently drops its security updates**. You must remember to unfreeze later, and you carry a forked package forever. |
| Pin the kernel to a working `6.12.x` | Stays on an older kernel **without** the security patch that exposed the bug, and breaks again once that patch is backported to 6.12.x. |
| Ship a prebuilt patched `bluetoothd` in our package | A binary built on one OS is **not ABI-compatible** across OS/library versions (libell/glib/dbus sonames differ). One prebuilt binary cannot serve "any Pi OS". |

The self-heal avoids all three: it is **version-agnostic** (decides by function,
not by version string), **compatibility-first** (builds against the running OS's
own libraries), and **self-retiring** (no permanent fork, no held package).

## The strategy: decide by function, not by version

Version strings differ across distributions and the fix may be backported, so we
never match versions. We ask the only question that matters:

> Does registering a BLE advertisement succeed on **this** kernel + `bluetoothd`?

`scripts/bluez-advertising-probe` answers it by registering one minimal
`LEAdvertisement1` over the BlueZ D-Bus API: exit 0 (`PROBE_RESULT=PASS`) if it
registers, non-zero (`PROBE_RESULT=FAIL`) if BlueZ rejects it.

`scripts/bluez-selfheal run` then:

1. Ensures the **stock** `bluetoothd` is in place (removes any prior diversion)
   and probes.
2. **PASS** → the OS is fine (or already carries the fix). Record `stack=stock`.
   This is the auto-retire path: when the distro catches up, the patch is removed
   automatically.
3. **FAIL** → build a `bluetoothd` from the OS's own `bluez` source with the
   one-line fix, `dpkg-divert` the stock binary aside, install the patched one,
   restart, and re-probe.
   - PASS → record `stack=patched`.
   - FAIL → roll back to stock, record `stack=stock` (degraded). The advertising
     failure then surfaces in the UI as `ADV_FAILED`.

### The one-line fix, applied portably

Upstream's fix changes the extended-advertising path to size the command with
`sizeof(*cp)`. In BlueZ 5.82 the buggy expression appears twice and is textually
identical:

```c
param_len = sizeof(struct mgmt_cp_add_advertising) + adv_data_len + scan_rsp_len;
```

- In `refresh_legacy_adv()`, `cp` **is** `struct mgmt_cp_add_advertising`, so
  `sizeof(*cp)` is identical — a safe no-op.
- In `add_adv_params_callback()`, `cp` is `struct mgmt_cp_add_ext_adv_data`, so
  `sizeof(*cp)` is the actual fix.

Because both are safe, the self-heal applies a **content-based** replacement
(`sizeof(struct mgmt_cp_add_advertising) + adv_data_len` → `sizeof(*cp) + adv_data_len`)
rather than a line-numbered patch, so it survives minor source revisions across
distributions.

### Building against the running OS (ABI safety)

The patched binary is compiled with the **distribution's own** `debian/rules`
configure flags (`debian/rules override_dh_auto_configure`) and built from
`apt-get source bluez` on the target, so it links the exact `libell`, `glib`,
and `dbus` that the OS provides. Only the `src/bluetoothd` target is built (not
the whole package), and the result is cached under
`/var/lib/universalchess/bluez/bluetoothd-<version>-<arch>` so reinstalling our
app does not rebuild.

## When it runs — install/upgrade, **not** every boot

The heavy, state-changing work (probe → build → `dpkg-divert`) runs **only when
the relevant inputs can have changed**:

- our package install/upgrade (the `postinst` starts the oneshot), and
- any apt run that could touch `bluez` or the kernel — the APT hook
  `/etc/apt/apt.conf.d/80-universal-chess-bluez` starts the oneshot **after** apt
  releases its lock.

The state-changing apply/retire work is **not** an unconditional boot-time job. A
`dpkg-divert` and the substituted binary persist on disk across reboots, so
re-applying every boot would be wasted work. Three things make this safe:

1. **Fast no-op guard.** The marker records the `bluez` version and kernel the
   decision was made against, plus a `healthy` flag. If both are unchanged since
   the last *confirmed-healthy* run, the script exits in ~1s without touching
   Bluetooth. (A *degraded* result — `healthy=false` — does not fast-skip, so a
   later online run can still heal it.)
2. **Per-boot detection.** `BleManager` registers the adverts at service start
   and sets `ADV_FAILED` if BlueZ rejects them, so a broken board is visibly
   flagged every boot regardless of the self-heal.
3. **Gated boot safety net.** `universal-chess-bluez-selfheal-boot.service` runs
   `bluez-selfheal boot` on each boot, but that mode **re-runs the heal only when
   the marker is absent or `healthy != true`**. A confirmed-healthy board does
   nothing (no probe, no `bluetoothd` restart). This recovers the one case the
   install/apt triggers miss: a reboot that **interrupts a first-time on-board
   build** — notably the fresh-install reboot in `postinst`, which starts the
   heal `--no-block` then reboots seconds later — would otherwise leave stock
   BlueZ broken with nothing to re-trigger it. The *inputs-changed* case (OS
   upgrade) is not the boot net's job: it flows through apt, which re-triggers
   via the hook before the reboot, so the marker is already healthy by boot.

So: **apply/retire is event-driven (install/apt), with a gated boot net for an
interrupted first heal; detection is continuous.** "This doesn't need to run on
*every* boot, right?" — correct: it only does real work at boot when the last
attempt did not leave a healthy result.

When a run actually changes the stack it restarts `bluetooth`, which drops every
advertisement the board service had registered (and `BleManager` does not
re-register on a `bluetoothd` restart). The script therefore `try-restart`s
`universal-chess.service` at the end of any real evaluation so the board
re-registers its adverts against the resulting `bluetoothd`. The fast no-op path
restarts nothing.

## What the operator sees (patched vs stock warning)

`scripts/bluez-selfheal` writes `/var/lib/universalchess/bluez-patch.json`. The
board reads it once at BLE bring-up (`managers/bluez_patch_status.read_status`,
wired in `managers/ble.py`) and carries it in the live Bluetooth status snapshot
(`managers/bluetooth_status_state` → `stack`). When `stack.patched` is true:

- **Web** (Connectivity → Bluetooth card): an amber banner — *"Running a patched
  (non-stock) Bluetooth stack based on BlueZ <version>. … This binary does not
  receive distribution security updates until it is rebuilt or retired."*
- **Device** (Bluetooth status screen): a `Stack` row — *"Patched BlueZ
  (pre-release fix) on <version> - not stock / No distro security updates."*

A stock or undetermined stack shows nothing, so healthy boards are never nagged.
The schema and warning wording live in one module
(`managers/bluez_patch_status`) so the two surfaces cannot drift.

## Components

| File | Role |
| --- | --- |
| `scripts/bluez-advertising-probe` | Functional probe: can BlueZ register an LE advert on this kernel? |
| `scripts/bluez-selfheal` | Orchestrator: probe → build-from-source → `dpkg-divert` apply/retire → write marker. Subcommands: `run`, `boot` (gated: only when marker absent/degraded), `probe-only`, `retire`, `status`. |
| `packaging/.../universal-chess-bluez-selfheal.service` | Install/apt-triggered `oneshot` that runs `bluez-selfheal run` (long `TimeoutStartSec` for a first build). |
| `packaging/.../universal-chess-bluez-selfheal-boot.service` | Boot `oneshot` (`WantedBy=multi-user.target`, `After=network-online.target`) that runs `bluez-selfheal boot` — the gated safety net for an interrupted first heal. |
| `packaging/.../apt.conf.d/80-universal-chess-bluez` | `DPkg::Post-Invoke` hook that kicks the install/apt oneshot (`--no-block`) after apt. |
| `DEBIAN/postinst` | Makes the scripts executable, creates the state dir, triggers the install/apt oneshot, and enables the boot safety-net unit. |
| `managers/bluez_patch_status.py` | Marker schema + `read_status` + `warning_label` (shared by web + device). |
| `managers/bluetooth_status_state.py` | Carries `stack` in the live snapshot. |

## Operations

```bash
# What stack is active right now?
sudo /opt/universalchess/scripts/bluez-selfheal status

# Re-evaluate now (build/apply/retire as needed; fast no-op if unchanged).
sudo /opt/universalchess/scripts/bluez-selfheal run

# Just ask the functional question (no changes).
sudo /opt/universalchess/scripts/bluez-selfheal probe-only

# Force back to the stock binary (e.g. for testing). The next 'run' will
# re-evaluate and re-apply if stock is still broken.
sudo /opt/universalchess/scripts/bluez-selfheal retire

# Logs:
journalctl -u universal-chess-bluez-selfheal.service
journalctl -t bluez-selfheal
```

## The generalizable pattern: shipping a pre-release patch safely

This is bigger than one BlueZ bug. Any time **upstream has a fix that is not yet
in your distribution** (or you need a local patch to a distro binary *before* a
release carries it), the same recipe applies and keeps you update-safe:

1. **Detect by behavior, not version.** Write the smallest runtime check that
   distinguishes "works" from "broken" (here: register one advertisement). This
   is what makes the patch auto-retire and survive backports — you never hard-code
   a version you'd have to maintain.
2. **Build against the target, not for it.** Rebuild the affected component from
   *the running OS's own source* with the distro's configure flags, so the
   artifact is ABI-correct on that exact OS. Cache it keyed by
   `(package-version, arch)`.
3. **Substitute reversibly with `dpkg-divert`.** Diverting the stock file aside
   (rather than overwriting it) means the package manager still "owns" the path,
   the original is one command away, and future package upgrades land cleanly.
   Never `apt-mark hold` — that is what drops security updates.
4. **Evaluate on change, not on a timer.** Trigger on your install/upgrade and via
   an APT `Post-Invoke` hook (after the lock is free); make the script a fast
   no-op when inputs are unchanged. Do not add boot-time work.
5. **Make the deviation loud.** Record a marker and surface a warning everywhere
   the operator looks; a substituted system binary that nobody can see is a
   latent security and support problem.
6. **Degrade safely.** If the build or apply fails, roll back to stock atomically
   and record the degraded state so a later (online) run retries — never leave the
   canonical path without a working binary.

Following these six points, the mechanism here can be retargeted to a different
package or a different fix by changing the probe and the patch step; everything
else (divert, marker, UI warning, triggers, no-op guard) is reusable.

## Limitations and failure modes

- **Offline first-heal.** Building needs `deb-src`, build-deps, and the source
  package. If the board is offline when stock is broken, the build fails, the
  script keeps stock and records `healthy=false`, and `ADV_FAILED` shows in the
  UI; the next online boot (the gated boot net re-runs on a degraded marker), apt
  run, or a manual `run` heals it.
- **Fresh install onto an already-broken kernel.** The `postinst` reboots on a
  fresh install, which can interrupt a first-time build (leaving no marker /
  empty cache). This is now recovered automatically: the boot safety-net unit
  (`universal-chess-bluez-selfheal-boot.service`) sees the absent/degraded marker
  on the next boot and re-runs the heal. The APT hook also re-triggers it on the
  next apt run, and the failure is visible as `ADV_FAILED` (then "self-heal in
  progress") meanwhile.
- **Hard power cut mid-heal.** Power loss is the one interruption the script's
  `trap` cannot catch. It is handled defensively rather than prevented:
  - *Applying the patch* stages the binary then swaps it in with a single atomic
    rename, so the canonical `bluetoothd` path is file-less for ~one rename, not a
    whole copy. If a cut still lands there, the next boot's safety net runs
    `retire_patch`, which renames `bluetoothd.stock` back — Bluetooth is down for
    that one boot, then recovers.
  - *The build cache* is written atomically (temp + rename) and integrity-checked
    (`--version`) before reuse, so a truncated binary can never be cached or
    reused — which would otherwise loop the heal forever.
  - *The progress file* is cleared on entry to `main`/`boot` and ignored once
    older than the heal timeout (`HEAL_MAX_AGE_SECONDS`), so a stale `running`
    record left by the cut cannot pin the UI on "Repairing…".
- **Trust.** The patched binary is built locally from the distro's signed source;
  it is not signed by the distribution and does not receive its security updates
  while active — hence the prominent warnings and the auto-retire.
- **First build cost.** Compiling `bluetoothd` on a Pi Zero 2 W takes several
  minutes (the oneshot allows up to 40). Subsequent runs use the cache.
