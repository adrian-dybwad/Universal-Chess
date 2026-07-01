"""Tests for the Centaur board serial decoder (board -> app direction).

The decoder is the eyes of the serial tap: it turns the raw board byte stream
into lift/place and key events, and drives the hold-to-exit gesture. These tests
pin the framing (length + checksum + resync), the two response models
(polled 0x85/0xB1 and notify 0x8e/0xa3), the square rotation, and the hold
detector's edge cases, all with hand-built frames so a framing regression is
caught deterministically without hardware.
"""

from universalchess.services.centaur_serial.decoder import (
    EventDecoder,
    HoldToExitDetector,
    KeyEvent,
    PieceEvent,
    checksum,
    rotate_field,
)


def build_frame(frame_type: int, payload: bytes, addr1: int = 0x00, addr2: int = 0x00) -> bytes:
    """Build a valid board -> app frame around ``payload``.

    Layout: [type][len_hi][len_lo][addr1][addr2][payload][checksum], where the
    14-bit length is the total frame length and checksum = sum(all but last) %128.
    """
    total = 1 + 2 + 2 + len(payload) + 1  # type + length(2) + addr(2) + payload + csum
    header = bytes((frame_type, (total >> 7) & 0x7F, total & 0x7F, addr1, addr2))
    body = header + payload
    return body + bytes((checksum(body),))


# ---------------------------------------------------------------------------
# Square rotation
# ---------------------------------------------------------------------------


def test_rotate_field_flips_rows_to_algebraic():
    """The raw controller index maps to algebraic with rows flipped.

    Why this test exists: the board numbers rows opposite to a1..h8, so a wrong
    (or absent) rotation would light/report the mirrored square on the web. Pins
    the corners and rejects out-of-range so a corrupt byte cannot fabricate a
    square. Regression manifests as a1<->a8 swap or a non-None for a bad index.
    """
    assert rotate_field(0) == "a8"
    assert rotate_field(56) == "a1"
    assert rotate_field(7) == "h8"
    assert rotate_field(63) == "h1"
    assert rotate_field(52) == "e2"
    assert rotate_field(36) == "e4"
    assert rotate_field(-1) is None
    assert rotate_field(64) is None


# ---------------------------------------------------------------------------
# Piece frames
# ---------------------------------------------------------------------------


def test_decodes_lift_then_place_from_piece_frame():
    """A 0x85 piece frame yields ordered lift/place events with rotated squares.

    Why this test exists: this is the core signal the tap adds -- physical
    lift/place. Leading time bytes precede the markers on real hardware, so the
    payload here includes them to prove they are skipped. Regression manifests as
    missing events, wrong order, or the time bytes being misread as a marker.
    """
    # time bytes (non-markers, skipped), then lift e2 (raw 52), place e4 (raw 36).
    payload = bytes((0x0A, 0x00, 0x40, 52, 0x41, 36))
    events = EventDecoder().feed(build_frame(0x85, payload))
    assert events == [
        PieceEvent(action="lift", field=52, square="e2"),
        PieceEvent(action="place", field=36, square="e4"),
    ]


def test_notify_model_piece_type_0x8e_also_decodes():
    """Piece events decode under the notify model type (0x8e), not just 0x85.

    Why this test exists: which response model the proprietary Centaur build uses
    is unknown, so both must work. Regression manifests as notify-model piece
    events being silently dropped (empty result).
    """
    events = EventDecoder().feed(build_frame(0x8E, bytes((0x41, 56))))
    assert events == [PieceEvent(action="place", field=56, square="a1")]


# ---------------------------------------------------------------------------
# Key frames
# ---------------------------------------------------------------------------


def test_decodes_key_down_and_key_up():
    """A key frame yields a down for a nonzero code, an up for the 00-prefixed code.

    Why this test exists: the exit gesture depends on distinguishing down from up.
    Uses BACK (0x01). Regression manifests as down/up swapped or the button
    unrecognized (which would break the hold-to-exit trigger).
    """
    down = EventDecoder().feed(build_frame(0xB1, bytes((0x00, 0x14, 0x0A, 0x05, 0x01))))
    assert down == [KeyEvent(button="BACK", code=0x01, is_down=True)]

    up = EventDecoder().feed(build_frame(0xB1, bytes((0x00, 0x14, 0x0A, 0x05, 0x00, 0x01))))
    assert up == [KeyEvent(button="BACK", code=0x01, is_down=False)]


def test_notify_model_key_type_0xa3_also_decodes():
    """Key events decode under the notify model type (0xa3), not just 0xB1.

    Why this test exists: symmetric with the piece-model test -- both key response
    models must be handled. Regression manifests as notify-model keys dropped.
    """
    events = EventDecoder().feed(build_frame(0xA3, bytes((0x00, 0x14, 0x0A, 0x05, 0x10))))
    assert events == [KeyEvent(button="TICK", code=0x10, is_down=True)]


def test_empty_key_poll_yields_no_event():
    """A key frame with no key signature (idle poll) yields nothing.

    Why this test exists: the board is polled continuously; idle polls must not
    fabricate key events. Regression manifests as spurious key events flooding the
    exit detector.
    """
    assert EventDecoder().feed(build_frame(0xB1, bytes((0x00, 0x00, 0x00)))) == []


# ---------------------------------------------------------------------------
# Framing: split reads, resync, checksum
# ---------------------------------------------------------------------------


def test_frame_split_across_feeds_is_reassembled():
    """A frame split over two feeds still decodes once complete.

    Why this test exists: serial reads chop frames arbitrarily; the decoder must
    buffer across calls. Regression manifests as the first partial feed emitting a
    bad/partial event or the frame being lost.
    """
    frame = build_frame(0x85, bytes((0x40, 52)))
    decoder = EventDecoder()
    assert decoder.feed(frame[:3]) == []
    assert decoder.feed(frame[3:]) == [PieceEvent(action="lift", field=52, square="e2")]


def test_leading_garbage_is_resynced_then_frame_decodes():
    """Unknown leading bytes are dropped until a known frame type, then decode.

    Why this test exists: the tap may start mid-stream or hit noise; the decoder
    must not desync permanently. Prefix with bytes that are not known frame types.
    Regression manifests as the following valid frame never decoding.
    """
    frame = build_frame(0xB1, bytes((0x00, 0x14, 0x0A, 0x05, 0x04)))
    events = EventDecoder().feed(bytes((0x11, 0x22, 0x33)) + frame)
    assert events == [KeyEvent(button="PLAY", code=0x04, is_down=True)]


def test_non_key_bad_checksum_resyncs_without_emitting():
    """A corrupt non-key frame is not emitted and does not desync later frames.

    Why this test exists: a bad checksum means we are mis-framed; trusting the
    declared length could skip real bytes. The decoder drops one byte and
    resyncs. Here a 0x85 frame with a corrupted checksum precedes a valid key
    frame; only the key event should surface. Regression manifests as a bogus
    piece event or the trailing valid frame being swallowed.
    """
    bad = bytearray(build_frame(0x85, bytes((0x40, 52))))
    bad[-1] ^= 0xFF  # corrupt checksum
    good = build_frame(0xB1, bytes((0x00, 0x14, 0x0A, 0x05, 0x01)))
    events = EventDecoder().feed(bytes(bad) + good)
    assert events == [KeyEvent(button="BACK", code=0x01, is_down=True)]


def test_key_frame_honored_despite_bad_checksum():
    """A key frame with a bad checksum is still decoded (matches hardware).

    Why this test exists: sync_centaur processes key payloads even when the
    checksum mismatches, so the tap must too or a real key (including the exit
    hold) could be missed. Regression manifests as the key being dropped on a
    checksum mismatch.
    """
    frame = bytearray(build_frame(0xA3, bytes((0x00, 0x14, 0x0A, 0x05, 0x01))))
    frame[-1] ^= 0xFF
    events = EventDecoder().feed(bytes(frame))
    assert events == [KeyEvent(button="BACK", code=0x01, is_down=True)]


# ---------------------------------------------------------------------------
# Hold-to-exit detector
# ---------------------------------------------------------------------------


def test_hold_detector_ignores_short_press():
    """A press released before the threshold never triggers.

    Why this test exists: BACK is a normal Centaur button; only a deliberate hold
    should exit. Regression manifests as a quick BACK tap bouncing the user out of
    Centaur.
    """
    det = HoldToExitDetector(button="BACK", hold_seconds=1.0)
    det.observe(KeyEvent("BACK", 0x01, True), now=0.0)
    det.observe(KeyEvent("BACK", 0x01, False), now=0.5)
    assert det.expired(now=2.0) is False


def test_hold_detector_fires_once_after_threshold_while_held():
    """A sustained hold fires exactly once, before release.

    Why this test exists: the exit must trigger while the button is still held
    (long-press feel) and must not re-trigger every poll. Regression manifests as
    firing early (< threshold) or repeatedly.
    """
    det = HoldToExitDetector(button="BACK", hold_seconds=1.0)
    det.observe(KeyEvent("BACK", 0x01, True), now=0.0)
    assert det.expired(now=0.9) is False
    assert det.expired(now=1.0) is True
    assert det.expired(now=1.5) is False  # only once


def test_hold_detector_latches_start_on_first_down_ignoring_repeats():
    """Repeated key-downs while held do not push the hold start forward.

    Why this test exists: the board may report key-down on each poll while a
    button is held; if each reset the timer the hold would never mature. The start
    is latched on the first down. Regression manifests as expired() never
    returning True under a stream of repeated downs.
    """
    det = HoldToExitDetector(button="BACK", hold_seconds=1.0)
    det.observe(KeyEvent("BACK", 0x01, True), now=0.0)
    det.observe(KeyEvent("BACK", 0x01, True), now=0.4)
    det.observe(KeyEvent("BACK", 0x01, True), now=0.8)
    assert det.expired(now=1.0) is True


def test_hold_detector_ignores_other_buttons():
    """Holding a different button does not trigger the exit gesture.

    Why this test exists: only the configured button should exit; TICK/PLAY held
    for menus must be inert here. Regression manifests as an unrelated held button
    exiting Centaur.
    """
    det = HoldToExitDetector(button="BACK", hold_seconds=1.0)
    det.observe(KeyEvent("PLAY", 0x04, True), now=0.0)
    assert det.expired(now=5.0) is False
