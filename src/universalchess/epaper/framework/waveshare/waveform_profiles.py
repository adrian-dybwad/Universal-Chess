"""Waveform profiles for the SSD1680 (V1 / E029A01-family) e-paper driver.

A *waveform profile* is the self-contained recipe that tells the panel how to
move its pixels: a *driver strategy* (see the ``DRIVER_*`` constants) plus its
look-up tables (LUTs) -- or an instruction to use the panel's own factory
waveform stored in OTP. The V1 family does not share one protocol, so the
strategy selects the init sequence, LUT format and refresh-activation bytes
(SSD1680 159-byte LUTs, IL3820 30-byte LUTs, or DEPG0290BS OTP-full + 153-byte
register partial).

Why this exists
---------------
The DGT Centaur 2.9" panel is not one part. The UC8151D (V2) panel works on the
primary driver; V1-family panels (Good Display E029A01 / GDEH029A1 / GDEM029T94,
all SSD16xx-class) fall back to the SSD1680 driver. Those panels need different
waveforms/voltages, and one bench panel renders correctly with the GDEM029T94
table while another (the faint ``E029A01T1956C0``) does not. Rather than hard-code
one table, the SSD1680 driver selects a *named, attributed* profile so the panel
can be matched empirically without code changes -- and without disturbing the
known-good default.

Data integrity
--------------
Every register LUT here is transcribed from a published manufacturer/community
source (credited in ``source``/``url``) -- none are invented. Panel waveform
bytes that resemble real data but are guessed can under/over-drive and damage a
panel, so new profiles must be added only from a verifiable source. The
``high_contrast`` voltage override (applied by the driver, not stored here) is the
one explicitly experimental knob and is labelled as such in the UI.

There is intentionally no synthetic "Default" profile: the entries are named
after the real tables. ``DEFAULT_PROFILE_KEY`` selects the GDEM029T94 profile
when no profile is configured, which reproduces the prior driver behavior exactly
so the working bench panel is unchanged.
"""

from dataclasses import dataclass, field
from typing import Tuple

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
DRIVER_SSD1680 = "ssd1680"
DRIVER_IL3820 = "il3820"
DRIVER_DKE_SSD1680 = "dke_ssd1680"


@dataclass(frozen=True)
class WaveformProfile:
    """A named, attributed waveform recipe for the V1-panel fallback driver.

    Attributes:
        key: Stable identifier persisted in ``[display] waveform_profile`` and
            sent over the wire. Never change an existing key (it is stored).
        label: Human-readable name shown in the UI dropdown.
        source: Attribution for the waveform data, shown in the UI and the
            Licenses page. Required -- a profile with no provenance must not ship.
        url: Link to the source.
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
    """

    key: str
    label: str
    source: str
    url: str
    driver: str = DRIVER_SSD1680
    use_otp: bool = False
    full_lut: Tuple[int, ...] = field(default=())
    partial_lut: Tuple[int, ...] = field(default=())


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
)

_PROFILES_BY_KEY = {p.key: p for p in _PROFILES}

# No-config selection: reproduces the prior driver behavior so the working bench
# panel is unchanged when nothing is configured.
DEFAULT_PROFILE_KEY = "gdem029t94"


def all_profiles() -> Tuple[WaveformProfile, ...]:
    """Return every registered profile, in display order."""
    return _PROFILES


def get_profile(key: str) -> WaveformProfile:
    """Return the profile for ``key``, or the default if unknown/empty.

    Falls back to the default rather than raising so a stale or mistyped stored
    key can never leave the board with no waveform (which would render nothing);
    the worst case is the panel renders with the known-good GDEM029T94 table.
    """
    return _PROFILES_BY_KEY.get(key or "", _PROFILES_BY_KEY[DEFAULT_PROFILE_KEY])


def is_known_profile(key: str) -> bool:
    """Whether ``key`` names a registered profile (used to validate web input)."""
    return key in _PROFILES_BY_KEY


def profiles_metadata() -> list:
    """Serializable profile list for the web API (no waveform bytes)."""
    return [
        {"key": p.key, "label": p.label, "source": p.source, "url": p.url}
        for p in _PROFILES
    ]
