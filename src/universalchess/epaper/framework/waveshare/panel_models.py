"""Selectable 2.9\" replacement panels.

The Centaur connector carries one glass at a time, and the controller behind
it is not detectable from a safe command. Probing the wrong command set drives
the panel with another part's voltages, so a replacement module is an explicit
choice. ``auto`` keeps the shipped V1/V2 probe.

The chess UI stays 128x296. A 168x384 glass receives that frame scaled at the
driver. Four-color panels (black/white/red/yellow) are driven from the
manufacturer init; the UI still draws black and white, and the existing red
highlights map onto the red ink when three-color mode is on. Yellow is unused.

GDEW029I6FD is a 4-gray flexible panel. When it is the selected glass the
UI draws flat gray and the driver loads the GxEPD2_4G 4-gray waveform
(``GxEPD2_290_I6FD`` lut_*_4G). Every other panel stays 1-bit and dithered.
The 4-gray refresh uses both RAM planes as the gray bits. A later update
that only changes black and white (the clock) loads the 1-bit partial
waveform instead; gray pixels are left undriven. A change to a gray level
runs the 4-gray waveform again.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from . import waveform_profiles as wp

# Stored in [display] panel. ``auto`` is the absent-key default.
PANEL_AUTO = "auto"

# Controller chips, as written to the display-status file and shown on the
# System card. Several glasses share a chip, so the status file also records
# the panel key.
CONTROLLER_UC8151D = "UC8151D"
CONTROLLER_SSD1680 = "SSD1680"
CONTROLLER_SSD1685 = "SSD1685"
CONTROLLER_JD79667 = "JD79667"
CONTROLLER_JD79661 = "JD79661"

# Waveform family of each Centaur controller the Automatic probe can find.
_CENTAUR_WAVEFORM_FAMILY = {
    CONTROLLER_UC8151D: wp.CONTROLLER_UC8151D,
    CONTROLLER_SSD1680: wp.CONTROLLER_SSD16XX,
}

# Logical frame the widgets composite, in the driver's width x height sense
# (short side first, matching the Centaur portrait buffer).
LOGICAL_WIDTH = 128
LOGICAL_HEIGHT = 296


@dataclass(frozen=True)
class PanelModel:
    """One choice in the Display tuning panel dropdown.

    Attributes:
        key: Persisted in ``[display] panel``. Never rename a shipped key.
        label: Dropdown text. English, like the waveform labels, because the
            name is a part number.
        explicit: False only for ``auto``. An explicit model skips the V1/V2
            probe and does not fall through to another controller.
        controller: The controller chip, written to the display-status file on
            a successful init. None for ``auto`` (the probe writes its own).
        driver_module: Driver module under ``epaper.framework.waveshare`` that
            ``display_boot.build_panel_epd`` constructs. Shown on the System card.
        system_model: Panel name on the System card. Empty for ``auto``, whose
            name depends on the controller the probe found.
        pinned_profile: Waveform key written when this panel is selected, so
            the matching partial/OTP recipe is the one that runs. None when the
            driver has a single manufacturer init and no waveform table.
        waveform_controller: Family passed to ``get_profile`` (``uc8151d`` or
            ``ssd16xx``). None when the waveform dropdown does not apply.
        native_width: Pixels across the short side, in the driver orientation.
        native_height: Pixels along the long side.
        supports_partial: False when the panel has no fast partial refresh.
            The scheduler then draws one full refresh of the latest frame.
        three_color_supported: Whether the glass has red ink. Only then is the
            Three-color switch offered, saved on, or passed to the driver;
            the Centaur V1/V2 glass is black and white.
        high_contrast_supported: Whether the experimental voltage toggle has
            a register to write on this controller.
        source: Attribution for the init sequence.
        url: Link to that source.
        gray_levels: 4 only for GDEW029I6FD. Every other choice stays 1-bit.
            A gray-level change loads the 4-gray waveform. A black-and-white
            change, including the clock, uses the fast partial.
        partial_only_supported: Whether the card offers a switch that skips
            the slow waveform and partial-refreshes every update. While that
            switch is on, shades are dithered.
    """

    key: str
    label: str
    explicit: bool
    controller: Optional[str]
    driver_module: str
    system_model: str
    pinned_profile: Optional[str]
    waveform_controller: Optional[str]
    native_width: int
    native_height: int
    supports_partial: bool
    three_color_supported: bool
    high_contrast_supported: bool
    source: str
    url: str
    gray_levels: int = 1
    partial_only_supported: bool = False

    @property
    def waveform_applicable(self) -> bool:
        """Whether the waveform dropdown has a table this driver can load."""
        return self.waveform_controller is not None

    @property
    def event_label(self) -> str:
        """Part number and controller, for the Settings diagnostics log."""
        return f"{self.key.upper()} ({self.controller})"

    @property
    def panel_mode(self) -> str:
        """Copy key for the tuning card: ``auto``, ``gray``, ``tunable``, ``bwr``, or ``fixed``."""
        if not self.explicit:
            return "auto"
        if self.gray_levels == 4:
            return "gray"
        # Tri-color glass with a fast black/white partial (GDEY029Z95), with
        # or without a waveform dropdown. The four-color panels also support
        # red ink, but they have no partial.
        if self.three_color_supported and self.supports_partial:
            return "bwr"
        if self.waveform_applicable:
            return "tunable"
        return "fixed"


_GXEPD2 = "https://github.com/ZinggJM/GxEPD2"
_GOOD_DISPLAY_F52 = "https://www.good-display.com/product/742.html"

_PANELS: Tuple[PanelModel, ...] = (
    PanelModel(
        key=PANEL_AUTO,
        label="Automatic (Centaur V1 / V2)",
        explicit=False,
        controller=None,
        driver_module="",
        system_model="",
        pinned_profile=None,
        waveform_controller=None,
        native_width=LOGICAL_WIDTH,
        native_height=LOGICAL_HEIGHT,
        supports_partial=True,
        three_color_supported=False,
        high_contrast_supported=True,
        source="Universal Chess V1/V2 probe",
        url="",
    ),
    PanelModel(
        key="gdew029i6fd",
        label="GDEW029I6FD — 296×128 flexible, 4 gray",
        explicit=True,
        controller=CONTROLLER_UC8151D,
        driver_module="epd2in9d",
        system_model='Good Display GDEW029I6FD 2.9" flexible (UC8151D)',
        pinned_profile="uc8151d_gdew029i6fd",
        waveform_controller=None,
        native_width=LOGICAL_WIDTH,
        native_height=LOGICAL_HEIGHT,
        supports_partial=True,
        three_color_supported=False,
        high_contrast_supported=False,
        source="GxEPD2_4G class GxEPD2_290_I6FD (Jean-Marc Zingg), Good Display GDEW029I6FD demo",
        url="https://github.com/ZinggJM/GxEPD2_4G",
        gray_levels=4,
        partial_only_supported=True,
    ),
    PanelModel(
        key="gdey029t94",
        label="GDEY029T94 — 296×128, SSD1680, fast refresh",
        explicit=True,
        controller=CONTROLLER_SSD1680,
        driver_module="epd2in9_ssd1680",
        system_model='Good Display GDEY029T94 2.9" (SSD1680)',
        pinned_profile="gdey029t94",
        waveform_controller="ssd16xx",
        native_width=LOGICAL_WIDTH,
        native_height=LOGICAL_HEIGHT,
        supports_partial=True,
        three_color_supported=False,
        high_contrast_supported=True,
        source="GxEPD2 class GxEPD2_290_GDEY029T94 (Jean-Marc Zingg), Good Display GDEY029T94 demo",
        url=_GXEPD2,
    ),
    PanelModel(
        key="gdey029z95",
        label="GDEY029Z95 — 296×128, tri-color, partial 1.5 s",
        explicit=True,
        controller=CONTROLLER_SSD1680,
        driver_module="epd2in9_ssd1680",
        system_model='Good Display GDEY029Z95 2.9" tri-color (SSD1680)',
        pinned_profile="gdey029z95",
        # The Centaur SSD1680 waveforms also drive this glass, three-color
        # included; the vendor recipe stays the default.
        waveform_controller="ssd16xx",
        native_width=LOGICAL_WIDTH,
        native_height=LOGICAL_HEIGHT,
        supports_partial=True,
        three_color_supported=True,
        high_contrast_supported=False,
        source="Good Display GDEY029Z95 Arduino demo (AU-GDEY029Z95, SSD1680)",
        url="https://www.good-display.com/product/527.html",
    ),
    PanelModel(
        key="gdey029t71h",
        label="GDEY029T71H — 384×168, SSD1685, black/white partial",
        explicit=True,
        controller=CONTROLLER_SSD1685,
        driver_module="epd2in9_ssd1685",
        system_model='Good Display GDEY029T71H 2.9" (SSD1685)',
        pinned_profile=None,
        waveform_controller=None,
        native_width=168,
        native_height=384,
        supports_partial=True,
        three_color_supported=False,
        high_contrast_supported=False,
        source="GxEPD2 class GxEPD2_290_GDEY029T71H (Jean-Marc Zingg), Good Display GDEY029T71H demo",
        url=_GXEPD2,
    ),
    PanelModel(
        key="gdey029f51h",
        label="GDEY029F51H — 384×168, four-color, full refresh ~25 s",
        explicit=True,
        controller=CONTROLLER_JD79667,
        driver_module="epd2in9_jd7966x",
        system_model='Good Display GDEY029F51H 2.9" four-color (JD79667)',
        pinned_profile=None,
        waveform_controller=None,
        native_width=168,
        native_height=384,
        supports_partial=False,
        three_color_supported=True,
        high_contrast_supported=False,
        source="GxEPD2 class GxEPD2_290c_GDEY029F51H (Jean-Marc Zingg), Good Display GDEY029F51H demo",
        url=_GXEPD2,
    ),
    PanelModel(
        key="gdey029f52",
        label="GDEY029F52 — 296×128, four-color, fast refresh ~11 s",
        explicit=True,
        controller=CONTROLLER_JD79661,
        driver_module="epd2in9_jd7966x",
        system_model='Good Display GDEY029F52 2.9" four-color (JD79661)',
        pinned_profile=None,
        waveform_controller=None,
        native_width=LOGICAL_WIDTH,
        native_height=LOGICAL_HEIGHT,
        supports_partial=False,
        three_color_supported=True,
        high_contrast_supported=False,
        source="Good Display GDEY029F52 Arduino demo (AU-GDEY029F52, JD79661)",
        url=_GOOD_DISPLAY_F52,
    ),
)

_BY_KEY = {panel.key: panel for panel in _PANELS}


def all_panels() -> Tuple[PanelModel, ...]:
    """Every panel choice, Automatic first."""
    return _PANELS


def get_model(key: str) -> PanelModel:
    """Return the model for ``key``, or Automatic when the key is unknown.

    A blank or stale stored value must keep the V1/V2 probe. Raising would
    leave a board that cannot boot its display.
    """
    return _BY_KEY.get((key or "").strip(), _BY_KEY[PANEL_AUTO])


def is_known_panel(key: str) -> bool:
    """Whether ``key`` is a persisted panel choice, including Automatic."""
    return (key or "").strip() in _BY_KEY


def waveform_for(model: PanelModel, stored_key: str,
                 centaur_family: Optional[str]) -> Optional["wp.WaveformProfile"]:
    """The waveform ``model`` runs with ``stored_key`` saved; the boot and live paths share it.

    Automatic resolves the stored key within ``centaur_family``, the family
    of the Centaur controller the probe found, and never loads a
    replacement panel's recipe. A replacement panel with a waveform dropdown
    uses the stored key when it may run there, so a waveform chosen after the
    panel survives a reboot; otherwise it runs its pinned recipe. None when the
    panel's driver has no waveform table (GDEY029T71H, the four-color glasses).
    """
    if not model.explicit:
        return wp.get_profile(stored_key, centaur_family)
    if model.waveform_applicable and wp.is_known_profile(
            stored_key, model.waveform_controller, panel=model.key):
        return wp.get_profile(stored_key, model.waveform_controller, panel=model.key)
    if model.pinned_profile is None:
        return None
    return wp.get_profile(model.pinned_profile, panel=model.key)


def waveform_family(panel_key: Optional[str], controller: Optional[str]) -> Optional[str]:
    """Waveform family offered for a panel reported as ``panel_key`` on ``controller``.

    A replacement panel's family is its own dropdown family, None when it has
    no dropdown. Automatic maps the Centaur controller the probe found. A
    status file from before the panel key was recorded reads as Automatic.
    """
    model = get_model(panel_key or PANEL_AUTO)
    if model.explicit:
        return model.waveform_controller
    return _CENTAUR_WAVEFORM_FAMILY.get(controller)


class PanelDriver:
    """Capabilities the scheduler, manager and board read from the live driver.

    Each driver takes its panel model in the constructor, so a driver built by
    the Automatic probe states the Centaur glass and one built for a
    replacement glass states that glass. None of these is set from outside.
    """

    panel: PanelModel = _BY_KEY[PANEL_AUTO]
    PANEL_KEY: str = PANEL_AUTO
    supports_partial: bool = True
    gray_levels: int = 1
    partial_only_supported: bool = False
    partial_only: bool = False

    def _adopt_panel(self, panel: Optional[PanelModel], partial_only: bool = False) -> None:
        """Record ``panel`` (Automatic when None) and the Partial refresh only switch.

        The switch is honored only on a panel that offers it, and turns its
        gray off, so a leftover setting cannot change another glass.
        """
        self.panel = panel if panel is not None else _BY_KEY[PANEL_AUTO]
        self.PANEL_KEY = self.panel.key
        self.supports_partial = self.panel.supports_partial
        self.partial_only_supported = self.panel.partial_only_supported
        self._set_partial_only(partial_only)

    def _set_partial_only(self, enabled: bool) -> None:
        self.partial_only = self.partial_only_supported and bool(enabled)
        self.gray_levels = 1 if self.partial_only else self.panel.gray_levels


def panels_metadata() -> list:
    """Serializable panel list for the web API (no init bytes)."""
    return [
        {
            "key": panel.key,
            "label": panel.label,
            "source": panel.source,
            "url": panel.url,
            "explicit": panel.explicit,
            "supports_partial": panel.supports_partial,
            "native_width": panel.native_width,
            "native_height": panel.native_height,
        }
        for panel in _PANELS
    ]
