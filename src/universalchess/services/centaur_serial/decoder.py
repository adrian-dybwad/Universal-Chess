"""Pure decoder for the Centaur board serial protocol (board -> app direction).

In "translate" mode the original Centaur binary talks to the physical board over
the serial port directly; the serial tap (see ``relay.py``) sits in the middle
and hands the board -> Centaur byte stream to this decoder so UC can observe
lift/place and key events without owning the game.

This module is intentionally pure: it takes bytes and yields decoded events, with
no serial/threading/IO of its own, so it is trivially unit-testable and carries no
application coupling. The framing and opcodes mirror UC's own driver
(``universalchess.board.sync_centaur``) and the reference probe
(``tools/dev-tools/probes/centaur_notify_events.py``):

Frame (board -> app): ``[type][len_hi][len_lo][addr1][addr2][payload...][csum]``
  - length is 14-bit: ``((len_hi & 0x7F) << 7) | (len_lo & 0x7F)`` and is the
    TOTAL frame length (header + payload + checksum);
  - ``checksum = sum(frame[:-1]) % 128``.

Two response models exist and both are handled, because which one the proprietary
Centaur build uses is not known a priori:
  - polled model (UC default): piece changes arrive as ``0x85``, keys as ``0xB1``;
  - notify model: piece events as ``0x8e``, keys as ``0xa3``.

Piece payload carries pairs ``0x40 <field>`` (LIFT) / ``0x41 <field>`` (PLACE),
where ``<field>`` is the raw controller square index. Key payload carries the
signature ``00 14 0a 05`` followed by the button code (key-down) or ``00 <code>``
(key-up).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Piece event markers within a piece payload.
LIFT_MARKER = 0x40
PLACE_MARKER = 0x41

# Frame type bytes that carry piece events / key events, across both the polled
# (0x85/0xB1) and notify (0x8e/0xa3) response models.
PIECE_FRAME_TYPES = frozenset({0x85, 0x8E})
KEY_FRAME_TYPES = frozenset({0xB1, 0xA3})

# Signature that precedes a key code inside a key frame's payload (from UC's
# sync_centaur._find_key_event_in_payload).
KEY_SIGNATURE = bytes((0x00, 0x14, 0x0A, 0x05))

# Button code -> name (from sync_centaur.DGT_BUTTON_CODES). LONG_PLAY (0x06) is a
# derived event UC synthesizes from a held PLAY, never a raw code, so it is not a
# frame-level button and is excluded here.
BUTTON_NAMES = {
    0x01: "BACK",
    0x10: "TICK",
    0x08: "UP",
    0x02: "DOWN",
    0x40: "HELP",
    0x04: "PLAY",
}

# All board -> app frame types UC knows about. Used only as a resync anchor: after
# a bad/implausible frame the decoder drops bytes until the buffer starts on a
# known type, mirroring sync_centaur's START_TYPE_BYTES gate. Unknown types are
# skipped (we do not need them) and the next known frame re-syncs.
KNOWN_FRAME_TYPES = frozenset(
    {0x90, 0x83, 0x85, 0xB1, 0xB2, 0xB4, 0xB5, 0xF0, 0xF4, 0x87, 0x93, 0x8E, 0xA3}
)

# Framing bounds. The header is 5 bytes (type + 2 length + 2 addr) plus a 1-byte
# checksum, so 5 is the smallest "short" frame with no checksum body. A declared
# length outside [3, 255] is implausible and forces a resync.
_MIN_DECLARED_LENGTH = 3
_MAX_DECLARED_LENGTH = 255
_HEADER_BYTES = 5


@dataclass(frozen=True)
class PieceEvent:
    """A physical piece lift or place.

    ``field`` is the raw controller square index as sent by the board;
    ``square`` is the algebraic name after applying the board's row rotation
    (mirrors ``board.rotateFieldHex``), or None if ``field`` is out of range.
    """

    action: str  # "lift" or "place"
    field: int
    square: Optional[str]


@dataclass(frozen=True)
class KeyEvent:
    """A button press (``is_down``) or release from the board."""

    button: str
    code: int
    is_down: bool


def checksum(data: bytes) -> int:
    """Return the board protocol checksum: sum of bytes modulo 128."""
    return sum(data) % 128


def rotate_field(field: int) -> Optional[str]:
    """Convert a raw controller square index to an algebraic square name.

    Mirrors ``universalchess.board.rotateFieldHex``: the board numbers rows in
    the opposite order to standard a1..h8, so the row is flipped. Reimplemented
    here (rather than imported) to keep this decoder free of the board module's
    hardware-bound imports. Returns None for an out-of-range index so a corrupt
    payload never fabricates a plausible-looking square.
    """
    if not 0 <= field <= 63:
        return None
    row, col = divmod(field, 8)
    idx = (7 - row) * 8 + col
    return f"{'abcdefgh'[idx % 8]}{idx // 8 + 1}"


def _extract_piece_events(payload: bytes) -> List[PieceEvent]:
    """Pull lift/place events from a piece frame's payload.

    Events are ``<marker><field>`` pairs; any non-marker byte (e.g. leading time
    bytes) is skipped, matching sync_centaur.handle_board_payload.
    """
    events: List[PieceEvent] = []
    i = 0
    while i < len(payload) - 1:
        marker = payload[i]
        if marker == LIFT_MARKER or marker == PLACE_MARKER:
            field = payload[i + 1]
            action = "lift" if marker == LIFT_MARKER else "place"
            events.append(PieceEvent(action=action, field=field, square=rotate_field(field)))
            i += 2
        else:
            i += 1
    return events


def _extract_key_event(frame: bytes) -> Optional[KeyEvent]:
    """Pull a single key event from a key frame, or None if absent.

    Scans for the ``00 14 0a 05`` signature: the following byte is the button
    code for a key-down; if it is 0x00, the byte after is the code for a key-up
    (mirrors sync_centaur._find_key_event_in_payload). An empty key frame (poll
    with no key) yields None.
    """
    start = frame.find(KEY_SIGNATURE)
    if start < 0:
        return None
    first_idx = start + len(KEY_SIGNATURE)
    if first_idx >= len(frame):
        return None
    first = frame[first_idx]
    if first != 0x00:
        return KeyEvent(button=BUTTON_NAMES.get(first, f"0x{first:02x}"), code=first, is_down=True)
    second_idx = first_idx + 1
    if second_idx < len(frame):
        second = frame[second_idx]
        if second != 0x00:
            return KeyEvent(
                button=BUTTON_NAMES.get(second, f"0x{second:02x}"), code=second, is_down=False
            )
    return None


class EventDecoder:
    """Stateful framer that turns the board -> app byte stream into events.

    Feed raw bytes as they arrive; each call returns the events decoded from any
    frames that completed. Bytes are buffered across calls so a frame split over
    reads is still decoded. Framing is anchored on known type bytes and validated
    by the declared length and checksum; on any mismatch the decoder drops one
    byte and resynchronizes, so a garbled stretch cannot desync it permanently.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List["PieceEvent | KeyEvent"]:
        """Append ``data`` and return events from all frames now complete."""
        self._buf.extend(data)
        events: List["PieceEvent | KeyEvent"] = []
        while True:
            consumed = self._try_one_frame(events)
            if not consumed:
                break
        return events

    def _try_one_frame(self, events: List["PieceEvent | KeyEvent"]) -> bool:
        """Attempt to consume one frame from the head of the buffer.

        Returns True if the buffer head advanced (a frame consumed, or a byte
        dropped during resync), so the caller loops; False when it must wait for
        more bytes.
        """
        buf = self._buf
        # Resync: the head must be a known frame type.
        if buf and buf[0] not in KNOWN_FRAME_TYPES:
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

        # Short frames (no checksum body) carry no events; just consume them.
        if declared <= _HEADER_BYTES:
            del buf[:declared]
            return True

        checksum_ok = checksum(frame[:-1]) == frame[-1]
        is_key = frame[0] in KEY_FRAME_TYPES
        # Keys are honored even on a checksum mismatch because the hardware key
        # poll is observed to do so (sync_centaur processes DGT_KEY_EVENTS_RESP
        # payloads despite a bad checksum). Everything else requires a valid
        # checksum; a mismatch means we are mis-framed, so drop one byte and
        # resync rather than trusting a wrong length.
        if not checksum_ok and not is_key:
            del buf[0]
            return True

        self._emit_events(frame, events)
        del buf[:declared]
        return True

    @staticmethod
    def _emit_events(frame: bytes, events: List["PieceEvent | KeyEvent"]) -> None:
        """Decode and append the events carried by a validated frame."""
        frame_type = frame[0]
        if frame_type in PIECE_FRAME_TYPES:
            events.extend(_extract_piece_events(frame[_HEADER_BYTES:-1]))
        elif frame_type in KEY_FRAME_TYPES:
            key = _extract_key_event(frame)
            if key is not None:
                events.append(key)


class HoldToExitDetector:
    """Detect a button held past a threshold, for an exit-to-UC gesture.

    A short press of the button is ignored; only a sustained hold triggers. The
    board is not assumed to repeat key-down while held, so the hold start is
    latched on the first down (transition into pressed) and cleared on the
    release -- a stream of repeated downs will not keep pushing the start
    forward, and the trigger fires while the button is still held (before the
    release arrives), which is the expected long-press feel.

    Driven by ``observe`` for each key event and ``expired`` polled on the read
    loop's cadence; kept pure (clock injected) so it is unit-testable without
    real time.
    """

    def __init__(self, button: str = "BACK", hold_seconds: float = 1.0) -> None:
        self._button = button
        self._hold_seconds = hold_seconds
        self._down_since: Optional[float] = None
        self._fired = False

    def observe(self, event: "PieceEvent | KeyEvent", now: float) -> None:
        """Update hold state from one event (non-key events are ignored)."""
        if not isinstance(event, KeyEvent) or event.button != self._button:
            return
        if event.is_down:
            if self._down_since is None:
                self._down_since = now
        else:
            self._down_since = None
            self._fired = False

    def expired(self, now: float) -> bool:
        """Return True once (per hold) when the button has been held long enough."""
        if self._fired or self._down_since is None:
            return False
        if now - self._down_since >= self._hold_seconds:
            self._fired = True
            return True
        return False
