"""Tests for Pegasus LED brightness sourcing and the override switch.

Why these tests exist
---------------------
Capture of live DGT Chess app traffic showed the app transmits a *constant*
intensity byte in every Pegasus LED packet regardless of any user setting -- the
app exposes no LED brightness control (confirmed against the real board and the
reverse-engineered Pegasus protocol, which treats intensity as a near-constant).
The old emulator ran that constant through a fabricated ``11 - intensity_in``
formula plus 0/1 special-cases, so Pegasus LEDs were effectively pinned and never
tracked UC's own brightness setting.

The fix drives Pegasus LEDs from UC's 1-10 setting by default, behind an
"override Pegasus brightness" switch (default on) so the passthrough can be
restored if a future DGT app ever varies the transmitted value. These tests pin:
  - the pure level-resolution contract (override on -> UC setting; off -> app
    value, clamped, with no inversion),
  - that led_control() actually sources the level through that contract, and
  - the setting's default (on) and persistence shape.
"""

from __future__ import annotations

import pytest

from universalchess.emulators import pegasus as pegasus_module
from universalchess.emulators.pegasus import Pegasus
from universalchess.players.settings import GameSettings
from universalchess.utils.led import dgt_intensity_to_uc, resolve_pegasus_intensity

# DGT app brightness (1 dim .. 4 full) -> UC level (1 dim .. 10 bright). Both run
# the same direction, so it is a straight linear scale of the endpoints. Source:
# goneill Pegasus driver ReadMe ("1 is quite dim ... maximum value of 4 is full
# brightness"). This is the contract the wire path depends on.
DGT_TO_UC = {1: 1, 2: 4, 3: 7, 4: 10}


@pytest.mark.parametrize("app_intensity", [0, 1, 5, 10, 200])
def test_override_on_uses_uc_setting_ignoring_app(app_intensity: int) -> None:
    """With override on, the UC 1-10 setting wins and the app value is ignored.

    The app sends a fixed constant, so honoring it (any value of app_intensity)
    would pin brightness. If a regression let the app value leak through, the
    result would vary with app_intensity instead of staying at the UC setting (7),
    which this catches by sweeping unrelated app values against a fixed expectation.
    """
    assert resolve_pegasus_intensity(app_intensity, override=True, uc_intensity=7) == 7


@pytest.mark.parametrize("dgt_value,expected", sorted(DGT_TO_UC.items()))
def test_dgt_intensity_maps_to_uc_scale(dgt_value: int, expected: int) -> None:
    """Each DGT 1-4 level maps to its UC 1-10 level on the shared (higher=brighter) scale.

    Regression guard for the exact curve: DGT 1->1 (dim) and 4->10 (full). If the
    map were inverted (treating DGT 1 as brightest) 1 would land at 10 and this
    fails, catching a direction flip; a wrong slope shifts the mid values (2/3).
    """
    assert dgt_intensity_to_uc(dgt_value) == expected


@pytest.mark.parametrize(
    "dgt_value,expected",
    [(0, 1), (-5, 1), (5, 10), (11, 10), (200, 10)],
)
def test_dgt_intensity_clamps_out_of_range_before_mapping(dgt_value: int, expected: int) -> None:
    """Values outside DGT 1-4 clamp to the nearest end, then map.

    The app has been observed to transmit 5 (above the documented max of 4), so
    the mapping must clamp to 4 -> UC 10 rather than extrapolate past full
    brightness or emit an out-of-domain UC level. A regression dropping the clamp
    would push 5/200 above UC 10.
    """
    assert dgt_intensity_to_uc(dgt_value) == expected


@pytest.mark.parametrize("app_intensity,expected", [(1, 1), (2, 4), (3, 7), (4, 10), (5, 10)])
def test_override_off_translates_dgt_scale_to_uc(app_intensity: int, expected: int) -> None:
    """With override off, the app's DGT value is translated to the UC scale (not passed raw).

    Guards two regressions at once: the deleted ``11 - intensity_in`` inversion
    (which would map 1->0/off) and a raw passthrough (which would leave 5 as UC 5
    instead of the translated UC 10). The uc_intensity here (3) must be ignored on
    the off path.
    """
    assert resolve_pegasus_intensity(app_intensity, override=False, uc_intensity=3) == expected


@pytest.mark.parametrize("uc_intensity,expected", [(0, 1), (11, 10)])
def test_override_on_clamps_uc_setting_defensively(uc_intensity: int, expected: int) -> None:
    """The UC path also clamps to 1-10 as defense in depth.

    get_led_intensity_from_settings already clamps, so this is belt-and-braces: a
    stray 0/11 reaching the resolver must still yield a valid level, not 0/off.
    """
    assert resolve_pegasus_intensity(5, override=True, uc_intensity=uc_intensity) == expected


class _FakeBoard:
    """Records the intensity handed to each board LED call, for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def led(self, num, intensity, speed, repeat) -> None:
        self.calls.append({"op": "single", "num": num, "intensity": intensity})

    def ledArray(self, squares, intensity, speed, repeat) -> None:
        self.calls.append({"op": "array", "squares": squares, "intensity": intensity})

    def ledsOff(self) -> None:
        self.calls.append({"op": "off"})


def _single_field_led_payload(app_intensity: int) -> list:
    """Mode-5 LED payload lighting exactly one field at the given app intensity.

    Layout: [mode=5, speed, mode_flag, intensity, field]. One field routes to
    board.led (not ledArray), so a single call carries the resolved intensity.
    """
    return [5, 7, 0, app_intensity, 10]


def _drive_led(monkeypatch, *, override: bool, uc_intensity: int, app_intensity: int) -> _FakeBoard:
    """Run led_control against a fake board with the two settings helpers stubbed.

    Enters through the public led_control (the highest level that owns the
    sourcing decision) with board + settings replaced at the module boundary, so
    the assertion observes the intensity the emulator actually chose.
    """
    fake_board = _FakeBoard()
    monkeypatch.setattr(pegasus_module, "board", fake_board)
    monkeypatch.setattr(pegasus_module, "get_pegasus_override_brightness", lambda: override)
    monkeypatch.setattr(pegasus_module, "get_led_intensity_from_settings", lambda: uc_intensity)

    Pegasus().led_control(_single_field_led_payload(app_intensity))
    return fake_board


def test_led_control_uses_uc_setting_when_override_on(monkeypatch) -> None:
    """led_control drives the LED at the UC setting when override is on.

    The app sends intensity 5 (its constant); with override on the LED must be
    lit at the UC setting (8), proving the emulator ignores the app constant. A
    regression that honored the app value would show intensity 5 here.
    """
    fake_board = _drive_led(monkeypatch, override=True, uc_intensity=8, app_intensity=5)
    single_calls = [c for c in fake_board.calls if c["op"] == "single"]
    assert single_calls, "expected exactly one board.led call for a one-field packet"
    assert single_calls[0]["intensity"] == 8


def test_led_control_honors_app_value_when_override_off(monkeypatch) -> None:
    """With override off, led_control uses the DGT->UC-translated app value, not UC.

    This is the escape hatch for a future app that varies brightness: override off
    must honor the app value (DGT 5, clamped to 4 -> UC 10) and ignore the UC
    setting (8). A regression that always used UC would show 8; the deleted
    ``11 - intensity_in`` would show 6; a raw passthrough would show 5.
    """
    fake_board = _drive_led(monkeypatch, override=False, uc_intensity=8, app_intensity=5)
    single_calls = [c for c in fake_board.calls if c["op"] == "single"]
    assert single_calls, "expected exactly one board.led call for a one-field packet"
    assert single_calls[0]["intensity"] == 10


def test_game_settings_defaults_override_on() -> None:
    """The new setting defaults to on so Pegasus tracks UC brightness out of the box.

    The whole point is that a fresh install has Pegasus honor the UC setting; if
    the default flipped to off, Pegasus would silently revert to the pinned app
    constant. Also asserts the key is serialized so the web/board can read+persist it.
    """
    settings = GameSettings(section="Game")
    assert settings.pegasus_override_brightness is True
    assert settings.to_dict()["pegasus_override_brightness"] is True
