"""A ``high_contrast`` key left in the settings file drives no panel.

Why these tests exist:
    Display tuning had a "High contrast (experimental)" switch. It pushed the
    SSD1680 source (0x04) and VCOM (0x2C) registers, the IL3820 VCOM, and the
    UC8151D VCOM_DC (0x82) past the selected waveform's values. The values
    were never checked against a datasheet, and a VCOM shift is the classic
    cause of DC imbalance and burn-in, yet the switch was saved to
    ``[display] high_contrast`` and re-applied at every boot. The switch is
    removed. A board that had it on still has ``high_contrast = True`` on
    disk, and that key must change nothing.

How a regression manifests:
    The register writes recorded with the stored key on differ from the writes
    with it absent: the 0x04 / 0x2C / 0x82 bytes carry the old boosted values,
    so the override is still wired into boot or into the live tuning path.
"""

from unittest.mock import MagicMock, patch


import pytest

from universalchess.app import display_boot
from universalchess.board import display_selection as ds
from universalchess.board import hardware_info
from universalchess.epaper.framework.waveshare import epd2in9_ssd1680, epd2in9d, epdconfig
from universalchess.epaper.framework.waveshare import panel_models as panels
from universalchess.epaper.framework.waveshare import waveform_profiles as wp
from universalchess.services import chromecast

PARTIAL_BUFFER = [0x00] * (128 * 296 // 8)
STORED_ON = "True"
IDLE = 0

# Every panel the switch was offered on, each with the waveform whose voltages
# it overrode: the SSD1680 0x04/0x2C pair, the IL3820 inline VCOM, the UC8151D
# VCOM_DC, and GDEY029T94's own recipe on the SSD1680 driver.
SWITCHED_PANELS = (
    pytest.param(panels.PANEL_AUTO, ds.CONTROLLER_SSD1680, "gdem029t94", id="v1-ssd1680"),
    pytest.param(panels.PANEL_AUTO, ds.CONTROLLER_SSD1680, "il3820_gdeh029a1", id="v1-il3820"),
    pytest.param(panels.PANEL_AUTO, ds.CONTROLLER_UC8151D, "uc8151d_waveshare", id="v2-uc8151d"),
    pytest.param("gdey029t94", ds.CONTROLLER_SSD1680, "gdey029t94", id="gdey029t94"),
)


@pytest.fixture
def stored(monkeypatch):
    """Return a dict standing in for the ``[display]`` section on disk."""
    from universalchess.board.settings import Settings

    values = {}
    monkeypatch.setattr(
        Settings, "read",
        staticmethod(lambda section, key, default=None:
                     values.get(key, default) if section == "display" else default),
    )
    return values


@pytest.fixture(autouse=True)
def idle_panel_pins(monkeypatch):
    """GPIO and timing calls a driver makes around its register writes."""
    monkeypatch.setattr(epdconfig, "module_init", MagicMock(return_value=0))
    monkeypatch.setattr(epdconfig, "digital_read", MagicMock(return_value=IDLE))
    monkeypatch.setattr(epdconfig, "digital_write", MagicMock())
    monkeypatch.setattr(epdconfig, "delay_ms", MagicMock())


def _register_writes(epd):
    """Record every command and data byte the driver sends for init and a partial.

    The partial step is where the SSD1680 re-applied the override and where
    the UC8151D programs VCOM_DC, so both paths are covered.
    """
    writes = []
    epd.send_command = lambda command: writes.append(("cmd", command))
    epd.send_data = lambda data: writes.append(("data", data))
    epd.send_data2 = lambda data: writes.append(("data2", list(data)))
    epd.reset = MagicMock()
    epd.ReadBusy = MagicMock()
    assert epd.init() == 0
    if isinstance(epd, epd2in9d.EPD):
        epd.SetPartReg()
    else:
        epd.DisplayPartial(PARTIAL_BUFFER)
    return writes


class _DriverBuilt(Exception):
    """Stops boot at the panel init, carrying the driver it was about to start."""

    def __init__(self, epd):
        super().__init__()
        self.epd = epd


def _driver_built_at_boot(monkeypatch, controller):
    """Run ``init_display`` up to the first panel init and return that driver.

    The previous boot's status names ``controller``, so Automatic probes it
    first. An explicit panel does not read the status.
    """
    monkeypatch.setattr(chromecast, "discard_epaper_jpg", lambda: None)
    monkeypatch.setattr(
        hardware_info, "read_display_status",
        lambda: {"initialized": True, "active_controller": controller, "panel": panels.PANEL_AUTO},
    )

    def stop_at_init(epd, **_kwargs):
        raise _DriverBuilt(epd)

    monkeypatch.setattr(display_boot, "attempt_display_init", stop_at_init)
    with pytest.raises(_DriverBuilt) as built:
        display_boot.init_display()
    return built.value.epd


@pytest.mark.parametrize("panel, controller, waveform", SWITCHED_PANELS)
def test_boot_drives_the_waveform_voltages_with_the_stored_key_on(
        stored, monkeypatch, panel, controller, waveform):
    """Boot reads the settings file, so it is where a leftover key would act.

    How a regression manifests: the boot driver writes the boosted 0x04 /
    0x2C bytes on V1 and GDEY029T94, 0x44 in place of 0xA8 on IL3820, or a
    raised 0x82 on V2, and the lists differ.
    """
    stored.update(panel=panel, waveform_profile=waveform)
    absent = _register_writes(_driver_built_at_boot(monkeypatch, controller))

    stored["high_contrast"] = STORED_ON
    left_on = _register_writes(_driver_built_at_boot(monkeypatch, controller))

    assert left_on == absent


class _TuningManager:
    """The Manager calls the live display-tuning path makes.

    The waveform is forwarded to the real driver with whatever arguments the
    board passes, so a leftover contrast argument reaches the driver.
    """

    initialized = True

    def __init__(self, epd):
        self.epd = epd

    def apply_waveform_profile(self, profile, *board_arguments):
        self.epd.apply_profile(profile, *board_arguments)

    def apply_three_color(self, enabled):
        pass

    def set_batch_updates(self, enabled):
        pass


def _running_driver(panel, controller, waveform):
    """The driver a board on ``panel`` is running before the tuning change."""
    model = panels.get_model(panel)
    if controller == ds.CONTROLLER_UC8151D:
        return epd2in9d.EPD(profile=panels.waveform_for(model, waveform, wp.CONTROLLER_UC8151D))
    if model.explicit:
        return epd2in9_ssd1680.EPD(profile=panels.waveform_for(model, waveform, None), panel=model)
    return epd2in9_ssd1680.EPD(profile=panels.waveform_for(model, waveform, wp.CONTROLLER_SSD16XX))


def _driver_after_live_change(panel, controller, waveform):
    """Apply a pending display-tuning change and return the driver it reached."""
    from universalchess.app import board_app
    from universalchess.app.pending_work import PendingWork
    from universalchess.board import board

    manager = _TuningManager(_running_driver(panel, controller, waveform))
    pending = PendingWork()
    pending.display_profile.request({})
    with patch.object(board_app, "_pending", pending), \
            patch.object(board, "display_manager", manager, create=True):
        board_app._process_pending_display_profile()
    return manager.epd


@pytest.mark.parametrize("panel, controller, waveform", SWITCHED_PANELS)
def test_a_live_tuning_change_drives_the_waveform_voltages_with_the_stored_key_on(
        stored, panel, controller, waveform):
    """A waveform change made while the board runs re-reads the settings file too.

    How a regression manifests: the board hands the stored key to the
    driver's apply_profile, the next init writes the boosted bytes, and the
    lists differ.
    """
    stored.update(panel=panel, waveform_profile=waveform)
    absent = _register_writes(_driver_after_live_change(panel, controller, waveform))

    stored["high_contrast"] = STORED_ON
    left_on = _register_writes(_driver_after_live_change(panel, controller, waveform))

    assert left_on == absent
