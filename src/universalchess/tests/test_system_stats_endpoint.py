"""Tests for the GET /api/system/stats web endpoint.

Why these tests exist:
  The endpoint is the web "System" card's only data source. It must (a) return
  the exact flat JSON contract produced by ``SystemInfo.to_dict`` and (b) stay
  unauthenticated like the other read-only GET probes, so the card renders for
  any visitor. The psutil-backed reader is patched out here so the test is
  deterministic and needs no real sensors.
"""

import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402
from universalchess.board.system_info import (  # noqa: E402
    DiskSnapshot,
    MemorySnapshot,
    SystemInfo,
)

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open


GIB = 1024 ** 3

_SAMPLE_INFO = SystemInfo(
    hostname="dgt-test",
    cpu_percent=37.5,
    cpu_temperature_celsius=48.3,
    memory=MemorySnapshot(used_bytes=3 * GIB, total_bytes=8 * GIB, percent=37.5),
    disk=DiskSnapshot(used_bytes=10 * GIB, total_bytes=32 * GIB, percent=31.25),
    uptime_seconds=90061.0,
    load_average_1m=0.42,
)


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    # The endpoint does `from universalchess.board.system_info import get_system_info`
    # at call time; patch it on that module so the same object the endpoint imports
    # is replaced, independent of test ordering.
    import universalchess.board.system_info as system_info
    monkeypatch.setattr(system_info, "get_system_info", lambda *a, **k: _SAMPLE_INFO)
    return webapp.app.test_client()


def test_stats_returns_full_to_dict_contract(client):
    """The payload must equal SystemInfo.to_dict() exactly.

    Regression manifestation: if the endpoint reshaped, renamed, or dropped a
    field, this equality fails and the React card would read undefined values.
    """
    resp = client.get("/api/system/stats")
    assert resp.status_code == 200
    assert json.loads(resp.data) == _SAMPLE_INFO.to_dict()


def test_stats_requires_no_auth(monkeypatch):
    """Stats is a read-only probe and must work without credentials.

    Regression manifestation: accidentally decorating it with @requires_auth
    would return 401 here and the card would be empty for unauthenticated users.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    import universalchess.board.system_info as system_info
    monkeypatch.setattr(system_info, "get_system_info", lambda *a, **k: _SAMPLE_INFO)
    resp = webapp.app.test_client().get("/api/system/stats")
    assert resp.status_code == 200
