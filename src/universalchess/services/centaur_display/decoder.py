"""Decode the original Centaur software's SPI byte stream into framebuffer images.

The LD_PRELOAD shim forwards every SPI transfer centaur makes to its panel,
tagged with the DC line state (DC low = command byte, DC high = data byte). This
module turns that stream back into the framebuffer image centaur intended to
show, so it can be re-rendered on whatever panel UC has installed.

Design
------
Pure, side-effect-free, and driver-independent so it is unit-testable without
hardware. The decode is the exact inverse of the driver packer
``pack_image_to_buffer`` (white = bit set, MSB-first, row-major), which is what
both supported controllers use, so a round-trip is bit-exact.

Two controller command sets are supported. Their opcodes overlap and mean
different things (UC8151D's waveform-LUT opcodes 0x20-0x24 are SSD1680's
RAM/refresh opcodes), so the controller is auto-detected from the command that
carries a framebuffer-sized payload -- not from a bare opcode -- unless one is
supplied. A LUT load is only tens of bytes; the framebuffer is 4736, so the
large write unambiguously identifies the family.

Why a state machine rather than parsing whole writes: spidev's ``writebytes2``
splits a framebuffer into <=4096-byte blocks, so the bytes for one RAM-write
opcode arrive across several DC=1 transfers. Data is accumulated under the
current command until the next command; a refresh opcode emits the frame.
"""

from dataclasses import dataclass
from typing import FrozenSet, Optional

import numpy as np
from PIL import Image

# Panel geometry of the DGT Centaur 2.9" e-paper (matches the UC EPD drivers:
# EPD_WIDTH=128, EPD_HEIGHT=296). The framebuffer is (width // 8) * height bytes.
PANEL_WIDTH = 128
PANEL_HEIGHT = 296

# DC line level that denotes a command byte (low); data bytes use the high level.
DC_COMMAND_LEVEL = 0

# A command is identified as the framebuffer (RAM) write only once its
# accumulated data passes this many bytes. This disambiguates the controller:
# UC8151D's waveform-LUT opcodes (0x20-0x24) collide with SSD1680's RAM/refresh
# opcodes, but a LUT write is only tens of bytes while the framebuffer is
# (width // 8) * height = 4736 bytes. Detecting on the large write avoids
# mistaking a UC8151D LUT load (e.g. opcode 0x24) for an SSD1680 RAM write.
DETECT_MIN_FRAME_BYTES = 256


@dataclass(frozen=True)
class ControllerProfile:
    """Opcode roles for one e-paper controller family.

    Attributes:
        name: Human-readable controller name.
        ram_write_commands: Opcodes after which DC=1 data is framebuffer bytes.
            Receiving one starts a fresh buffer (centaur re-sends the full frame
            each refresh, so accumulation must reset, not append).
        current_ram_command: The opcode whose buffer holds the displayed image
            (UC8151D writes old=0x10 and new=0x13; the new buffer is shown).
        refresh_commands: Opcodes that trigger the panel to show the RAM, i.e.
            when a frame is emitted.
    """

    name: str
    ram_write_commands: FrozenSet[int]
    current_ram_command: int
    refresh_commands: FrozenSet[int]


# UC8151D (Universal-Chess "V2"; the controller the current DGT Centaur build
# speaks -- confirmed by a captured trace): RAM-old 0x10, RAM-new 0x13,
# DISPLAY_REFRESH 0x12.
UC8151D_PROFILE = ControllerProfile(
    name="UC8151D",
    ram_write_commands=frozenset({0x10, 0x13}),
    current_ram_command=0x13,
    refresh_commands=frozenset({0x12}),
)

# SSD1680 (Universal-Chess "V1"): write RAM B/W 0x24, RAM red 0x26, MASTER
# ACTIVATION 0x20 triggers the update. Only needed to decode a centaur build
# that itself speaks SSD1680; the current build speaks UC8151D. These opcodes
# are from the datasheet and are validated against a real capture if/when such a
# build is available.
SSD1680_PROFILE = ControllerProfile(
    name="SSD1680",
    ram_write_commands=frozenset({0x24, 0x26}),
    current_ram_command=0x24,
    refresh_commands=frozenset({0x20}),
)

_DETECTION_PROFILES = (UC8151D_PROFILE, SSD1680_PROFILE)


def buffer_to_image(buffer: bytes, width: int = PANEL_WIDTH, height: int = PANEL_HEIGHT,
                    white_bit_is_set: bool = True) -> Image.Image:
    """Unpack a 1bpp panel buffer into a PIL mode-'1' image.

    Inverse of ``pack_image_to_buffer``: MSB-first, row-major, ``(width // 8) *
    height`` bytes, white = bit set. A short buffer is padded with 0xFF (white)
    and a long one truncated, so a partial/oversized stream degrades to a valid
    image rather than raising.
    """
    expected = (width // 8) * height
    raw = bytes(buffer[:expected]).ljust(expected, b"\xff")
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8)).reshape(height, width)
    plane = bits if white_bit_is_set else (1 - bits)
    return Image.fromarray((plane * 255).astype(np.uint8), mode="L").convert("1")


class CentaurDisplayDecoder:
    """Stateful decoder turning centaur's DC-tagged SPI stream into frames.

    Feed each SPI transfer via ``feed(dc, data)``. A frame is returned (and
    stored for ``take_frame``) when a refresh opcode completes a RAM write.
    """

    def __init__(self, width: int = PANEL_WIDTH, height: int = PANEL_HEIGHT,
                 profile: Optional[ControllerProfile] = None,
                 white_bit_is_set: bool = True):
        self._width = width
        self._height = height
        self._profile = profile
        self._white_bit_is_set = white_bit_is_set
        self._buffers = {}  # opcode -> bytearray of accumulated RAM data
        self._current_command: Optional[int] = None
        self._last_frame: Optional[Image.Image] = None

    @property
    def controller(self) -> Optional[str]:
        """Name of the detected/selected controller, or None if not yet known."""
        return self._profile.name if self._profile is not None else None

    def feed(self, dc: int, data: bytes) -> Optional[Image.Image]:
        """Feed one SPI transfer.

        Args:
            dc: DC line state during the transfer (0 = command, 1 = data).
            data: The transferred bytes.

        Returns:
            The decoded frame if this transfer triggered a refresh, else None.
        """
        if not data:
            return None
        if dc == DC_COMMAND_LEVEL:
            frame = None
            for opcode in data:
                emitted = self._on_command(opcode)
                if emitted is not None:
                    frame = emitted
            return frame
        self._on_data(data)
        return None

    def take_frame(self) -> Optional[Image.Image]:
        """Return and clear the most recently decoded frame (None if none)."""
        frame, self._last_frame = self._last_frame, None
        return frame

    def _on_command(self, opcode: int) -> Optional[Image.Image]:
        self._current_command = opcode
        # Every command opens a fresh data phase for that opcode, so a re-sent
        # frame replaces the previous one instead of appending. Resetting the
        # incoming opcode's buffer (not the RAM opcode's) keeps an already
        # accumulated framebuffer intact until its refresh opcode arrives.
        self._buffers[opcode] = bytearray()

        if self._profile is not None and opcode in self._profile.refresh_commands:
            return self._emit_frame()

        return None

    def _on_data(self, data: bytes) -> None:
        cmd = self._current_command
        if cmd is None:
            return
        # Accumulate under the current command regardless of controller: before
        # detection the RAM opcode is unknown, and non-RAM commands only ever
        # carry a handful of parameter bytes, so this is cheap and harmless.
        buf = self._buffers.setdefault(cmd, bytearray())
        buf.extend(data)

        # Detect the controller from the command carrying a framebuffer-sized
        # payload. A waveform-LUT write (tens of bytes) must not trigger this,
        # because UC8151D LUT opcodes (e.g. 0x24) collide with SSD1680 RAM
        # opcodes; only the large RAM write disambiguates the family.
        if self._profile is None and len(buf) >= DETECT_MIN_FRAME_BYTES:
            self._detect_from_ram_write(cmd)

    def _detect_from_ram_write(self, opcode: int) -> None:
        for profile in _DETECTION_PROFILES:
            if opcode in profile.ram_write_commands:
                self._profile = profile
                return

    def _emit_frame(self) -> Optional[Image.Image]:
        if self._profile is None:
            return None
        buf = self._buffers.get(self._profile.current_ram_command)
        if not buf:
            return None
        frame = buffer_to_image(
            bytes(buf), self._width, self._height, self._white_bit_is_set
        )
        self._last_frame = frame
        return frame
