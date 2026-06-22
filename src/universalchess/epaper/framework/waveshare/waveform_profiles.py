"""Waveform profiles for the SSD1680 (V1 / E029A01-family) e-paper driver.

A *waveform profile* is the self-contained recipe that tells the panel how to
move its pixels: the full-refresh look-up table (LUT), the partial-refresh LUT,
and the drive voltages bundled with them -- or an instruction to use the panel's
own factory waveform stored in OTP.

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


@dataclass(frozen=True)
class WaveformProfile:
    """A named, attributed waveform recipe for the SSD1680 driver.

    Attributes:
        key: Stable identifier persisted in ``[display] waveform_profile`` and
            sent over the wire. Never change an existing key (it is stored).
        label: Human-readable name shown in the UI dropdown.
        source: Attribution for the waveform data, shown in the UI and the
            Licenses page. Required -- a profile with no provenance must not ship.
        url: Link to the source.
        use_otp: When True the driver ignores ``full_lut``/``partial_lut`` and
            drives the panel from its built-in OTP waveform. Because no register
            partial LUT exists in this mode, partial refreshes fall back to full
            refreshes (see the driver). Mutually exclusive with the LUT fields.
        full_lut: 159-byte full-refresh table (153 LUT + 6 voltage bytes). Empty
            iff ``use_otp``.
        partial_lut: Partial-refresh table. Empty iff ``use_otp``.
        il3820_additions: Apply the IL3820/SSD1608 analog block on top of the
            base init (booster/dummy-line/gate-width/VCOM) for true IL3820 panels.
    """

    key: str
    label: str
    source: str
    url: str
    use_otp: bool = False
    full_lut: Tuple[int, ...] = field(default=())
    partial_lut: Tuple[int, ...] = field(default=())
    il3820_additions: bool = False


# Ordered registry. Only verifiable profiles ship; add new ones solely from a
# credited, published source (see module docstring). The GDEM029T94 entry first
# so it is the natural initial selection and the no-config default.
_PROFILES = (
    WaveformProfile(
        key="gdem029t94",
        label="Waveshare 2.9\" V2 — GDEM029T94 (SSD1680)",
        source="Waveshare e-Paper reference driver (also in GxEPD2 by Jean-Marc Zingg)",
        url="https://github.com/waveshareteam/e-Paper",
        full_lut=WS_20_30,
        partial_lut=WF_PARTIAL_2IN9,
    ),
    WaveformProfile(
        key="builtin_otp",
        label="Built-In (panel OTP waveform)",
        source="Panel factory waveform (loaded from the controller's OTP)",
        url="",
        use_otp=True,
    ),
    WaveformProfile(
        key="il3820_gdeh029a1",
        label="IL3820 / GDEH029A1 additions",
        source="Good Display / Waveshare IL3820 (GDEH029A1) reference init",
        url="https://github.com/waveshareteam/e-Paper",
        full_lut=WS_20_30,
        partial_lut=WF_PARTIAL_2IN9,
        il3820_additions=True,
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
