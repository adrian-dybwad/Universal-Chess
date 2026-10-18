"""Pack and scale 1-bit frames for replacement 2.9\" panels.

The chess UI is laid out for the Centaur's 128x296 panel. A replacement glass
with a different native resolution still receives that frame: this module
scales it with nearest-neighbor, then (for four-color controllers) packs two
bits per pixel. Both steps are pure so the byte layout can be pinned without
SPI hardware.

Pixel conventions match the drivers that already ship:

* 1-bit: 1 = white, 0 = black, most-significant bit is the leftmost pixel.
* Red mask: 0 = red, 1 = not red (the ``getbuffer_red`` convention).
* Four-color codes, from the Good Display GDEY029F52 demo: black ``00``,
  white ``01``, yellow ``10``, red ``11``. Four pixels per byte, high pair
  first. Yellow is a defined code and is not produced here; the chess UI has
  no yellow plane.
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

# Good Display GDEY029F52 / GxEPD2 GDEY029F51H 2-bit codes.
BWRY_BLACK = 0x00
BWRY_WHITE = 0x01
BWRY_YELLOW = 0x02
BWRY_RED = 0x03


def scale_1bpp(
    source: Sequence[int],
    source_width: int,
    source_height: int,
    dest_width: int,
    dest_height: int,
) -> list:
    """Nearest-neighbor scale of a packed 1-bit frame.

    ``source`` is ``source_width * source_height / 8`` bytes. The destination
    has the same packing. When the sizes match, the bytes are copied. A source
    pixel is chosen with integer division (``x * source_width // dest_width``),
    so the mapping is stable and does not depend on Pillow's resample filters.
    """
    if source_width == dest_width and source_height == dest_height:
        return list(source)
    dest = [0] * ((dest_width // 8) * dest_height)
    for y in range(dest_height):
        source_y = y * source_height // dest_height
        for x in range(dest_width):
            source_x = x * source_width // dest_width
            if _bit(source, source_width, source_x, source_y):
                _set_bit(dest, dest_width, x, y)
    return dest


def pack_bwry(
    black_white: Sequence[int],
    width: int,
    height: int,
    red: Optional[Sequence[int]] = None,
) -> list:
    """Pack a 1-bit black/white frame, and an optional red mask, into 2-bit BWRY.

    A red-mask bit of 0 becomes red (``11``). Otherwise a black/white bit of 0
    becomes black (``00``) and 1 becomes white (``01``). The result is
    ``width * height / 4`` bytes. ``width`` must be a multiple of 4, which both
    128 and 168 are.
    """
    pixel_count = width * height
    packed = []
    accumulator = 0
    filled = 0
    for index in range(pixel_count):
        if red is not None and not _bit_at(red, index):
            code = BWRY_RED
        elif _bit_at(black_white, index):
            code = BWRY_WHITE
        else:
            code = BWRY_BLACK
        accumulator = ((accumulator << 2) | code) & 0xFF
        filled += 1
        if filled == 4:
            packed.append(accumulator)
            accumulator = 0
            filled = 0
    return packed


def bw_partial_keeps_gray(
    old_mono: Sequence[int],
    new_mono: Sequence[int],
    old_gray: Sequence[int],
    new_gray: Sequence[int],
) -> bool:
    """True when a fast black/white partial will not drive a gray pixel.

    The partial LUT drives a pixel only when its old and new bits differ.
    Light gray is stored as white and dark gray as black in that 1-bit
    image, so an unchanged gray pixel is a no-op and the particles stay
    where the 4-gray waveform left them. A pixel that enters, leaves, or
    moves between the two grays has to take the 4-gray waveform instead.
    A length mismatch returns False so the caller loads that waveform
    rather than a partial that would paint the gray solid.
    """
    if not (len(old_mono) == len(new_mono) == len(old_gray) == len(new_gray)):
        return False
    for old_bits, new_bits, old_mask, new_mask in zip(old_mono, new_mono, old_gray, new_gray):
        changed = (old_bits ^ new_bits) | (old_mask ^ new_mask)
        if changed & (old_mask | new_mask) & 0xFF:
            return False
    return True


@dataclass(frozen=True)
class Gray4Frame:
    """One GDEW029I6FD frame: both 4-gray planes, and what the fast partial compares.

    ``previous`` goes to command 0x10 and ``current`` to 0x13. ``mono`` is the
    1-bit image the fast partial compares: white and light gray are 1, black
    and dark gray are 0. ``gray_mask`` is 1 on the two gray levels.
    """

    previous: Tuple[int, ...]
    current: Tuple[int, ...]
    mono: Tuple[int, ...]
    gray_mask: Tuple[int, ...]


def gray4_frame(image, width: int, height: int) -> Gray4Frame:
    """Quantize an image to 4 gray.

    The planes follow GxEPD2_4G ``writeImage_4G``. A size that matches
    neither orientation is a white frame with no gray, the same refusal as
    ``pack_image_to_buffer``.
    """
    nbytes = (width // 8) * height
    gray = np.array(image.convert("L"), dtype=np.uint8)
    imheight, imwidth = gray.shape
    if imwidth == height and imheight == width:
        gray = gray.T[::-1, :]
    elif not (imwidth == width and imheight == height):
        return gray4_frame_from_mono([0xFF] * nbytes)
    level = np.digitize(gray, (64, 128, 192))
    mono_bits = level >= 2
    gray_bits = (level == 1) | (level == 2)
    current_bits = (level == 3) | (level == 1)
    mono = tuple(np.packbits(mono_bits, axis=1).reshape(-1).tolist())
    gray_mask = tuple(np.packbits(gray_bits, axis=1).reshape(-1).tolist())
    current = tuple(np.packbits(current_bits, axis=1).reshape(-1).tolist())
    return Gray4Frame(previous=mono, current=current, mono=mono, gray_mask=gray_mask)


def gray4_frame_from_mono(black_white: Sequence[int]) -> Gray4Frame:
    """A packed 1-bit frame as a 4-gray frame with no gray pixels.

    White is 1 in both planes and black is 0 in both, so the two planes are
    the 1-bit frame itself.
    """
    mono = tuple(black_white)
    return Gray4Frame(previous=mono, current=mono, mono=mono, gray_mask=(0x00,) * len(mono))


def pack_gray4(image, width: int, height: int) -> Tuple[list, list]:
    """Split an image into the two planes of the GDEW029I6FD 4-gray waveform.

    GxEPD2_4G ``writeImage_4G`` (2 bits per pixel) maps each pixel onto the
    controller's previous RAM (command 0x10) and current RAM (command 0x13):

        white      (3)  previous 1, current 1
        light gray (2)  previous 1, current 0
        dark gray  (1)  previous 0, current 1
        black      (0)  previous 0, current 0

    Luminance is quantized in four equal bands of 0..255 (0 is black). A
    1-bit image only contains 0 and 255, so it stays black and white; gray
    appears only when the drawing path filled a flat mid level. The same two
    orientations as ``pack_image_to_buffer`` are accepted. Any other size
    returns two all-white planes (both bits 1), matching that packer's
    refusal to guess a layout.
    """
    frame = gray4_frame(image, width, height)
    return list(frame.previous), list(frame.current)


def _bit(buffer: Sequence[int], width: int, x: int, y: int) -> int:
    return _bit_at(buffer, y * width + x)


def _bit_at(buffer: Sequence[int], pixel_index: int) -> int:
    return (buffer[pixel_index // 8] >> (7 - (pixel_index % 8))) & 1


def _set_bit(buffer: list, width: int, x: int, y: int) -> None:
    pixel_index = y * width + x
    buffer[pixel_index // 8] |= 1 << (7 - (pixel_index % 8))
