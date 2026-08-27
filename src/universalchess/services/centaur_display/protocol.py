"""Wire protocol between the LD_PRELOAD shim and the UC display gateway.

Each record is:

    +--------+------------------+----------------+
    | kind (1) | length (4, LE) | payload (len)  |
    +--------+------------------+----------------+

SPI transfers use ``kind`` 0 (command, DC low) or 1 (data, DC high); the
payload is the raw bytes transferred. Observation records use ``kind`` 2
(GPIO bitmask, 4-byte little-endian, bit N = BCM pin N) and ``kind`` 3
(SPI device path as ASCII, no NUL). The gateway must not feed kinds 2/3
to the framebuffer decoder. The framing is deliberately trivial so the
C shim can emit it with a single header struct + the payload, and so the
gateway can reassemble across arbitrary socket chunk boundaries.
"""

import struct
from typing import Callable, List, Optional, Tuple

# kind as uint8, payload length as little-endian uint32.
_HEADER = struct.Struct("<BI")
HEADER_SIZE = _HEADER.size

# First header byte. 0/1 remain the DC line for SPI (historical name ``dc``).
RECORD_SPI_COMMAND = 0
RECORD_SPI_DATA = 1
RECORD_GPIO_PINS = 2
RECORD_SPI_PATH = 3

_GPIO_MASK = struct.Struct("<I")
_BCM_PIN_MAX = 27

# Sanity cap so a corrupt/hostile header cannot make the gateway allocate
# unbounded memory. A full framebuffer transfer is <= 4096 bytes (spidev block),
# so 1 MiB is far above any legitimate record.
MAX_PAYLOAD = 1 << 20

ReadFn = Callable[[int], bytes]


def encode_record(dc: int, payload: bytes) -> bytes:
    """Encode one SPI transfer record (header + payload).

    ``dc`` is the DC line (0 = command, 1 = data). Observation records use
    :func:`encode_gpio_pins` and :func:`encode_spi_path`.
    """
    return _HEADER.pack(1 if dc else 0, len(payload)) + payload


def encode_gpio_pins(mask: int) -> bytes:
    """Encode a BCM pin bitmask as a ``RECORD_GPIO_PINS`` record."""
    payload = _GPIO_MASK.pack(mask & 0xFFFFFFFF)
    return _HEADER.pack(RECORD_GPIO_PINS, len(payload)) + payload


def encode_spi_path(path: str) -> bytes:
    """Encode an opened ``/dev/spidevN.M`` path as a ``RECORD_SPI_PATH`` record."""
    payload = path.encode("ascii")
    return _HEADER.pack(RECORD_SPI_PATH, len(payload)) + payload


def gpio_mask_to_pins(mask: int) -> List[int]:
    """Return sorted BCM pin numbers set in ``mask`` (header pins 0-27)."""
    return [bit for bit in range(_BCM_PIN_MAX + 1) if mask & (1 << bit)]


def decode_gpio_mask(payload: bytes) -> Optional[int]:
    """Unpack a ``RECORD_GPIO_PINS`` payload, or None if the length is not 4."""
    if len(payload) != _GPIO_MASK.size:
        return None
    return _GPIO_MASK.unpack(payload)[0]


def decode_spi_path(payload: bytes) -> Optional[str]:
    """Unpack a ``RECORD_SPI_PATH`` payload, or None if it is not a spidev node."""
    try:
        path = payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not path.startswith("/dev/spidev"):
        return None
    if ".." in path or "\x00" in path or len(path) > 63:
        return None
    return path


def _read_exact(read_fn: ReadFn, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes, reassembling across short reads.

    Returns None if the stream closes (read_fn returns empty) before ``n`` bytes
    arrive -- i.e. a clean EOF or a truncated tail, which the caller treats as
    disconnect rather than corruption.
    """
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = read_fn(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_record(read_fn: ReadFn) -> Optional[Tuple[int, bytes]]:
    """Read one record from ``read_fn``.

    Args:
        read_fn: A recv-like callable returning up to ``n`` bytes (``b''`` at EOF).

    Returns:
        ``(kind, payload)``, or None on a clean EOF / truncated stream.
        ``kind`` 0/1 is the SPI DC line; 2/3 are observation records.

    Raises:
        ValueError: If a record declares a payload larger than ``MAX_PAYLOAD``
            (a corrupt or out-of-spec stream; failing loudly beats allocating
            arbitrary memory or silently desyncing the stream).
    """
    header = _read_exact(read_fn, HEADER_SIZE)
    if header is None:
        return None
    dc, length = _HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ValueError(f"record payload too large: {length} > {MAX_PAYLOAD}")
    if length == 0:
        return dc, b""
    payload = _read_exact(read_fn, length)
    if payload is None:
        return None
    return dc, payload
