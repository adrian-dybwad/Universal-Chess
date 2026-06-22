"""Tests for the vectorized e-paper image packer (``getbuffer``).

Why these tests exist:
    ``getbuffer`` packs a PIL image into the panel's 1bpp byte buffer on every
    display refresh. The original implementation was a pure-Python nested loop
    over all 128x296 pixels -- a measurable latency cost on the single-core Pi
    Zero W. It was replaced with a vectorized numpy packer (``pack_image_to_
    buffer``). Because the packed bytes drive the panel RAM directly, the new
    packer must be BYTE-IDENTICAL to the old loop for every input, or the panel
    renders garbage/inverted/rotated.

How a regression manifests:
    These tests use the original nested loop as a reference oracle and assert
    the vectorized output matches it byte-for-byte. A transpose/flip error in
    the rotated branch surfaces as a mirrored or 90-degree-wrong image; a
    polarity error surfaces as an inverted image (all bytes complemented); a
    length error corrupts every subsequent RAM write. The structured (diagonal)
    fixtures specifically catch axis/orientation mistakes that symmetric
    all-white / all-black images cannot.
"""

import sys
import unittest
from unittest.mock import MagicMock

# Mock Raspberry Pi hardware libraries before importing the drivers: they import
# RPi.GPIO / spidev / gpiozero transitively via epdconfig (mirrors the pattern in
# test_epd_ssd1680). The packing logic itself touches no hardware.
for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.waveshare.epd2in9d import (
    EPD_WIDTH,
    EPD_HEIGHT,
    pack_image_to_buffer,
)
from universalchess.epaper.framework.waveshare.epd2in9d import EPD as EpdV2
from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD as EpdSsd1680

BUFFER_LEN = (EPD_WIDTH // 8) * EPD_HEIGHT  # 4736 bytes for 128x296 @ 1bpp


def reference_getbuffer(image, width, height):
    """Original pure-Python packing loop, kept verbatim as the correctness oracle.

    This is the exact algorithm that shipped before vectorization; the
    vectorized packer must reproduce its output byte-for-byte. White pixels
    leave the bit set (buffer starts all-0xFF); black pixels clear it, MSB-first
    within each byte. Two orientations are handled: upright (width x height) and
    the 180-mounted rotated frame (height x width).
    """
    buf = [0xFF] * ((width // 8) * height)
    mono = image.convert('1')
    imwidth, imheight = mono.size
    pixels = mono.load()
    if imwidth == width and imheight == height:
        for y in range(imheight):
            for x in range(imwidth):
                if pixels[x, y] == 0:
                    buf[int((x + y * width) / 8)] &= ~(0x80 >> (x % 8))
    elif imwidth == height and imheight == width:
        for y in range(imheight):
            for x in range(imwidth):
                newx = y
                newy = height - x - 1
                if pixels[x, y] == 0:
                    buf[int((newx + newy * width) / 8)] &= ~(0x80 >> (y % 8))
    return buf


def make_upright_diagonal():
    """Upright (128x296) image with a black diagonal -- asymmetric on both axes.

    A diagonal is not symmetric under transpose or row/column reversal, so any
    orientation bug in the packer changes which bytes/bits flip versus the
    reference. Returns a mode-'1' image (white background, black diagonal).
    """
    img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)
    px = img.load()
    for i in range(min(EPD_WIDTH, EPD_HEIGHT)):
        px[i % EPD_WIDTH, i] = 0
    return img


def make_rotated_diagonal():
    """Rotated (296x128) image with a black diagonal plus an asymmetric marker.

    Exercises the rotated branch (imwidth==height, imheight==width). The extra
    corner marker breaks the residual symmetry of a pure diagonal so a flip on
    the wrong axis is detectable.
    """
    img = Image.new('1', (EPD_HEIGHT, EPD_WIDTH), 255)
    px = img.load()
    for i in range(min(EPD_WIDTH, EPD_HEIGHT)):
        px[i, i % EPD_WIDTH] = 0
    px[0, 0] = 0  # asymmetric corner marker
    return img


class PackImageToBufferTests(unittest.TestCase):
    """The vectorized packer must equal the reference loop for every input."""

    def _assert_matches_reference(self, image):
        expected = reference_getbuffer(image, EPD_WIDTH, EPD_HEIGHT)
        actual = pack_image_to_buffer(image, EPD_WIDTH, EPD_HEIGHT)
        # Length first: a wrong-length buffer corrupts the whole RAM write and
        # makes a byte-wise diff meaningless.
        self.assertEqual(len(actual), BUFFER_LEN)
        self.assertEqual(len(actual), len(expected))
        # Type contract: callers (display/DisplayPartial) store and re-send this
        # as a list; an ndarray would change downstream copy/serialize behavior.
        self.assertIsInstance(actual, list)
        self.assertTrue(all(isinstance(b, int) for b in actual))
        # Byte-for-byte equality is the core guarantee of the refactor.
        self.assertEqual(actual, expected)

    def test_all_white_matches_reference(self):
        # All-white must pack to all-0xFF; pins the white=1 polarity.
        self._assert_matches_reference(Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255))

    def test_all_black_matches_reference(self):
        # All-black must pack to all-0x00; pins the black=0 polarity (not inverted).
        self._assert_matches_reference(Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 0))

    def test_upright_diagonal_matches_reference(self):
        # Asymmetric upright content: catches row/column packing order errors.
        self._assert_matches_reference(make_upright_diagonal())

    def test_rotated_diagonal_matches_reference(self):
        # Asymmetric rotated content: catches transpose/flip-axis errors in the
        # 180-mounted branch (the most error-prone path).
        self._assert_matches_reference(make_rotated_diagonal())

    def test_many_random_upright_images_match_reference(self):
        # Random pixels stress every byte/bit position in the upright branch;
        # a single mispacked bit anywhere fails the byte-wise comparison.
        import random
        rng = random.Random(20240621)
        for _ in range(8):
            img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 0)
            px = img.load()
            for y in range(EPD_HEIGHT):
                for x in range(EPD_WIDTH):
                    px[x, y] = 255 if rng.random() < 0.5 else 0
            self._assert_matches_reference(img)

    def test_many_random_rotated_images_match_reference(self):
        # Random pixels in the rotated orientation stress the transpose+flip
        # mapping across every bit position.
        import random
        rng = random.Random(13371337)
        for _ in range(8):
            img = Image.new('1', (EPD_HEIGHT, EPD_WIDTH), 0)
            px = img.load()
            for y in range(EPD_WIDTH):
                for x in range(EPD_HEIGHT):
                    px[x, y] = 255 if rng.random() < 0.5 else 0
            self._assert_matches_reference(img)

    def test_grayscale_input_is_converted_like_reference(self):
        # Non-'1' input must be thresholded identically to the reference (which
        # calls image.convert('1')); otherwise non-board images render wrong.
        img = Image.new('L', (EPD_WIDTH, EPD_HEIGHT), 0)
        px = img.load()
        for y in range(EPD_HEIGHT):
            for x in range(EPD_WIDTH):
                px[x, y] = (x * 7 + y * 3) % 256
        self._assert_matches_reference(img)

    def test_mismatched_size_returns_all_white(self):
        # An image matching neither orientation must yield an untouched all-0xFF
        # buffer, exactly as the original loop did (it skipped both branches).
        odd = Image.new('1', (64, 64), 0)
        expected = reference_getbuffer(odd, EPD_WIDTH, EPD_HEIGHT)
        actual = pack_image_to_buffer(odd, EPD_WIDTH, EPD_HEIGHT)
        self.assertEqual(len(actual), BUFFER_LEN)
        self.assertTrue(all(b == 0xFF for b in actual))
        self.assertEqual(actual, expected)


class DriverGetbufferDelegationTests(unittest.TestCase):
    """Both panel drivers must produce identical buffers via the shared packer."""

    def test_v2_and_ssd1680_getbuffer_agree_with_reference(self):
        # Both drivers historically carried duplicate copies of the same loop;
        # they now delegate to the shared packer. Verify each driver's
        # getbuffer matches the reference (and therefore each other) so the
        # active driver choice never changes the rendered bytes.
        for image in (
            Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255),
            make_upright_diagonal(),
            make_rotated_diagonal(),
        ):
            expected = reference_getbuffer(image, EPD_WIDTH, EPD_HEIGHT)
            self.assertEqual(EpdV2().getbuffer(image), expected)
            self.assertEqual(EpdSsd1680().getbuffer(image), expected)


if __name__ == '__main__':
    unittest.main()
