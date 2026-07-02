"""Pure decoder for the Centaur board serial protocol (app -> board direction).

In "translate" mode the original Centaur binary drives the board directly; the
serial tap (see ``relay.py``) forwards the app -> board byte stream verbatim and
hands a copy to this decoder so UC can observe the LED commands Centaur issues
(intensity, speed, squares) without interfering. This is what lets UC learn the
intensity values the stock software actually uses -- the reference this project's
own LED code is calibrated against.

Like :mod:`decoder` (the board -> app direction) this module is intentionally
pure: it takes bytes and yields decoded commands with no serial/threading/IO, so
it is trivially unit-testable and never gates the live forward path.

Command frame (app -> board), mirroring ``sync_centaur.buildPacket``:
  ``[type][len_hi][len_lo][addr1][addr2][payload...][csum]``
  - length is 14-bit: ``((len_hi & 0x7F) << 7) | (len_lo & 0x7F)`` and is the
    TOTAL frame length (header + payload + checksum);
  - ``checksum = sum(frame[:-1]) % 128``.

The LED command is type ``0xB0``; its payload's first byte is the sub-command:
  - ``0x05`` set: ``[0x05][speed][repeat][intensity][square...]``;
  - ``0x00`` off: ``[0x00]`` (all LEDs off).

Framing here anchors on the LED frame type: only LED commands are of interest,
so the decoder resyncs to the next ``0xB0`` byte and accepts a frame only when
its declared length is plausible AND its checksum validates, dropping a byte to
resync otherwise. Anchoring on the type (rather than a full command whitelist,
which is not enumerated for the app -> board direction) avoids misaligned heads on
other command types stalling the decoder, and the checksum gate makes it
overwhelmingly unlikely that unrelated traffic is mistaken for an LED command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from universalchess.services.centaur_serial.decoder import checksum

# LED command frame type and payload sub-commands.
LED_FRAME_TYPE = 0xB0
LED_SET_SUBCOMMAND = 0x05
LED_OFF_SUBCOMMAND = 0x00

# Framing bounds. Header is 5 bytes (type + 2 length + 2 addr) plus a 1-byte
# checksum, so 6 is the smallest possible complete frame (no payload). A declared
# length outside [6, 255] is implausible and forces a resync.
_MIN_DECLARED_LENGTH = 6
_MAX_DECLARED_LENGTH = 255
_HEADER_BYTES = 5
# A set LED payload needs at least sub-command + speed + repeat + intensity.
_MIN_SET_PAYLOAD = 4


@dataclass(frozen=True)
class LedCommand:
    """A decoded LED command sent app -> board.

    ``off`` distinguishes the all-off command (sub-command 0x00) from a set
    command. For a set command ``intensity``/``speed``/``repeat`` are the raw
    protocol bytes and ``squares`` is the raw controller square indices lit
    (unrotated, exactly as on the wire); for an off command those are None/empty.
    """

    off: bool
    intensity: Optional[int]
    speed: Optional[int]
    repeat: Optional[int]
    squares: List[int]


def _parse_led_frame(frame: bytes) -> Optional[LedCommand]:
    """Decode a validated 0xB0 frame into an LedCommand, or None if not LED-shaped.

    Returns None (rather than a fabricated default) when the payload is not a
    recognized LED sub-command, so unrelated 0xB0 traffic never becomes a bogus
    intensity reading.
    """
    payload = frame[_HEADER_BYTES:-1]
    if not payload:
        return None
    sub = payload[0]
    if sub == LED_OFF_SUBCOMMAND:
        return LedCommand(off=True, intensity=None, speed=None, repeat=None, squares=[])
    if sub == LED_SET_SUBCOMMAND and len(payload) >= _MIN_SET_PAYLOAD:
        return LedCommand(
            off=False,
            speed=payload[1],
            repeat=payload[2],
            intensity=payload[3],
            squares=list(payload[_MIN_SET_PAYLOAD:]),
        )
    return None


class LedCommandDecoder:
    """Stateful framer that extracts LED commands from the app -> board stream.

    Feed raw bytes as they arrive; each call returns the LED commands decoded
    from any frames that completed. Bytes are buffered across calls so a frame
    split over reads is still decoded. The buffer is resynced to the next LED
    frame type byte, so non-LED traffic is dropped silently; a bad/implausible
    LED-typed frame drops one byte and resynchronizes.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[LedCommand]:
        """Append ``data`` and return LED commands from all frames now complete."""
        self._buf.extend(data)
        commands: List[LedCommand] = []
        while self._try_one_frame(commands):
            pass
        return commands

    def _try_one_frame(self, commands: List[LedCommand]) -> bool:
        """Consume one frame from the head of the buffer.

        Returns True if the buffer head advanced (a frame consumed or a byte
        dropped during resync) so the caller loops; False when more bytes are
        needed.
        """
        buf = self._buf
        # Resync: the head must be an LED frame type. Anchoring here avoids a
        # misaligned head on some other command type declaring a plausible length
        # that outruns the buffer and stalls decoding.
        if buf and buf[0] != LED_FRAME_TYPE:
            del buf[0]
            return True
        if len(buf) < 3:
            return False

        declared = ((buf[1] & 0x7F) << 7) | (buf[2] & 0x7F)
        if declared < _MIN_DECLARED_LENGTH or declared > _MAX_DECLARED_LENGTH:
            del buf[0]
            return True
        if len(buf) < declared:
            return False

        frame = bytes(buf[:declared])
        if checksum(frame[:-1]) != frame[-1]:
            # Mis-framed: drop one byte and resync rather than trusting a wrong
            # length. A real frame will re-align on the next checksum match.
            del buf[0]
            return True

        # Head is guaranteed to be an LED frame type by the resync anchor above.
        command = _parse_led_frame(frame)
        if command is not None:
            commands.append(command)
        del buf[:declared]
        return True
