"""Waveform profiles for both DGT Centaur 2.9" e-paper controllers.

A *waveform profile* is the self-contained recipe that tells a panel how to move
its pixels: a *controller family* (``CONTROLLER_*``) and *driver strategy*
(``DRIVER_*``) plus its look-up tables (LUTs) -- or an instruction to use the
panel's own factory waveform stored in OTP.

Two controller families ship, one per board generation / panel swap:

* ``CONTROLLER_SSD16XX`` -- the V1-family fallback driver (``epd2in9_ssd1680``).
  The V1 family does not share one protocol, so the ``DRIVER_*`` strategy selects
  the init sequence, LUT format and activation bytes (SSD1680 159-byte LUTs,
  IL3820 30-byte LUTs, or DEPG0290BS OTP-full + 153-byte register partial).
* ``CONTROLLER_UC8151D`` -- the primary V2 driver (``epd2in9d``). All UC8151D
  variants drive the *full* refresh from OTP and differ only in the *partial*
  register LUT set (0x20-0x24) and the analog VCOM/interval/PLL bytes
  (0x82/0x50/0x30). A ``Uc8151dWaveform`` carries those; full stays OTP for every
  UC8151D profile, which keeps the (already working) V2 full refresh untouched.

Why this exists
---------------
The DGT Centaur 2.9" panel is not one part, and field units have had panels
replaced. The UC8151D (V2) panel works on the primary driver; V1-family panels
(Good Display E029A01 / GDEH029A1 / GDEM029T94, all SSD16xx-class) fall back to
the SSD1680 driver; and a replacement UC8151D *variant* (flexible GDEW029I6FD,
GDEW029M06, LILYGO T5D) can pass the primary driver's BUSY check yet ghost or
render faint with the stock partial waveform. Rather than hard-code one table per
controller, each driver selects a *named, attributed* profile so a panel can be
matched empirically without code changes -- and without disturbing the
known-good default for that controller.

Data integrity
--------------
Every register LUT here is transcribed from a published manufacturer/community
source (credited in ``source``/``url``) -- none are invented. Panel waveform
bytes that resemble real data but are guessed can under/over-drive and damage a
panel, so new profiles must be added only from a verifiable source. The
``high_contrast`` voltage override (applied by the driver, not stored here) is the
one explicitly experimental knob and is labelled as such in the UI.

There is intentionally no synthetic "Default" profile: the entries are named
after the real tables. ``DEFAULT_PROFILE_KEY_BY_CONTROLLER`` selects, per
controller, the profile used when none is configured -- the GDEM029T94 (SSD1680)
and Waveshare ``epd2in9d`` (UC8151D) tables -- each of which reproduces the prior
driver behavior exactly so a working panel is unchanged.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

# --- Waveform look-up tables ------------------------------------------------
# 159 bytes: 153 LUT bytes written via 0x32, then 6 trailing bytes consumed by
# SetLut() for the gate/source/VCOM voltage registers (0x3F/0x03/0x04/0x2C).
#
# Source: Waveshare e-Paper reference driver ``epd2in9_V2.py`` (SSD1680 /
# GDEM029T94 V2). The identical tables are carried by the GxEPD2 Arduino library
# (Jean-Marc Zingg, class GxEPD2_290_T94). Ported verbatim; panel-specific.
WF_PARTIAL_2IN9 = (
    0x0, 0x40, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x80, 0x80, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x40, 0x40, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x80, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0A, 0x0, 0x0, 0x0, 0x0, 0x0, 0x1,
    0x1, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x1, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x0, 0x0, 0x0,
    0x22, 0x17, 0x41, 0xB0, 0x32, 0x36,
)

WS_20_30 = (
    0x80, 0x66, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x40, 0x0, 0x0, 0x0,
    0x10, 0x66, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x20, 0x0, 0x0, 0x0,
    0x80, 0x66, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x40, 0x0, 0x0, 0x0,
    0x10, 0x66, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x20, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x14, 0x8, 0x0, 0x0, 0x0, 0x0, 0x2,
    0xA, 0xA, 0x0, 0xA, 0xA, 0x0, 0x1,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x14, 0x8, 0x0, 0x1, 0x0, 0x0, 0x1,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x1,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x44, 0x44, 0x44, 0x44, 0x44, 0x44, 0x0, 0x0, 0x0,
    0x22, 0x17, 0x41, 0x0, 0x32, 0x36,
)

# --- IL3820 (GDEH029A1) register LUTs ---------------------------------------
# IL3820 uses a *different* waveform format than the SSD1680: a 30-byte LUT
# written via 0x32, with the drive voltages programmed separately by the IL3820
# init (0x0C booster, 0x2C VCOM, 0x3A/0x3B timing) rather than appended to the
# LUT. Lengths and bytes must NOT be mixed with the SSD1680 159-byte tables.
#
# Source: GxEPD2 (Jean-Marc Zingg), class GxEPD2_290 (LUTDefault_full /
# LUTDefault_part), itself from the Good Display IL3820 demo. Transcribed
# verbatim (the leading 0x32 command byte is omitted; the driver issues it).
# Independently corroborated byte-for-byte by Waveshare's v1 epd2in9.py
# (lut_full_update / lut_partial_update) -- two sources agree on these bytes.
IL3820_LUT_FULL = (
    0x50, 0xAA, 0x55, 0xAA, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x1F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
)

IL3820_LUT_PART = (
    0x10, 0x18, 0x18, 0x08, 0x18, 0x18, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x13, 0x14, 0x44, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
)

# --- DEPG0290BS (SSD1680) register partial LUT ------------------------------
# 153 LUT bytes (NO trailing voltage bytes -- this panel drives its full refresh
# from OTP and only loads a register LUT for partial refresh). Distinct from
# WF_PARTIAL_2IN9 (e.g. the repeat byte at index 65 is 0x2 here vs 0x1 there).
#
# Source: GxEPD2 (Jean-Marc Zingg), class GxEPD2_290_BS (lut_partial), itself
# from the Good Display DEPG0290BS demo. Transcribed verbatim.
DEPG0290BS_LUT_PARTIAL = (
    0x0, 0x40, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x80, 0x80, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x40, 0x40, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x80, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0A, 0x0, 0x0, 0x0, 0x0, 0x0, 0x2,
    0x1, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x1, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x0, 0x0, 0x0,
)


# --- UC8151D (V2 / E029A01-family) partial register LUTs --------------------
# UC8151D variants share one partial-LUT *shape*: a 44-byte VCOM LUT (0x20) and
# four 42-byte channel LUTs (WW 0x21, BW 0x22, WB 0x23, BB 0x24). The first byte
# selects the channel polarity (VCOM 0x00, WW 0x00, BW 0x80, WB 0x40, BB 0x00)
# and the second byte is the phase length -- the single knob that distinguishes
# the known panel variants. Full refresh is OTP for every UC8151D profile, so no
# full LUT is stored here.
#
# Source: the Waveshare default set is the reference ``epd2in9d.py`` driver
# (lut_vcom1/lut_ww1/...); the variant sets are GxEPD2 (Jean-Marc Zingg) classes
# GxEPD2_290_I6FD / GxEPD2_290_T5D / GxEPD2_290_M06, themselves from the Good
# Display / LILYGO demos. Transcribed verbatim (the GxEPD2 ``Tx19`` phase macro
# is folded into the bytes below).
def _uc8151d_lut_set(phase: int) -> dict:
    """Build the 5-LUT UC8151D partial set for a given phase length.

    The Waveshare/I6FD/T5D variants are byte-identical apart from the phase
    length (second LUT byte: Waveshare 0x19=25, I6FD 0x10=16, T5D 0x20=32), so
    the shared shape is expressed once here and the only varying byte is passed
    in. Returns the five LUTs (vcom 44 bytes, ww/bw/wb/bb 42 bytes each) keyed by
    the ``Uc8151dWaveform`` field names.
    """
    pad44 = (0x00,) * 38
    pad42 = (0x00,) * 36
    return {
        "vcom": (0x00, phase, 0x01, 0x00, 0x00, 0x01) + pad44,
        "ww": (0x00, phase, 0x01, 0x00, 0x00, 0x01) + pad42,
        "bw": (0x80, phase, 0x01, 0x00, 0x00, 0x01) + pad42,
        "wb": (0x40, phase, 0x01, 0x00, 0x00, 0x01) + pad42,
        "bb": (0x00, phase, 0x01, 0x00, 0x00, 0x01) + pad42,
    }


_UC8151D_WAVESHARE_LUTS = _uc8151d_lut_set(0x19)
_UC8151D_I6FD_LUTS = _uc8151d_lut_set(0x10)
_UC8151D_T5D_LUTS = _uc8151d_lut_set(0x20)

# GDEW029M06: the GxEPD2 author's "experimental ... balanced charge" LUTs --
# short data arrays padded with zeros to the 44/42 register lengths (exactly the
# bytes GxEPD2 sends via ``_writeDataPGM(..., 44 - sizeof(lut))``). T3=25=0x19 is
# the color-change phase; the other phase slots are zero ("this panel doesn't
# seem to need balanced charge"). Marked experimental in the registry below.
_UC8151D_M06_LUTS = {
    "vcom": (0x00, 0x00, 0x00, 0x19, 0x00, 0x01) + (0x00,) * 38,
    "ww": (0x02, 0x00, 0x00, 0x19, 0x00, 0x01) + (0x00,) * 36,
    "bw": (0x48, 0x00, 0x00, 0x19, 0x00, 0x01) + (0x00,) * 36,
    "wb": (0x84, 0x00, 0x00, 0x19, 0x00, 0x01) + (0x00,) * 36,
    "bb": (0x01, 0x00, 0x00, 0x19, 0x00, 0x01) + (0x00,) * 36,
}


# Controller families. Each profile targets exactly one, and the web layer only
# offers the active controller's profiles (a UC8151D table is meaningless on the
# SSD1680 driver and vice versa). Kept as plain strings; the EPD driver classes
# expose a matching ``CONTROLLER`` attribute so ``main`` can resolve the right
# profile for whichever driver actually drove the panel.
CONTROLLER_SSD16XX = "ssd16xx"
CONTROLLER_UC8151D = "uc8151d"


# Driver strategies. The SSD1680 fallback driver dispatches on this to pick the
# init sequence, LUT format, and refresh activation bytes -- because the V1-panel
# family does NOT share one protocol:
#   "ssd1680"     : Waveshare epd2in9_V2 style. Register full+partial LUTs
#                   (159 bytes each: 153 LUT + 6 voltage), full activation 0xC7.
#                   With use_otp=True, full is driven from OTP (0xF7) and partial
#                   falls back to full.
#   "il3820"      : IL3820/GDEH029A1. IL3820 init (no SWRESET), 30-byte LUTs via
#                   0x32, voltages set by init (0x0C/0x2C/0x3A/0x3B), full
#                   activation 0xC4, partial 0x04.
#   "dke_ssd1680" : DEPG0290BS (SSD1680). Full from OTP (0xF7); partial loads a
#                   153-byte register LUT (no voltage bytes), activation 0xCC.
#   "uc8151d"     : UC8151D (V2). Full from OTP; partial loads the 5 register
#                   LUTs (0x20-0x24) carried in ``uc8151d``. Consumed by the
#                   ``epd2in9d`` driver, not the SSD1680 fallback.
DRIVER_SSD1680 = "ssd1680"
DRIVER_IL3820 = "il3820"
DRIVER_DKE_SSD1680 = "dke_ssd1680"
DRIVER_UC8151D = "uc8151d"


@dataclass(frozen=True)
class Uc8151dWaveform:
    """UC8151D partial-refresh recipe: the 5 register LUTs + analog bytes.

    All UC8151D profiles drive the full refresh from OTP, so this holds only the
    partial-refresh state the ``epd2in9d`` driver programs in ``SetPartReg``:

    Attributes:
        vcom: 44-byte VCOM LUT written to register 0x20.
        ww/bw/wb/bb: 42-byte channel LUTs written to 0x21/0x22/0x23/0x24.
        vcom_dc: VCOM_DC setting byte (register 0x82). ``high_contrast`` bumps a
            harder value on top of this (see the driver); the stored value is the
            source's nominal one.
        interval: VCOM-and-data-interval byte (register 0x50).
        pll: PLL/frame-rate byte (register 0x30), or ``None`` to leave the
            controller default in place. GxEPD2's I6FD/T5D partial init does not
            touch 0x30, so those profiles carry ``None`` to match exactly; the
            Waveshare default and M06 set it explicitly.
    """

    vcom: Tuple[int, ...]
    ww: Tuple[int, ...]
    bw: Tuple[int, ...]
    wb: Tuple[int, ...]
    bb: Tuple[int, ...]
    vcom_dc: int
    interval: int
    pll: Optional[int]


def _uc8151d_waveform(luts: dict, vcom_dc: int, interval: int,
                      pll: Optional[int]) -> Uc8151dWaveform:
    """Assemble a :class:`Uc8151dWaveform` from a ``_uc8151d_lut_set`` dict."""
    return Uc8151dWaveform(
        vcom=luts["vcom"], ww=luts["ww"], bw=luts["bw"], wb=luts["wb"],
        bb=luts["bb"], vcom_dc=vcom_dc, interval=interval, pll=pll,
    )


@dataclass(frozen=True)
class WaveformProfile:
    """A named, attributed waveform recipe for the V1-panel fallback driver.

    Attributes:
        key: Stable identifier persisted in ``[display] waveform_profile`` and
            sent over the wire. Never change an existing key (it is stored).
            Unique across *all* controllers (the persisted setting is shared).
        label: Human-readable name shown in the UI dropdown.
        source: Attribution for the waveform data, shown in the UI and the
            Licenses page. Required -- a profile with no provenance must not ship.
        url: Link to the source.
        controller: Controller family (``CONTROLLER_*``) this profile targets.
            The web layer only offers the active controller's profiles, and the
            board only applies a profile whose controller matches the live driver.
        driver: Driver strategy (see DRIVER_* constants) selecting the panel's
            init sequence, LUT format and activation bytes.
        use_otp: ``ssd1680`` only -- drive the full refresh from the panel's OTP
            waveform (0xF7) and route partial refreshes through full. Ignored by
            the other drivers (``dke_ssd1680`` always uses OTP for full).
        full_lut: Driver-specific full-refresh LUT bytes. ``ssd1680``: 159 bytes
            (153 LUT + 6 voltage); ``il3820``: 30 bytes; ``dke_ssd1680``: unused
            (OTP). Empty when the driver drives full from OTP.
        partial_lut: Driver-specific partial-refresh LUT bytes. ``ssd1680``: 159;
            ``il3820``: 30; ``dke_ssd1680``: 153 (no voltage bytes).
        uc8151d: UC8151D partial recipe (5 register LUTs + analog bytes). Set
            only on ``uc8151d`` profiles; ``None`` for the SSD16xx family.
    """

    key: str
    label: str
    source: str
    url: str
    controller: str = CONTROLLER_SSD16XX
    driver: str = DRIVER_SSD1680
    use_otp: bool = False
    full_lut: Tuple[int, ...] = field(default=())
    partial_lut: Tuple[int, ...] = field(default=())
    uc8151d: Optional[Uc8151dWaveform] = None


# Ordered registry. Only verifiable profiles ship; add new ones solely from a
# credited, published source (see module docstring). The GDEM029T94 entry first
# so it is the natural initial selection and the no-config default.
_PROFILES = (
    WaveformProfile(
        key="gdem029t94",
        label="Waveshare 2.9\" V2 — GDEM029T94 (SSD1680)",
        source="Waveshare e-Paper reference driver (also in GxEPD2 by Jean-Marc Zingg)",
        url="https://github.com/waveshareteam/e-Paper",
        driver=DRIVER_SSD1680,
        full_lut=WS_20_30,
        partial_lut=WF_PARTIAL_2IN9,
    ),
    WaveformProfile(
        key="builtin_otp",
        label="Built-In (panel OTP waveform)",
        source="Panel factory waveform (loaded from the controller's OTP)",
        url="",
        driver=DRIVER_SSD1680,
        use_otp=True,
    ),
    WaveformProfile(
        key="il3820_gdeh029a1",
        label="IL3820 / GDEH029A1 (Good Display)",
        source="GxEPD2 class GxEPD2_290 (Jean-Marc Zingg), from the Good Display IL3820 demo",
        url="https://github.com/ZinggJM/GxEPD2",
        driver=DRIVER_IL3820,
        full_lut=IL3820_LUT_FULL,
        partial_lut=IL3820_LUT_PART,
    ),
    WaveformProfile(
        key="depg0290bs",
        label="DEPG0290BS (SSD1680, OTP full + LUT partial)",
        source="GxEPD2 class GxEPD2_290_BS (Jean-Marc Zingg), from the Good Display DEPG0290BS demo",
        url="https://github.com/ZinggJM/GxEPD2",
        driver=DRIVER_DKE_SSD1680,
        partial_lut=DEPG0290BS_LUT_PARTIAL,
    ),
    # --- UC8151D (V2) family. Full refresh is OTP for all; they differ only in
    # the partial register LUTs + analog bytes. The Waveshare entry first so it
    # is the no-config default and reproduces the stock V2 partial exactly.
    WaveformProfile(
        key="uc8151d_waveshare",
        label="Waveshare 2.9\" V2 — UC8151D (default)",
        source="Waveshare e-Paper reference driver epd2in9d.py (UC8151D)",
        url="https://github.com/waveshareteam/e-Paper",
        controller=CONTROLLER_UC8151D,
        driver=DRIVER_UC8151D,
        uc8151d=_uc8151d_waveform(_UC8151D_WAVESHARE_LUTS, vcom_dc=0x12,
                                  interval=0x97, pll=0x3a),
    ),
    WaveformProfile(
        key="uc8151d_gdew029i6fd",
        label="GDEW029I6FD flexible (UC8151D, faster partial)",
        source="GxEPD2 class GxEPD2_290_I6FD (Jean-Marc Zingg), from the Good Display GDEW029I6FD demo",
        url="https://github.com/ZinggJM/GxEPD2",
        controller=CONTROLLER_UC8151D,
        driver=DRIVER_UC8151D,
        uc8151d=_uc8151d_waveform(_UC8151D_I6FD_LUTS, vcom_dc=0x08,
                                  interval=0x17, pll=None),
    ),
    WaveformProfile(
        key="uc8151d_t5d",
        label="T5D / LILYGO (UC8151D, longer partial)",
        source="GxEPD2 class GxEPD2_290_T5D (Jean-Marc Zingg), from the Good Display / LILYGO demo",
        url="https://github.com/ZinggJM/GxEPD2",
        controller=CONTROLLER_UC8151D,
        driver=DRIVER_UC8151D,
        uc8151d=_uc8151d_waveform(_UC8151D_T5D_LUTS, vcom_dc=0x08,
                                  interval=0x17, pll=None),
    ),
    WaveformProfile(
        key="uc8151d_gdew029m06",
        label="GDEW029M06 (UC8151D, experimental balanced-charge)",
        source="GxEPD2 class GxEPD2_290_M06 (Jean-Marc Zingg) — author-labelled experimental",
        url="https://github.com/ZinggJM/GxEPD2",
        controller=CONTROLLER_UC8151D,
        driver=DRIVER_UC8151D,
        uc8151d=_uc8151d_waveform(_UC8151D_M06_LUTS, vcom_dc=0x12,
                                  interval=0x17, pll=0x3c),
    ),
)

_PROFILES_BY_KEY = {p.key: p for p in _PROFILES}

# No-config selection per controller: each reproduces that driver's prior
# behavior exactly so a working panel is unchanged when nothing is configured.
DEFAULT_PROFILE_KEY_BY_CONTROLLER = {
    CONTROLLER_SSD16XX: "gdem029t94",
    CONTROLLER_UC8151D: "uc8151d_waveshare",
}

# Back-compat global default (SSD16xx). Used when no controller is specified --
# e.g. the SSD1680 driver's own no-arg fallback.
DEFAULT_PROFILE_KEY = DEFAULT_PROFILE_KEY_BY_CONTROLLER[CONTROLLER_SSD16XX]


def all_profiles(controller: Optional[str] = None) -> Tuple[WaveformProfile, ...]:
    """Return registered profiles in display order, optionally one controller.

    ``controller`` ``None`` returns every profile; otherwise only those targeting
    that controller family (what the web UI offers for the active panel).
    """
    if controller is None:
        return _PROFILES
    return tuple(p for p in _PROFILES if p.controller == controller)


def get_profile(key: str, controller: Optional[str] = None) -> WaveformProfile:
    """Return the profile for ``key``, falling back to a verified default.

    Falls back rather than raising so a stale or mistyped stored key -- or a key
    belonging to the *other* controller after a panel swap -- can never leave the
    board with no waveform (which would render nothing). When ``controller`` is
    given, a key whose profile targets a different controller is treated as a
    miss and that controller's default is returned; the worst case is the panel
    renders with its own known-good table.
    """
    profile = _PROFILES_BY_KEY.get(key or "")
    if profile is not None and (controller is None or profile.controller == controller):
        return profile
    default_key = (
        DEFAULT_PROFILE_KEY if controller is None
        else DEFAULT_PROFILE_KEY_BY_CONTROLLER[controller]
    )
    return _PROFILES_BY_KEY[default_key]


def is_known_profile(key: str, controller: Optional[str] = None) -> bool:
    """Whether ``key`` names a registered profile (used to validate web input).

    With ``controller`` given, also requires the profile to target that
    controller family, so the UI cannot persist a UC8151D key for an SSD1680
    panel (or vice versa).
    """
    profile = _PROFILES_BY_KEY.get(key)
    if profile is None:
        return False
    return controller is None or profile.controller == controller


def profiles_metadata(controller: Optional[str] = None) -> list:
    """Serializable profile list for the web API (no waveform bytes)."""
    return [
        {"key": p.key, "label": p.label, "source": p.source,
         "url": p.url, "controller": p.controller}
        for p in all_profiles(controller)
    ]
