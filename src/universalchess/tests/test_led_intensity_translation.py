"""Tests for logical LED brightness (1-10) -> hardware intensity byte (127-1).

These guard the on-wire translation added in ``sync_centaur``: the whole app
carries the user-facing 1-10 brightness level, and only the four LED packet
builders convert it to the DGT controller's raw intensity byte at the last
moment before the wire.

Hardware facts verified on the board (per-square anchor-vs-test LED sweep):
  - The controller honors intensity bytes 1..127 only. Bytes >= 128 are ignored
    (the command is discarded, the previously lit LED persists); they are NOT an
    "off". So a mapped value must never land >= 128.
  - The scale is inverted and roughly logarithmic: byte 1 is brightest, byte 127
    the dimmest still-lit. Level 1 -> 127 (dimmest), level 10 -> 1 (brightest).
"""

from __future__ import annotations

import pytest

from universalchess.board.sync_centaur import SyncCentaur, brightness_to_intensity
from universalchess.utils.led import LED_SPEED_SLOW, LedController

# The geometric curve measured on hardware. Each entry is (level, wire byte).
# This is the contract: if the formula or its constants drift, the mapped byte
# for some level changes and one of these rows fails, pinpointing the level.
EXPECTED_TABLE = {
    1: 127,
    2: 74,
    3: 43,
    4: 25,
    5: 15,
    6: 9,
    7: 5,
    8: 3,
    9: 2,
    10: 1,
}

# Packet layout for the LED builders: [0x05, speed, repeat, intensity, *squares].
_INTENSITY_INDEX = 3
_DEFAULT_SPEED = 3
_DEFAULT_REPEAT = 1


@pytest.mark.parametrize("level,expected_byte", sorted(EXPECTED_TABLE.items()))
def test_mapping_matches_measured_curve(level: int, expected_byte: int) -> None:
    """Each 1-10 level maps to the exact byte measured on the board.

    Regression guard: if the curve constants change, the specific level whose
    byte drifted is the one that fails, rather than a vague whole-suite break.
    """
    assert brightness_to_intensity(level) == expected_byte


def test_endpoints_are_inverted() -> None:
    """Level 1 is dimmest (127), level 10 is brightest (1).

    If the inversion were dropped (level passed straight through), level 1 would
    map to 1 (brightest) and this fails - catching the exact bug this change fixes.
    """
    assert brightness_to_intensity(1) == 127
    assert brightness_to_intensity(10) == 1


def test_strictly_decreasing_across_levels() -> None:
    """Higher level must be strictly brighter (strictly lower byte).

    A non-monotonic curve would make some higher setting dimmer than a lower one;
    the strict check across every adjacent pair catches any such inversion.
    """
    bytes_by_level = [brightness_to_intensity(level) for level in range(1, 11)]
    for lower, higher in zip(bytes_by_level, bytes_by_level[1:]):
        assert higher < lower, f"expected strictly decreasing, got {bytes_by_level}"


def test_all_levels_within_honored_range() -> None:
    """Every mapped byte is 1..127, never an ignored (>=128) or absent (0) value.

    A byte >= 128 would be silently ignored by the firmware (LED unchanged), so
    a mapping that produced one would make a brightness setting do nothing.
    """
    for level in range(1, 11):
        byte = brightness_to_intensity(level)
        assert 1 <= byte <= 127


@pytest.mark.parametrize(
    "out_of_range,expected_byte",
    [(0, 127), (-5, 127), (11, 1), (100, 1)],
)
def test_out_of_range_clamps_to_nearest_valid_end(out_of_range: int, expected_byte: int) -> None:
    """Out-of-range levels clamp to the nearest valid end, never emit a bad byte.

    Upstream already clamps to 1-10, but this is defense in depth: a stray 0 must
    become the dimmest honored byte (127), not 0; a stray >10 the brightest (1).
    A regression that let 0 through would send byte 0, and >10 would risk >=128.
    """
    assert brightness_to_intensity(out_of_range) == expected_byte


def _capture_led_packet(method_name: str, *args, **kwargs) -> bytes:
    """Call a SyncCentaur LED builder on a bare instance, capturing the packet.

    Uses object.__new__ to skip __init__ (which opens serial/threads); the LED
    builders only touch self.sendCommand, which is replaced here to record the
    data payload. Returns the exact bytes the method would hand to the wire.
    """
    device = object.__new__(SyncCentaur)
    captured: dict = {}

    def fake_send(command_name, data=None, timeout: float = 10.0) -> None:
        captured["command"] = command_name
        captured["data"] = bytes(data)

    device.sendCommand = fake_send  # type: ignore[method-assign]
    getattr(device, method_name)(*args, **kwargs)
    return captured["data"]


@pytest.mark.parametrize("level,expected_byte", [(10, 1), (1, 127), (5, 15)])
def test_led_translates_intensity_at_the_wire(level: int, expected_byte: int) -> None:
    """led() converts the 1-10 level to the wire byte in the packet it sends.

    Without translation, data[3] would equal the raw level (e.g. 10) instead of
    the honored byte (1), so the assertion on the whole packet catches both a
    missing translation and any layout shift.
    """
    data = _capture_led_packet("led", 28, intensity=level)
    assert data == bytes([0x05, _DEFAULT_SPEED, _DEFAULT_REPEAT, expected_byte, 28])


def test_ledArray_translates_and_keeps_squares() -> None:
    """ledArray() translates intensity and preserves the square list after it.

    Full-packet assert distinguishes a translation bug (wrong data[3]) from a
    payload-ordering bug (squares dropped or shifted).
    """
    data = _capture_led_packet("ledArray", [0, 63], intensity=10)
    assert data == bytes([0x05, _DEFAULT_SPEED, _DEFAULT_REPEAT, 1, 0, 63])


def test_ledFromTo_translates_and_keeps_endpoints() -> None:
    """ledFromTo() translates intensity and preserves from/to squares.

    Level 1 -> byte 127 with endpoints 0,63 intact; a missing translation would
    leave byte 1 here (brightest) instead of 127 (dimmest).
    """
    data = _capture_led_packet("ledFromTo", 0, 63, intensity=1)
    assert data == bytes([0x05, _DEFAULT_SPEED, _DEFAULT_REPEAT, 127, 0, 63])


def test_ledFlash_translates_intensity() -> None:
    """ledFlash() translates intensity (packet has no square payload).

    Guards that the flash builder shares the same translation as the others.
    """
    data = _capture_led_packet("ledFlash", intensity=10)
    assert data == bytes([0x05, _DEFAULT_SPEED, _DEFAULT_REPEAT, 1])


def test_ledsOff_is_untouched() -> None:
    """ledsOff() stays the raw 0x00 off command with no intensity byte.

    The off command must never be run through the intensity translation; doing so
    would corrupt it into an LED_CMD with a spurious payload.
    """
    data = _capture_led_packet("ledsOff")
    assert data == bytes([0x00])


@pytest.mark.parametrize("method,args", [
    ("led", (28,)),
    ("ledArray", ([0, 63],)),
    ("ledFromTo", (0, 63)),
    ("ledFlash", ()),
])
def test_every_builder_emits_honored_byte_for_all_levels(method: str, args: tuple) -> None:
    """No LED builder ever puts an ignored (>=128) or zero byte on the wire.

    Sweeps all 1-10 levels through each builder; a builder that skipped
    translation would emit the raw level (fine) but a broken curve could emit
    >=128, which the firmware ignores - this catches that class of bug.
    """
    for level in range(1, 11):
        data = _capture_led_packet(method, *args, intensity=level)
        assert 1 <= data[_INTENSITY_INDEX] <= 127


class _FakeBoard:
    """Captures the intensity and speed handed to each board LED call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def ledFromTo(self, lfrom, lto, intensity, speed, repeat) -> None:
        self.calls.append({"op": "from_to", "intensity": intensity, "speed": speed})

    def ledArray(self, squares, speed, intensity, repeat) -> None:
        self.calls.append({"op": "array", "intensity": intensity, "speed": speed})

    def led(self, num, intensity, speed, repeat) -> None:
        self.calls.append({"op": "single", "intensity": intensity, "speed": speed})

    def ledsOff(self) -> None:
        self.calls.append({"op": "off"})


@pytest.mark.parametrize("brightness", list(range(1, 11)))
def test_hint_uses_standard_intensity_no_dimming(brightness: int) -> None:
    """Hint LEDs use the standard brightness (no dimming); only their pulse is slower.

    Suggestion/hint LEDs must be as bright as every other LED -- only the slow
    pulse distinguishes them. If a dimming step were reintroduced (the old
    behavior lowered the level for hints), the intensity handed to the board would
    drop below the configured brightness and this fails; the speed assert pins that
    hints stay on the slow pulse so the two concerns can't be conflated.
    """
    board = _FakeBoard()
    controller = LedController(board, intensity=brightness)
    controller.from_to_hint(0, 1)
    controller.array_hint([0, 1, 2])
    hint_calls = board.calls
    assert hint_calls, "expected hint calls to reach the board"
    for call in hint_calls:
        assert call["intensity"] == brightness
        assert call["speed"] == LED_SPEED_SLOW
