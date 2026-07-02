"""Tests for the app -> board LED command decoder.

These pin the behaviour that lets UC read the stock Centaur software's LED
intensity from the translate-mode serial tap: LED set/off frames must decode to
the exact protocol bytes, frames split across reads must still decode, and
unrelated or corrupt traffic must never fabricate a bogus LED command.
"""

from typing import List

from universalchess.services.centaur_serial.command_decoder import (
    LedCommand,
    LedCommandDecoder,
)
from universalchess.services.centaur_serial.decoder import checksum


def _frame(frame_type: int, payload: bytes, addr1: int = 0x3E, addr2: int = 0x5E) -> bytes:
    """Build a valid app -> board frame mirroring sync_centaur.buildPacket.

    Layout: [type][len_hi][len_lo][addr1][addr2][payload...][csum] with a 14-bit
    total length and checksum = sum(frame[:-1]) % 128.
    """
    total = 1 + 2 + 2 + len(payload) + 1
    header = bytes((frame_type, (total >> 7) & 0x7F, total & 0x7F, addr1, addr2))
    body = header + payload
    return body + bytes((checksum(body),))


def _led_set(intensity: int, squares: List[int], speed: int = 3, repeat: int = 0) -> bytes:
    """A LED "set" frame: sub-command 0x05 then speed, repeat, intensity, squares."""
    return _frame(0xB0, bytes((0x05, speed, repeat, intensity, *squares)))


def _led_off() -> bytes:
    """A LED "off" frame: sub-command 0x00, no further payload."""
    return _frame(0xB0, bytes((0x00,)))


def test_decodes_single_led_set_frame():
    # Guards the core mapping: a set frame must yield the exact on-wire bytes.
    # Regression (e.g. reading the wrong payload offset) manifests as a wrong
    # intensity/square or no command at all.
    decoder = LedCommandDecoder()
    commands = decoder.feed(_led_set(intensity=7, squares=[56], speed=3, repeat=0))
    assert commands == [
        LedCommand(off=False, intensity=7, speed=3, repeat=0, squares=[56])
    ]


def test_decodes_captured_frame_from_hardware_log():
    # Uses a byte-for-byte frame observed on the wire (b0 00 0b 3e 5e 05 03 00 01
    # 38 18) so the decoder is validated against real hardware framing, not just
    # our own builder. Failure means the framing/offset assumptions drifted.
    raw = bytes.fromhex("b0 00 0b 3e 5e 05 03 00 01 38 18".replace(" ", ""))
    commands = LedCommandDecoder().feed(raw)
    assert commands == [
        LedCommand(off=False, intensity=1, speed=3, repeat=0, squares=[0x38])
    ]


def test_decodes_led_off_frame():
    # The off command (sub 0x00) must be distinguished from a set with intensity
    # 0; conflating them would misreport "off" as "intensity 0" and vice versa.
    commands = LedCommandDecoder().feed(_led_off())
    assert commands == [
        LedCommand(off=True, intensity=None, speed=None, repeat=None, squares=[])
    ]


def test_decodes_multi_square_set_frame():
    # A from/to move indication lights two squares in one frame; both must be
    # captured. A regression that reads only the first square would drop the "to".
    commands = LedCommandDecoder().feed(_led_set(intensity=5, squares=[0, 63]))
    assert commands == [
        LedCommand(off=False, intensity=5, speed=3, repeat=0, squares=[0, 63])
    ]


def test_decodes_frame_split_across_two_feeds():
    # The PTY read boundary can fall mid-frame; the decoder must buffer and still
    # decode. Failure manifests as a dropped command when a read splits a frame.
    frame = _led_set(intensity=9, squares=[24])
    decoder = LedCommandDecoder()
    assert decoder.feed(frame[:4]) == []  # partial: nothing yet
    assert decoder.feed(frame[4:]) == [
        LedCommand(off=False, intensity=9, speed=3, repeat=0, squares=[24])
    ]


def test_resyncs_past_garbage_prefix():
    # A late attach can leave stray leading bytes before a real frame; the
    # decoder must drop them (checksum-gated) and still decode the frame. A
    # regression here loses the first command after any noise.
    frame = _led_set(intensity=4, squares=[10])
    commands = LedCommandDecoder().feed(b"\xff\x00\x99" + frame)
    assert commands == [
        LedCommand(off=False, intensity=4, speed=3, repeat=0, squares=[10])
    ]


def test_bad_checksum_frame_is_dropped_then_recovers():
    # A corrupt frame must not be trusted (wrong length would desync); the
    # decoder drops a byte and recovers on the next valid frame. The test
    # corrupts the checksum so the first frame is invalid and asserts only the
    # second, valid frame decodes.
    bad = bytearray(_led_set(intensity=1, squares=[1]))
    bad[-1] ^= 0xFF  # break the checksum
    good = _led_set(intensity=2, squares=[2])
    commands = LedCommandDecoder().feed(bytes(bad) + good)
    assert commands == [
        LedCommand(off=False, intensity=2, speed=3, repeat=0, squares=[2])
    ]


def test_non_led_frame_is_consumed_without_emitting():
    # Poll/other command frames share the framing but are not LED commands; they
    # must be consumed (so framing stays aligned) without producing a command,
    # and a following LED frame must still decode. A regression that mis-handles
    # non-LED frames would either emit a bogus command or desync and lose the LED.
    poll = _frame(0x83, bytes((0x00,)))  # a non-LED command frame
    led = _led_set(intensity=6, squares=[33])
    commands = LedCommandDecoder().feed(poll + led)
    assert commands == [
        LedCommand(off=False, intensity=6, speed=3, repeat=0, squares=[33])
    ]


def test_non_led_0xb0_payload_does_not_fabricate_command():
    # A 0xB0 frame whose payload is neither set (0x05) nor off (0x00) must yield
    # no command rather than a fabricated intensity, per the "never invent a
    # fallback value" rule. Here the sub-command byte is 0x07 (unknown).
    unknown = _frame(0xB0, bytes((0x07, 0x01, 0x02)))
    assert LedCommandDecoder().feed(unknown) == []
