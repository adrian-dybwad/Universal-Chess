"""Tests for the centaur SPI-stream -> framebuffer decoder.

Background / why these tests exist
----------------------------------
The display-translation gateway receives the DC-tagged SPI byte stream the
original Centaur software sends to its panel (captured by the LD_PRELOAD shim)
and must reconstruct the exact framebuffer image so UC can re-render it on any
installed panel. The decoder is the pure heart of that path.

These tests pin:
- an exact round-trip: an image packed by the real driver packer must decode
  back to the identical image (guards bit order, polarity, row/column layout),
- correct handling of spidev's 4096-byte chunking (the real trace shows the
  framebuffer split into 4096 + 640 writes under one RAM-write command),
- no frame is emitted before the refresh opcode (centaur streams RAM, then
  triggers the panel; emitting early would paint a half-written buffer),
- a re-sent buffer replaces (not appends to) the previous one,
- controller auto-detection from the command that carries a framebuffer-sized
  payload (UC8151D 0x10/0x13 vs SSD1680 0x24/0x26), since opcode *meaning* is
  controller-specific and the opcode sets overlap,
- the real-world collision case: a UC8151D init streams waveform-LUT writes to
  opcodes 0x20-0x24 (which are SSD1680's RAM/refresh opcodes) before the real
  framebuffer; this must still be detected as UC8151D, not SSD1680.
"""

from PIL import Image, ImageChops

from universalchess.epaper.framework.waveshare.epd2in9d import pack_image_to_buffer
from universalchess.services.centaur_display import (
    CentaurDisplayDecoder,
    UC8151D_PROFILE,
    SSD1680_PROFILE,
    PANEL_WIDTH,
    PANEL_HEIGHT,
)

# DC line semantics on the SPI bus: low = command byte, high = data byte.
DC_COMMAND = 0
DC_DATA = 1


def _pattern_image():
    """A deterministic 128x296 mode-'1' image with a distinctive layout.

    Asymmetric (filled top-left block + a single far-corner pixel) so any
    transpose/flip/rotation regression in the decoder changes the result.
    """
    img = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 255)
    for y in range(40):
        for x in range(24):
            img.putpixel((x, y), 0)
    img.putpixel((PANEL_WIDTH - 1, PANEL_HEIGHT - 1), 0)
    return img


def _assert_same_image(a, b):
    assert ImageChops.difference(a.convert("1"), b.convert("1")).getbbox() is None


def _feed_data_chunked(decoder, data, chunk=4096):
    """Feed DC=1 data in spidev-sized chunks (mimics writebytes2 blocking)."""
    for i in range(0, len(data), chunk):
        decoder.feed(DC_DATA, data[i:i + chunk])


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_uc8151d_round_trip_reconstructs_exact_image():
    """A driver-packed image must decode back identically (UC8151D).

    Guards the core inverse: bit order, white=1 polarity, and row/column layout.
    A regression (transpose, inverted polarity, wrong stride) changes pixels and
    the difference bbox becomes non-None.
    """
    original = _pattern_image()
    packed = bytes(pack_image_to_buffer(original, PANEL_WIDTH, PANEL_HEIGHT))

    decoder = CentaurDisplayDecoder()
    decoder.feed(DC_COMMAND, bytes([0x13]))   # write RAM (current/new)
    _feed_data_chunked(decoder, packed)
    frame = decoder.feed(DC_COMMAND, bytes([0x12]))  # DISPLAY_REFRESH

    assert frame is not None
    _assert_same_image(frame, original)


def test_ssd1680_round_trip_reconstructs_exact_image():
    """Same exact round-trip for the SSD1680 opcode set (0x24 RAM, 0x20 refresh).

    The packing is identical across panels; only the opcodes differ. Guards that
    the profile indirection did not break the reconstruction.
    """
    original = _pattern_image()
    packed = bytes(pack_image_to_buffer(original, PANEL_WIDTH, PANEL_HEIGHT))

    decoder = CentaurDisplayDecoder(profile=SSD1680_PROFILE)
    decoder.feed(DC_COMMAND, bytes([0x24]))   # write RAM (B/W)
    _feed_data_chunked(decoder, packed)
    frame = decoder.feed(DC_COMMAND, bytes([0x20]))  # master activation (refresh)

    assert frame is not None
    _assert_same_image(frame, original)


# ---------------------------------------------------------------------------
# Streaming behavior
# ---------------------------------------------------------------------------

def test_no_frame_emitted_before_refresh_opcode():
    """RAM data alone must NOT yield a frame; only the refresh opcode emits.

    Guards against painting a partially-streamed buffer. Failure manifests as a
    frame returned (non-None) while data is still being written.
    """
    original = _pattern_image()
    packed = bytes(pack_image_to_buffer(original, PANEL_WIDTH, PANEL_HEIGHT))

    decoder = CentaurDisplayDecoder()
    decoder.feed(DC_COMMAND, bytes([0x13]))
    result_mid = decoder.feed(DC_DATA, packed)

    assert result_mid is None
    assert decoder.take_frame() is None


def test_resent_buffer_replaces_previous_not_appends():
    """A second RAM-write opcode starts a fresh buffer (no append/overflow).

    centaur re-sends the full buffer each frame; appending would grow past 4736
    bytes and corrupt the decode. Asserts that after a stale write + a fresh
    write of the real image, the decoded frame matches the fresh image.
    """
    stale = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 255)  # all white
    fresh = _pattern_image()

    decoder = CentaurDisplayDecoder()
    decoder.feed(DC_COMMAND, bytes([0x13]))
    decoder.feed(DC_DATA, bytes(pack_image_to_buffer(stale, PANEL_WIDTH, PANEL_HEIGHT)))
    # New frame: opcode again must reset the buffer before the fresh data.
    decoder.feed(DC_COMMAND, bytes([0x13]))
    decoder.feed(DC_DATA, bytes(pack_image_to_buffer(fresh, PANEL_WIDTH, PANEL_HEIGHT)))
    frame = decoder.feed(DC_COMMAND, bytes([0x12]))

    assert frame is not None
    _assert_same_image(frame, fresh)


# ---------------------------------------------------------------------------
# Controller auto-detection
# ---------------------------------------------------------------------------

def test_autodetects_uc8151d_from_framebuffer_write():
    """A framebuffer-sized payload under 0x13 must select the UC8151D profile.

    Detection keys on the large RAM write, not a bare opcode, because the opcode
    sets overlap. Failure manifests as the wrong profile (or None) once the full
    framebuffer has streamed under the UC8151D RAM opcode.
    """
    packed = bytes(pack_image_to_buffer(_pattern_image(), PANEL_WIDTH, PANEL_HEIGHT))
    decoder = CentaurDisplayDecoder()  # profile unset
    decoder.feed(DC_COMMAND, bytes([0x13]))
    _feed_data_chunked(decoder, packed)
    assert decoder.controller == UC8151D_PROFILE.name


def test_autodetects_ssd1680_from_framebuffer_write():
    """A framebuffer-sized payload under 0x24 must select the SSD1680 profile."""
    packed = bytes(pack_image_to_buffer(_pattern_image(), PANEL_WIDTH, PANEL_HEIGHT))
    decoder = CentaurDisplayDecoder()  # profile unset
    decoder.feed(DC_COMMAND, bytes([0x24]))
    _feed_data_chunked(decoder, packed)
    assert decoder.controller == SSD1680_PROFILE.name


def test_uc8151d_lut_writes_do_not_trigger_ssd1680_detection():
    """A real UC8151D init must not be misdetected as SSD1680 via LUT opcodes.

    Why this exists: an on-device capture showed centaur streaming waveform-LUT
    writes to opcodes 0x20-0x24 (each ~42 bytes) during init -- and 0x24 is
    SSD1680's RAM opcode while 0x20 is its refresh opcode. The original
    bare-opcode detection mistook the 0x24 LUT load for an SSD1680 RAM write and
    the 0x20 LUT load for a refresh, emitting a blank frame and reporting
    controller=SSD1680. This replays that exact sequence: the small LUT writes
    must NOT detect a controller; only the 0x13 framebuffer must select UC8151D,
    and the 0x12 refresh must emit the correct image.

    Regression manifests as controller == "SSD1680", an early (blank) frame from
    a 0x20 LUT write, or a wrong/None final frame.
    """
    original = _pattern_image()
    packed = bytes(pack_image_to_buffer(original, PANEL_WIDTH, PANEL_HEIGHT))

    decoder = CentaurDisplayDecoder()  # profile unset

    # Waveform-LUT loads, mirroring the captured init (opcodes 0x20-0x24, ~42B).
    early_frames = []
    for lut_opcode in (0x20, 0x21, 0x22, 0x23, 0x24):
        early_frames.append(decoder.feed(DC_COMMAND, bytes([lut_opcode])))
        early_frames.append(decoder.feed(DC_DATA, bytes(42)))

    # No controller may be detected and no frame emitted from the LUT phase.
    assert decoder.controller is None
    assert all(f is None for f in early_frames)
    assert decoder.take_frame() is None

    # Now the real framebuffer write + refresh.
    decoder.feed(DC_COMMAND, bytes([0x13]))
    _feed_data_chunked(decoder, packed)
    assert decoder.controller == UC8151D_PROFILE.name
    frame = decoder.feed(DC_COMMAND, bytes([0x12]))

    assert frame is not None
    _assert_same_image(frame, original)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
