"""Tests for the centaur display gateway (wire protocol + stream processing).

Background / why these tests exist
----------------------------------
The LD_PRELOAD shim streams centaur's DC-tagged SPI transfers to the UC process
over a unix socket. The gateway frames/parses those transfers, drives the
decoder, and renders each completed frame via display_frame(). These tests pin
the wire framing (so shim and gateway agree byte-for-byte), partial-read
robustness (sockets deliver arbitrary chunk sizes), clean EOF handling, and that
exactly one render happens per refresh.
"""

import os
import socket
import tempfile
import threading
import time
from io import BytesIO

import pytest
from PIL import Image, ImageChops

from universalchess.epaper.framework.waveshare.epd2in9d import pack_image_to_buffer
from universalchess.services.centaur_display import PANEL_WIDTH, PANEL_HEIGHT
from universalchess.services.centaur_display.protocol import encode_record, read_record
from universalchess.services.centaur_display.gateway import (
    CentaurDisplayGateway,
    ThreadedGatewayServer,
    render_and_signal,
)

DC_COMMAND = 0
DC_DATA = 1


def _pattern_image():
    img = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 255)
    for y in range(40):
        for x in range(24):
            img.putpixel((x, y), 0)
    img.putpixel((PANEL_WIDTH - 1, PANEL_HEIGHT - 1), 0)
    return img


def _frame_records(image, ram_cmd=0x13, refresh_cmd=0x12, chunk=4096):
    """Encode a full UC8151D frame as wire records: RAM-write, data chunks, refresh."""
    packed = bytes(pack_image_to_buffer(image, PANEL_WIDTH, PANEL_HEIGHT))
    blob = encode_record(DC_COMMAND, bytes([ram_cmd]))
    for i in range(0, len(packed), chunk):
        blob += encode_record(DC_DATA, packed[i:i + chunk])
    blob += encode_record(DC_COMMAND, bytes([refresh_cmd]))
    return blob


def _reader(data):
    """A recv-like read_fn over a byte buffer (returns up to n bytes, b'' at EOF)."""
    buf = BytesIO(data)
    return lambda n: buf.read(n)


def _byte_at_a_time_reader(data):
    """A read_fn that returns at most ONE byte per call (worst-case fragmentation)."""
    buf = BytesIO(data)
    return lambda n: buf.read(1)


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------

def test_record_round_trips_through_encode_and_read():
    """encode_record + read_record must preserve dc flag and payload exactly.

    Guards the shim<->gateway contract. A framing regression (wrong header size/
    endianness) makes the parsed dc/payload differ from what was encoded.
    """
    blob = encode_record(DC_DATA, b"\x01\x02\x03\xff")
    dc, payload = read_record(_reader(blob))

    assert dc == DC_DATA
    assert payload == b"\x01\x02\x03\xff"


def test_read_record_reassembles_across_fragmented_reads():
    """read_record must reassemble a record split across many tiny reads.

    Sockets deliver arbitrary chunk sizes; the header or payload can arrive one
    byte at a time. Failure manifests as a truncated/garbled payload or a wrong
    length. Asserts a 5000-byte payload survives 1-byte-at-a-time delivery.
    """
    payload = bytes(range(256)) * 20  # 5120 bytes, spans many reads
    blob = encode_record(DC_DATA, payload)

    dc, got = read_record(_byte_at_a_time_reader(blob))

    assert dc == DC_DATA
    assert got == payload


def test_read_record_returns_none_on_clean_eof():
    """read_record must return None when the stream is closed at a boundary.

    The accept loop uses None to detect disconnect. Failure manifests as a hang
    or exception instead of a clean None.
    """
    assert read_record(_reader(b"")) is None


# ---------------------------------------------------------------------------
# Gateway stream processing
# ---------------------------------------------------------------------------

def test_gateway_renders_decoded_frame_on_refresh():
    """A full frame in the stream must produce exactly one render of that image.

    End-to-end through the real decoder: guards that protocol + decoder + render
    wiring reconstruct the exact image and fire once per refresh. Failure
    manifests as zero/multiple renders or a mismatched image.
    """
    original = _pattern_image()
    rendered = []

    gateway = CentaurDisplayGateway(render_fn=rendered.append)
    gateway.run_stream(_reader(_frame_records(original)))

    assert len(rendered) == 1
    assert ImageChops.difference(rendered[0].convert("1"), original.convert("1")).getbbox() is None


def test_gateway_renders_once_per_refresh_for_multiple_frames():
    """Two frames in the stream must render twice, latest content last.

    Guards that the decoder/gateway reset between frames and do not coalesce or
    drop. Failure manifests as a render count != 2.
    """
    first = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 255)
    second = _pattern_image()
    rendered = []

    gateway = CentaurDisplayGateway(render_fn=rendered.append)
    gateway.run_stream(_reader(_frame_records(first) + _frame_records(second)))

    assert len(rendered) == 2
    assert ImageChops.difference(rendered[1].convert("1"), second.convert("1")).getbbox() is None


def test_gateway_stops_cleanly_on_eof_without_rendering_partial():
    """A stream that ends before the refresh opcode must render nothing.

    Guards against painting a half-streamed buffer when centaur/shim disconnects
    mid-frame. Failure manifests as a render call despite no refresh.
    """
    original = _pattern_image()
    packed = bytes(pack_image_to_buffer(original, PANEL_WIDTH, PANEL_HEIGHT))
    # RAM-write + data, but NO refresh opcode.
    partial = encode_record(DC_COMMAND, bytes([0x13])) + encode_record(DC_DATA, packed)
    rendered = []

    gateway = CentaurDisplayGateway(render_fn=rendered.append)
    gateway.run_stream(_reader(partial))

    assert rendered == []


def test_render_and_signal_sets_event_after_successful_render():
    """The first-frame Event must fire only after render_fn returns.

    Why this test exists: translate mode releases the serial hold on this event.
    If the wrapper set the event before render_fn, a raising display_frame would
    still unblock battery traffic and Centaur would crash on None.tobytes().
    How the regression manifests: the event is set when render_fn raises, or is
    never set when it succeeds.
    """
    rendered = []
    event = threading.Event()
    wrapped = render_and_signal(rendered.append, event)

    assert not event.is_set()
    wrapped(_pattern_image())
    assert event.is_set()
    assert len(rendered) == 1


def test_render_and_signal_does_not_set_event_when_render_raises():
    """A failing paint must not release the serial hold.

    Why this test exists: same race as test_render_and_signal_sets_event_after
    successful_render, the failure direction. How the regression manifests: the
    event is set even though render_fn raised.
    """
    event = threading.Event()

    def boom(_image):
        raise RuntimeError("panel busy")

    wrapped = render_and_signal(boom, event)
    with pytest.raises(RuntimeError):
        wrapped(_pattern_image())
    assert not event.is_set()


# ---------------------------------------------------------------------------
# ThreadedGatewayServer (real unix-socket boundary)
# ---------------------------------------------------------------------------

def _wait_until(predicate, timeout=5.0):
    """Poll predicate until true or timeout; returns the final boolean."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_threaded_server_renders_frame_from_a_connected_client():
    """A client that sends a frame over the real socket must get it rendered.

    This is the live-process boundary: the server serves on a background thread
    while the (here, test) client streams a frame. It pins that start() binds and
    accepts, the decode/render path runs off-thread, and the exact image is
    reconstructed. A regression in the thread/socket wiring manifests as no
    render within the timeout or a mismatched image.
    """
    original = _pattern_image()
    rendered = []
    # Lock guards the list across the gateway thread and the test thread.
    lock = threading.Lock()

    def render(image):
        with lock:
            rendered.append(image)

    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "centaur-display.sock")
        server = ThreadedGatewayServer(
            CentaurDisplayGateway(render_fn=render), socket_path=sock_path)
        server.start()
        try:
            assert _wait_until(lambda: os.path.exists(sock_path)), "server never bound"
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(sock_path)
            client.sendall(_frame_records(original))
            assert _wait_until(lambda: len(rendered) == 1), "frame not rendered"
            client.close()
        finally:
            server.stop()

    with lock:
        assert len(rendered) == 1
        assert ImageChops.difference(
            rendered[0].convert("1"), original.convert("1")).getbbox() is None


def test_threaded_server_stop_joins_the_thread():
    """stop() must terminate the accept loop and join the serve thread.

    Guards against a leaked/zombie gateway thread (and a held socket file) after
    a centaur session ends. Failure manifests as the thread still being alive
    after stop() returns.
    """
    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "centaur-display.sock")
        server = ThreadedGatewayServer(
            CentaurDisplayGateway(render_fn=lambda img: None), socket_path=sock_path)
        server.start()
        assert _wait_until(lambda: os.path.exists(sock_path)), "server never bound"
        thread = server._thread
        assert thread is not None and thread.is_alive()

        server.stop()

        assert not thread.is_alive()
        # The socket file is cleaned up on serve() exit.
        assert _wait_until(lambda: not os.path.exists(sock_path))


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
