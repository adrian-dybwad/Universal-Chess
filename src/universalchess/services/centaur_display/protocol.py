"""Wire protocol between the LD_PRELOAD shim and the UC display gateway.

Each SPI transfer centaur makes is sent as one record:

    +--------+------------------+----------------+
    | dc (1) | length (4, LE)   | payload (len)  |
    +--------+------------------+----------------+

``dc`` is the DC line state during the transfer (0 = command, 1 = data); the
payload is the raw bytes transferred. The framing is deliberately trivial so the
C shim can emit it with a single header struct + the payload, and so the gateway
can reassemble across arbitrary socket chunk boundaries.
"""

import struct
from typing import Callable, Optional, Tuple

# dc as uint8, payload length as little-endian uint32.
_HEADER = struct.Struct("<BI")
HEADER_SIZE = _HEADER.size

# Sanity cap so a corrupt/hostile header cannot make the gateway allocate
# unbounded memory. A full framebuffer transfer is <= 4096 bytes (spidev block),
# so 1 MiB is far above any legitimate record.
MAX_PAYLOAD = 1 << 20

ReadFn = Callable[[int], bytes]


def encode_record(dc: int, payload: bytes) -> bytes:
    """Encode one transfer record (header + payload)."""
    return _HEADER.pack(1 if dc else 0, len(payload)) + payload


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
        ``(dc, payload)``, or None on a clean EOF / truncated stream.

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
