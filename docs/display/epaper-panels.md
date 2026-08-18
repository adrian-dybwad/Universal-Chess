# E-Paper Panels: DGT Centaur V2 and V1 (SSD1680 / E029A01)

> **Status (2026-06-22): two of three panels working; one (the faint
> `E029A01T1956C0`) still unsolved.**
> The original V2 panel works. A bench `E029A01` (SSD1680) renders correctly on
> the automatic SSD1680 fallback. A third, remotely-held `E029A01T1956C0` panel
> reaches the SSD1680 driver but renders a very faint image; that is the open
> problem. Selectable, attributed waveform profiles for it exist (see below),
> applied live without a reboot.

## Summary

The 2.9" e-paper display on a DGT Centaur is not a single part. At least three
distinct panel/controller combinations exist in the field, and they do not all
speak the same protocol or need the same waveform. This document records what we
know about each, how the driver selection copes with the differences, and what
remains unsolved.

| # | Panel | Controller | BUSY polarity | Status |
|---|---|---|---|---|
| 1 | DGT Centaur **V2** | UC8151D | LOW = idle | Works (primary driver) |
| 2 | **E029A01** (bench, `FPC-7519rev.b`) | SSD1680 | HIGH = busy | Works (SSD1680 fallback, default profile) |
| 3 | **E029A01T1956C0** (remote) | SSD16xx-family (unconfirmed) | unconfirmed | **Faint image — unsolved** |

The two controllers use **inverse BUSY polarity** and **entirely different
command sets**, which is the root of the V1/V2 split.

**Both** controllers are now tunable via selectable, attributed waveform
profiles, applied live without a reboot. This covers replacement panels: a field
unit may have had its panel swapped for a **UC8151D variant** (flexible
`GDEW029I6FD`, `GDEW029M06`, LILYGO `T5D`) that passes the primary driver's BUSY
check yet ghosts or renders faint on the stock partial waveform. The UI offers
only the **active** controller's profiles.

## Driver architecture

- **Primary driver — UC8151D:** `epaper/framework/waveshare/epd2in9d.py`.
  Used for the V2 panel. BUSY is active-LOW (idle = LOW). A bounded wait raises
  `EPDTimeoutError` after `BUSY_TIMEOUT_SECONDS = 5.0`; `init()` converts that to
  a `-1` result so the board disables the display rather than hanging.
  Consumes a UC8151D `WaveformProfile` (`+ high_contrast`), with
  `apply_profile()` for live re-selection. **Full refresh is OTP for every
  UC8151D profile** (the already-working V2 full path is untouched); only the
  **partial** register LUTs (`0x20`–`0x24`) and analog bytes (PLL `0x30`,
  VCOM_DC `0x82`, interval `0x50`) come from the profile. The no-config default
  (`uc8151d_waveshare`) reproduces the stock `epd2in9d.py` partial byte-for-byte.
- **Fallback driver — SSD16xx/IL3820 family:**
  `epaper/framework/waveshare/epd2in9_ssd1680.py`. Used for V1-family (E029A01)
  panels. BUSY is active-HIGH (busy = HIGH) — the **inverse** of the UC8151D.
  Same 128×296 geometry. Drives the panel from a selected **waveform profile**
  (see `waveform_profiles.py`). The V1 family does **not** share one protocol, so
  each profile names a **driver strategy** that selects the init sequence, LUT
  format and refresh-activation bytes:
  - `ssd1680` — Waveshare `epd2in9_V2.py` style. Register full+partial LUTs (159
    bytes each = 153 LUT + 6 voltage), full activation `0xC7`. With `use_otp`,
    full is driven from the panel OTP (`0xF7`) and partial falls back to full.
    The **default** profile uses this with the `WS_20_30` / `WF_PARTIAL_2IN9`
    tables ported verbatim from Waveshare (GDEM029T94 / SSD1680).
  - `il3820` — true IL3820/GDEH029A1. **No** SWRESET; voltages programmed in init
    (booster `0x0C`, VCOM `0x2C`, dummy-line `0x3A`, gate-width `0x3B`); a
    **30-byte** LUT via `0x32`; full activation `0xC4`, partial `0x04`.
  - `dke_ssd1680` — DEPG0290BS (SSD1680). Full from OTP (`0xF7`); a **153-byte**
    register partial LUT (no voltage bytes), partial activation `0xCC`.

  All non-default strategies are transcribed verbatim from GxEPD2 (Jean-Marc
  Zingg); see each profile's `source`.
- **Selection logic:** `board/display_selection.py` →
  `should_attempt_alt(primary)` returns True **only** when the primary attempt
  fails *by BUSY timeout* (the V1 signature). `app/display_boot.py::init_display`
  tries UC8151D first and constructs the SSD1680 driver only on that timeout.
  The outcome (active controller + `busy_timeout`) is published to the
  cross-process display-status file read by the web UI.

The SSD1680 fallback is **automatic**; it requires no opt-in. The waveform
profile selection below only *configures* the SSD1680 driver once it has been
selected, and is applied **live** (no reboot).

### Partial refresh (differential baseline)
All three V1 drivers do **differential** partial refreshes: the controller
transitions each pixel from its OLD value (RAM `0x26`) to its NEW value (RAM
`0x24`). The OLD bank must therefore hold *the frame currently on the panel*
before each partial. Two things break that if left unmanaged: `init()`'s SWRESET
(run on every full→partial / deep-sleep-wake transition) wipes both RAM banks,
and a partial otherwise never re-writes `0x26`. So `DisplayPartial`
(`_write_partial_rams`) re-loads `0x26` with the last shown frame (`self.buffer`)
and `0x24` with the new frame on **every** call — mirroring GxEPD2's
`writeImageAgain`.

This fixed a real ghosting bug: previously only `0x24` was written, so every
partial diffed against the last *full-refresh* baseline (or, after a transition,
garbage), and the prior frame was never cleared — e.g. a clock's digits stacked
on top of each other. The waveform/voltage profiles and the high-contrast toggle
could **not** fix this, because the cause was baseline-RAM management, not the
waveform. Pinned by `PartialBaselineTests` in `tests/test_epd_ssd1680.py`.

## Panel 1 — DGT Centaur V2 (UC8151D)

- The factory V2 panel. Works on the unmodified primary driver with the default
  `uc8151d_waveshare` profile (byte-for-byte the stock partial waveform).
- BUSY active-LOW. Initializes within the 5 s window, so the SSD1680 fallback is
  never attempted.
- **Replacement-panel tuning:** because field units may have a swapped UC8151D
  *variant*, the display-tuning card is now shown on V2 panels too, offering the
  UC8151D profiles (`uc8151d_waveshare` default, `uc8151d_gdew029i6fd`,
  `uc8151d_t5d`, experimental `uc8151d_gdew029m06`). On an unmodified factory
  panel the default needs no change.

## Panel 2 — E029A01, bench (SSD1680)

- **Physical identity:** Waveshare "2.9inch e-Paper" raw rigid panel, SKU
  **12563** — bare panel, no HAT/PCB driver board. 296×128, black/white, SPI.
- **Markings observed:** panel part `E029A01`; flex/PCB markings seen across
  sources as `E029A01-FPCA-V2.0`, `E029A01-FPC-A1`, and the bench unit's flex is
  stamped `FPC-7519rev.b`. (The `E029A01-FPC-*` part number and the flex-cable
  manufacturing number are independent markings; both have been observed.)
- **Controller:** SSD1680.
- **Status:** Works correctly on the SSD1680 fallback using the default
  `gdem029t94` profile (register LUT `WS_20_30`, full-refresh activation byte
  `0xC7`). Confirmed working *before* the profile model was added.
- **Regression guarantee:** when no profile is configured, the driver resolves to
  the `gdem029t94` profile, whose path is byte-for-byte identical to the prior
  hard-coded driver. High contrast defaults **off**. So this panel behaves exactly
  as it did. Pinned by `test_default_writes_register_lut`, the `0xC7` case of
  `test_full_refresh_control_byte_depends_on_otp`,
  `test_partial_stays_partial_when_not_otp` in `tests/test_epd_ssd1680.py`, and
  `test_default_is_gdem029t94_with_register_luts` in
  `tests/test_waveform_profiles.py`.
- **Role going forward:** regression reference. A candidate profile for Panel 3
  can be selected here first to confirm it does not break a known-good panel — but
  the cure itself can only be confirmed on Panel 3.

## Panel 3 — E029A01T1956C0, remote (faint image) — UNSOLVED

- **Full marking:** `E029A01T1956C0 V07CC1Y597U055`. Not physically available to
  us; held by a remote tester.
- **Symptoms:**
  - Serial/LED path is healthy (the V1 startup LED "spinning circles" stop, so
    the Pi reaches and initializes the board).
  - Reaches the SSD1680 fallback (UC8151D times out → fallback selected).
  - **Draws, but very faintly**, with ghosting of the previous (stock firmware)
    image still visible behind it.
- **What has been tried:** an earlier **mislabeled** "IL3820 additions" profile
  (which actually layered IL3820 analog tweaks on top of the SSD1680 init + the
  159-byte SSD1680 LUT) made *little to no difference*. That was **not** a real
  IL3820 driver. It has since been replaced by a **faithful IL3820 profile**
  (`il3820` driver: no SWRESET, 30-byte LUT, `0xC4`/`0x04` activation) plus a new
  **DEPG0290BS** profile — neither tried on this panel yet.
- **Working hypothesis:** the `E029A01T1956C0` is a different panel revision (or
  controller variant) from the bench `E029A01`. The default `WS_20_30` LUT and
  voltages are correct for the bench `FPC-7519rev.b` revision but under-drive
  this one. The two leading causes of a faint-but-drawing image are (a) a wrong
  waveform LUT for this revision, or (b) under-driven source/VCOM voltages.
- **Why we can't bench-fix it:** the panel is not here, and changing the
  *defaults* to chase it would risk regressing the working bench panel. Hence the
  selectable profiles, applied and compared remotely.

## Waveform profiles (both controllers)

A **waveform profile** is the self-contained recipe for how a panel moves its
pixels. Each profile is tagged with a **controller family** (`ssd16xx` /
`uc8151d`): the SSD16xx families carry a **driver strategy** (`ssd1680` /
`il3820` / `dke_ssd1680`) plus LUT data and/or the panel's OTP waveform; the
UC8151D family carries a `Uc8151dWaveform` (5 partial register LUTs + analog
bytes, full always OTP). Profiles live in
`epaper/framework/waveshare/waveform_profiles.py` as a small, **attributed**
registry — every register LUT is transcribed verbatim from a credited published
source, never invented (see "Data integrity" in that module).

Exposed in the web UI under **Settings → System → "Display tuning"** as a
dropdown plus a high-contrast toggle, backed by `GET/POST
/api/system/display-tuning`. The card is **shown whenever the board reports an
initialized panel with a known controller** (`_display_tuning_available()` /
`_active_waveform_controller()`), and the dropdown is **filtered to the active
controller** so it never offers a table the live driver cannot drive. The card
title/copy adapt to the active controller (V1 vs V2). The selection is stored in
`[display] waveform_profile` / `high_contrast` (one key shared across
controllers; each driver resolves it against its own family, falling back to that
controller's verified default for a mismatched key — e.g. after a panel swap).

### Live apply (no reboot)
Selecting a profile (or toggling high contrast) **takes effect immediately**:

1. The web POST persists the setting and sends the board process a
   `display_profile` command (`send_board_command`, the same Unix-socket channel
   used for board remote control).
2. `app/board_app.py::_on_board_command` defers it to the main thread
   (`_process_pending_display_profile`), which calls
   `Manager.apply_waveform_profile(...)`.
3. That swaps the driver's profile (`EPD.apply_profile`), forces the scheduler to
   re-run `init()` on the next refresh (`Scheduler.force_reinit`), and submits a
   **full refresh** — so the current screen redraws with the new waveform/voltages.

If the board process is not running, the change still applies on the next boot
from the persisted setting.

### Shipped profiles — SSD16xx family (V1 fallback driver)
| Key | Label | Driver | What it does | Source |
|---|---|---|---|---|
| `gdem029t94` | Waveshare 2.9″ V2 — GDEM029T94 (SSD1680) | `ssd1680` | Register LUT `WS_20_30` full / `WF_PARTIAL_2IN9` partial; activation `0xC7`. The no-config **SSD16xx default**; identical to the prior driver. | Waveshare e-Paper (also GxEPD2) |
| `builtin_otp` | Built-In (panel OTP waveform) | `ssd1680` (`use_otp`) | Skips the register LUT; activation `0xF7` (load temperature + OTP LUT). No register partial LUT exists, so **every partial refresh becomes a full refresh**. | Panel factory OTP |
| `il3820_gdeh029a1` | IL3820 / GDEH029A1 (Good Display) | `il3820` | Faithful IL3820: no SWRESET; init programs booster `0x0C=D7 D6 9D`, VCOM `0x2C=A8`, dummy-line `0x3A=1A`, gate-width `0x3B=08`; **30-byte** full/partial LUTs via `0x32`; activation `0xC4` full / `0x04` partial. | GxEPD2 `GxEPD2_290` (Good Display IL3820 demo) |
| `depg0290bs` | DEPG0290BS (SSD1680, OTP full + LUT partial) | `dke_ssd1680` | SSD1680 init with border `0x3C=05` + internal temp `0x18=80`; **full from OTP** (`0xF7`); **153-byte** register partial LUT, activation `0xCC`. | GxEPD2 `GxEPD2_290_BS` (Good Display DEPG0290BS demo) |

### Shipped profiles — UC8151D family (V2 primary driver)
Full refresh is **OTP for all**; they differ only in the partial register LUTs
(`0x20`–`0x24`) and analog bytes. The distinguishing knob is the partial **phase
length** (second LUT byte) plus VCOM_DC (`0x82`) / interval (`0x50`).

| Key | Label | Partial phase | `0x82`/`0x50`/`0x30` | Source |
|---|---|---|---|---|
| `uc8151d_waveshare` | Waveshare 2.9″ V2 — UC8151D (default) | `0x19` (25) | `0x12`/`0x97`/`0x3a` | Waveshare `epd2in9d.py` |
| `uc8151d_gdew029i6fd` | GDEW029I6FD flexible (faster partial) | `0x10` (16) | `0x08`/`0x17`/skip | GxEPD2 `GxEPD2_290_I6FD` |
| `uc8151d_t5d` | T5D / LILYGO (longer partial) | `0x20` (32) | `0x08`/`0x17`/skip | GxEPD2 `GxEPD2_290_T5D` |
| `uc8151d_gdew029m06` | GDEW029M06 (experimental balanced-charge) | `0x19` (25), short LUTs | `0x12`/`0x17`/`0x3c` | GxEPD2 `GxEPD2_290_M06` (author-labelled experimental) |

The `uc8151d_waveshare` default emits the stock partial sequence byte-for-byte
(pinned by `DefaultProfilePreservesStockPartialTests` in
`tests/test_epd_uc8151d.py`), so a working V2 panel is unchanged. I6FD/T5D **skip
the PLL write** (`0x30`), matching GxEPD2's partial init exactly.

**Built-In behavior:** if the OTP holds a valid waveform the image renders
(possibly cleaner) but all updates are full-screen flashes; if the OTP is
blank/generic (common on raw panels) it may render blank or garbage. No damage
risk — OTP is factory data and nothing is written to OTP.

### High contrast (experimental toggle)
Orthogonal to the profile. For the `ssd1680`/`dke_ssd1680` drivers it overrides
the source (`0x04`) and VCOM (`0x2C`) registers **after** the profile's LUT, so
it is the final word on voltage. The `il3820` driver has **no** `0x04` register,
so high contrast there instead raises VCOM inline (`0x2C=0x44` vs the `0xA8`
default). On the **UC8151D** driver it bumps VCOM_DC (`0x82`) by
`UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA = 0x08`, clamped to the 6-bit field max
`0x3F` (e.g. the default `0x12` → `0x1A`) — a more-negative VCOM_DC to darken a
faint partial. Register deltas vs. the `WS_20_30` trailing bytes (`ssd1680`):

| Register | Default (`WS_20_30`) | High contrast | Delta |
|---|---|---|---|
| `0x04` VSH1 | `0x41` | `0x4A` | +9 codes |
| `0x04` VSH2 | `0x00` | `0x00` | none |
| `0x04` VSL | `0x32` | `0x3A` | +8 codes |
| `0x2C` VCOM | `0x36` | `0x44` | +14 codes |

> The code→voltage mapping has **not** been verified against the SSD1680/E029A01
> datasheet, so no volt figures are quoted. The values are an on-hardware tuning
> starting point, not a datasheet guarantee — which is why this is surfaced as an
> experimental toggle.

- **Cosmetic/reversible:** darker output, possible over-saturation/blooming, and
  worse ghosting (waveform timing was tuned for the original voltages). Usually
  clears after turning it off and running a few full refreshes.
- **Real risk if left on:** if these exceed the panel's rated source/VCOM
  limits, sustained operation is out-of-spec; the VCOM shift in particular is the
  classic cause of DC imbalance → accelerated aging / persistent burn-in.
- **Guidance:** safe to enable **briefly** to observe; do not leave running on a
  panel you care about.

It composes with any profile (including `builtin_otp` and `depg0290bs`, where the
register overrides are written best-effort over an OTP-driven full refresh — the
least-characterized combination).

## Remote test procedure (for Panel 3)

1. Select a profile in the display-tuning card (it applies immediately, no reboot).
2. Photograph the panel (splash + a position).
3. Try the next profile; then try each with **High contrast** on.
4. Report which profile (and whether high contrast) yields a clean, solid image.

`builtin_otp` is the lower-risk, higher-information experiment to try first;
high contrast should be enabled only briefly.

## Three-color (red/white/black) mode

Some 2.9" panels are tri-color **BWR**. Both controller families have a BWR
variant and both drivers implement three-color mode, because tri-color is a
property of the **panel**, not the controller:

- **UC8151D** (e.g. `GDEH029Z13` / `GDEW029Z13`): command `0x10` is the
  black/white channel and `0x13` is the **red** channel — but the mono partial
  path uses `0x10`/`0x13` as old/new B/W RAM.
- **SSD1680** (the panel in use at `192.168.20.116`): command `0x24` is the
  black/white RAM and `0x26` is the **red** RAM — but the mono partial path uses
  `0x24`/`0x26` as new/old B/W RAM.

In both cases the mono partial writes a B/W frame into the panel's red RAM, which
is why a mono driver bleeds the board's black into red on a BWR panel. Three-color
mode is an opt-in `[display] three_color` switch that fixes the channel mapping
for whichever driver is live and adds red highlighting.

**Hardware facts**

- Red ink can only change with a **full** tri-color refresh (~12–15 s); the front
  ITO is one electrode, so red cannot be partial-refreshed.
- The B/W layer can still refresh fast on a BWR panel using the register B/W LUTs
  with the red LUT left unloaded (red "muted") — the technique used for the fast
  path.

**Architecture (additive, non-invasive)**

- A parallel 1-bit **red mask** plane (`0` = red, `255` = not red) is composited
  alongside the unchanged B/W plane. Widgets opt in via `Widget.render_red`; the
  `Manager` builds the red plane only when the driver reports `three_color`, so a
  mono panel pays zero cost.
- **Hybrid scheduler:** no red on screen → fast B/W refresh (`DisplayPartial`);
  red appears/changes/clears, or any explicit full refresh → `display_color`
  (full tri-color). The mono full path is never used in three-color mode (it
  would write B/W to the red channel). Clearing red forces one full refresh to
  erase the bistable red ink, then fast refreshes resume.
- **Red wins:** any red pixel is forced white in the B/W buffer so a pixel is
  never driven both black and red.

**What is highlighted in red:** the checked king and the checking piece; else a
threatened queen and its attacker; the game-over result line; and the losing-side
(negative) bars of the evaluation graph. The web mirror composes a white/black/red
RGB preview so the dashboard shows red too.

**Bring-up unknowns (verified on hardware at the panel).** The byte layout and
channel routing are unit-tested, but these BWR-specific constants are finalized on
the board. The live panel is SSD1680, so the SSD1680 driver is the one to tune
(`SSD1680_BWR_RED_INVERTED` in `epd2in9_ssd1680.py`; the UC8151D equivalents are
`UC8151D_BWR_PANEL_SETTING` / `UC8151D_BWR_RED_INVERTED` in `epd2in9d.py`):

- **SSD1680 waveform (confirmed on hardware):** the OTP waveform (`0x22 = 0xF7`)
  loads the image then erases it to blank on this panel. The **register LUT**
  (`0xC7`, the default profile) produces a stable image. Three-color therefore
  reuses the profile's normal init + register-LUT activation and only changes the
  channel mapping (B/W → `0x24`, red-only plane → `0x26`); it does **not** force
  OTP. The red electrode is driven by the same register LUT that already produced
  visible (faint) red in mono.
- **SSD1680 red polarity (confirmed):** the mono driver wrote the image
  (black = `0` bit) to `0x26` and produced red where the image was black, so a
  **cleared** bit in `0x26` is red (active-LOW) — the same polarity as the red
  mask, hence `SSD1680_BWR_RED_INVERTED = False`. (Inverting it produced the
  earlier solid-red, but that was under the abandoned OTP regime.)
- UC8151D only: the panel-setting byte (`0x00`) selecting the BWR OTP waveform
  (`0x0F`).
- that a fast B/W refresh does not visibly disturb existing red. On SSD1680 the
  fast path writes B/W to `0x24` only and leaves the red RAM (`0x26`) untouched;
  if red still ghosts, the fallback is a full refresh whenever red is on screen
  (still fast when no red — only the OTP profile forces full always).

## Open questions / next steps

- Confirm the **controller** of `E029A01T1956C0` (SSD1680 vs SSD1608/IL3820 vs
  other) — currently inferred, not confirmed.
- Determine whether its OTP holds a usable waveform (resolves `otp_waveform`'s
  viability).
- Obtain the `E029A01`/`E029A01T1956C0` datasheet to replace the experimental
  high-contrast codes with datasheet-rated VSH/VSL/VCOM values.
- Add new vendor profiles from credited published tables as they are obtained
  (the registry is built for this; see its "Data integrity" note).
- If a profile is confirmed to fix Panel 3 *and* verified non-harmful on Panel 2,
  consider promoting it to a revision-detected default rather than a manual choice.

## Key source files

- `src/universalchess/epaper/framework/waveshare/epd2in9d.py` — UC8151D (V2);
  consumes a UC8151D `WaveformProfile` + `high_contrast`, with `apply_profile()`
  for live re-selection. Exposes `CONTROLLER` for profile resolution.
- `src/universalchess/epaper/framework/waveshare/epd2in9_ssd1680.py` — SSD1680
  (V1); consumes a `WaveformProfile` + `high_contrast`, with `apply_profile()`
  for live re-selection.
- `src/universalchess/epaper/framework/waveshare/waveform_profiles.py` — the
  attributed profile registry (per-controller LUT data + metadata).
- `src/universalchess/epaper/framework/manager.py` (`apply_waveform_profile`,
  `epd` property) and `.../scheduler.py` (`force_reinit`) — live re-init + full
  refresh.
- `src/universalchess/board/display_selection.py` — primary→fallback selection.
- `src/universalchess/app/display_boot.py` (`init_display`,
  `read_display_selection`) — startup wiring and controller selection.
- `src/universalchess/app/board_app.py` (`_on_board_command`,
  `_process_pending_display_profile`) — per-active-controller profile
  resolution + live-apply handling.
- `src/universalchess/web/app.py` (`/api/system/display-tuning`,
  `_display_tuning_available`, `_active_waveform_controller`) — web API +
  visibility gate + active-controller filtering + live-apply command.
- `src/universalchess/web-app/src/pages/Settings.tsx` (`DisplayTuningCard`) — UI
  dropdown + high-contrast toggle + per-controller copy + source credits.
- `src/universalchess/web-app/src/pages/Licenses.tsx` — waveform-source credits.
- `src/universalchess/tests/test_epd_ssd1680.py`,
  `src/universalchess/tests/test_epd_uc8151d.py`,
  `src/universalchess/tests/test_waveform_profiles.py`,
  `src/universalchess/tests/test_debug_endpoints.py` — tests.
