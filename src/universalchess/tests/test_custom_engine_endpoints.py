"""HTTP contract tests for the custom-engine endpoints.

Background / why these tests exist
----------------------------------
The Settings page gains two ways to add a UCI engine the catalog does not ship:

* ``POST /api/engines/upload`` (multipart) -- upload a binary or .tar.gz.
* ``POST /api/engines/install-url`` (JSON) -- download one from an HTTPS URL.

Both mutate the system (write an executable that the board will later run), so
both are auth-gated like install/uninstall. These tests pin that contract: auth
enforcement, input validation (safe id, arch match, HTTPS+non-private URL),
single-active-install serialization, that custom engines appear in
``/api/engines/all``, and that uninstall removes them.

The real download/extraction is stubbed (mirroring test_engine_endpoints' use of
_SyncThread) so the endpoint layer is exercised deterministically without
network or compilation.
"""

import importlib
import io
import json
import sys
import threading

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402

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


# A documentation-range address that no SSRF rule treats as private.
_PUBLIC_TEST_ADDRESS = "93.184.216.34"


def _elf(arch: str = "arm64") -> bytes:
    """Minimal little-endian ARM ELF header (see test_custom_engines for detail)."""
    machine = {"arm64": 183, "armhf": 40}[arch]
    buf = bytearray(64)
    buf[0:4] = b"\x7fELF"
    buf[4] = 2 if arch == "arm64" else 1
    buf[5] = 1
    buf[18:20] = machine.to_bytes(2, "little")
    return bytes(buf) + b"\x00" * 16


class _SyncThread:
    """Run the worker inline on start() so dispatch is observable without a race."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)  # nosemgrep: python.flask.security.audit.hardcoded-config.avoid_hardcoded_config_TESTING
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture(autouse=True)
def install_store(tmp_path):
    """Isolate the install-state singleton per test (see test_engine_endpoints)."""
    from universalchess.services.engine_install_state import InstallStateStore

    original = webapp._engine_install_store
    store = InstallStateStore(tmp_path / "engine_install_state.json")
    webapp._engine_install_store = store
    yield store
    webapp._engine_install_store = original


@pytest.fixture(autouse=True)
def custom_env(tmp_path, monkeypatch):
    """Point the custom-engine store and engines dir at temp; pin arch to arm64.

    The engines dir is where uploads/url installs write the binary; pinning the
    device arch makes arch-validation deterministic regardless of the test host.
    """
    from universalchess.services.custom_engine_registry import CustomEngineRegistry

    store = CustomEngineRegistry(tmp_path / "custom_engines.json")
    engines_dir = tmp_path / "engines"
    engines_dir.mkdir()
    monkeypatch.setattr(webapp, "_custom_engine_store", store)
    monkeypatch.setattr(webapp, "_ENGINES_DIR", str(engines_dir))
    monkeypatch.setattr(webapp, "get_current_arch", lambda: "arm64")
    return store, engines_dir


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """Resolve named hosts to a fixed public address so the SSRF guard is offline.

    The URL-install route runs the guard before dispatching, and the guard
    resolves the host through ``socket.getaddrinfo``. Without this stub the
    routing tests depend on the host's resolver: a machine whose DNS or
    /etc/hosts maps example.com to 127.0.0.1 makes the guard reject the URL and
    the route answer 400, so tests about dispatch and 409 serialization fail for
    a reason unrelated to what they assert.

    Literal IP hosts resolve to themselves, exactly as a real resolver does, so
    the private/loopback rejection cases still exercise the guard's decision.
    Only the real guard's resolver is replaced, through the injection point
    validate_download_url already exposes for this purpose.
    """
    import ipaddress
    import socket

    from universalchess.services import custom_engines

    def offline_resolver(host, port, *a, **k):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = _PUBLIC_TEST_ADDRESS
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    real_validate = custom_engines.validate_download_url
    monkeypatch.setattr(
        custom_engines,
        "validate_download_url",
        lambda url: real_validate(url, resolver=offline_resolver),
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_requires_auth(monkeypatch):
    """Uploading a binary must require auth (401 when unauthenticated).

    Why: an uploaded file becomes an executable the board runs; this is at least
    as privileged as install. Manifestation if the decorator is dropped: an
    anonymous POST writes an executable and returns 200.
    """
    webapp.app.config.update(TESTING=True)  # nosemgrep: python.flask.security.audit.hardcoded-config.avoid_hardcoded_config_TESTING
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    resp = unauth.post(
        "/api/engines/upload",
        data={"id": "mine", "display_name": "Mine", "file": (io.BytesIO(_elf()), "engine")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401


def test_upload_success_places_binary_and_registers(client, custom_env):
    """A valid arm64 binary is stored under the engines dir and registered.

    Asserts the full success shape: 200, the file exists at engines_dir/<id> and
    is executable, and the registry now knows the engine as a custom upload.
    Manifestation if placement/registration regressed: the engine would not run
    or would not appear in the list.
    """
    store, engines_dir = custom_env
    resp = client.post(
        "/api/engines/upload",
        data={"id": "mine", "display_name": "My Engine", "file": (io.BytesIO(_elf("arm64")), "engine")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    import os
    placed = engines_dir / "mine"
    assert placed.exists() and os.access(placed, os.X_OK)
    entry = store.get("mine")
    assert entry is not None and entry.source == "upload" and entry.display_name == "My Engine"


def test_upload_rejects_invalid_id(client):
    """A traversal/invalid id is rejected with 400 and writes nothing.

    Manifestation if dropped: '../evil' would escape the engines dir.
    """
    resp = client.post(
        "/api/engines/upload",
        data={"id": "../evil", "display_name": "X", "file": (io.BytesIO(_elf()), "engine")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_upload_rejects_wrong_arch(client, custom_env):
    """A binary of the wrong arch is rejected with 400 and nothing is placed.

    Why: operator chose to reject mismatches. Manifestation if dropped: an armhf
    binary lands on an arm64 board and crashes when the engine is launched.
    """
    _, engines_dir = custom_env
    resp = client.post(
        "/api/engines/upload",
        data={"id": "mine", "display_name": "Mine", "file": (io.BytesIO(_elf("armhf")), "engine")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert not (engines_dir / "mine").exists()


def test_upload_missing_file_returns_400(client):
    """A request without a file part is rejected with 400.

    Manifestation if dropped: the handler would dereference a missing file and 500.
    """
    resp = client.post(
        "/api/engines/upload",
        data={"id": "mine", "display_name": "Mine"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Install from URL
# ---------------------------------------------------------------------------


def test_install_url_requires_auth(monkeypatch):
    """Installing from a URL must require auth (401 when unauthenticated)."""
    webapp.app.config.update(TESTING=True)  # nosemgrep: python.flask.security.audit.hardcoded-config.avoid_hardcoded_config_TESTING
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    resp = unauth.post(
        "/api/engines/install-url",
        data=json.dumps({"id": "mine", "display_name": "Mine", "url": "https://example.com/e.tar.gz"}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_install_url_starts_async_install(client, custom_env, monkeypatch):
    """A valid request dispatches the background URL-install worker and marks active.

    Mirrors the catalog install contract: the worker is stubbed and run inline so
    the dispatch and the in-progress store state are observable. Manifestation if
    the route regressed: the worker would not be dispatched and the UI would not
    show progress.
    """
    dispatched = []
    monkeypatch.setattr(
        webapp, "_run_custom_url_install",
        lambda engine_id, display_name, url: dispatched.append((engine_id, display_name, url)),
    )
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/install-url",
        data=json.dumps({"id": "mine", "display_name": "Mine", "url": "https://example.com/e.tar.gz"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert dispatched == [("mine", "Mine", "https://example.com/e.tar.gz")]
    assert webapp._engine_install_store.status_dict()["active"] is True


@pytest.mark.parametrize(
    "url",
    ["http://example.com/e.tar.gz", "https://127.0.0.1/e.tar.gz", "https://10.0.0.5/e.tar.gz", "not-a-url"],
)
def test_install_url_rejects_unsafe_url(client, monkeypatch, url):
    """Non-HTTPS and private/loopback targets are rejected with 400, no dispatch.

    Why: SSRF guard + HTTPS-only policy. Manifestation if dropped: a URL pointing
    at the board's own services or the LAN is fetched. Uses literal IPs so the
    check is deterministic and offline.
    """
    dispatched = []
    monkeypatch.setattr(
        webapp, "_run_custom_url_install",
        lambda *a, **k: dispatched.append(a),
    )
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/install-url",
        data=json.dumps({"id": "mine", "display_name": "Mine", "url": url}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert dispatched == []


def test_install_url_rejects_invalid_id(client):
    """An invalid engine id is rejected with 400."""
    resp = client.post(
        "/api/engines/install-url",
        data=json.dumps({"id": "Bad Id", "display_name": "Mine", "url": "https://example.com/e.tar.gz"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_install_url_while_installing_returns_409(client, monkeypatch):
    """A URL install while one install is running is rejected with 409.

    The board installs one engine at a time; the same serialization that guards
    catalog installs must guard URL installs. Manifestation if dropped: two
    installs race.
    """
    monkeypatch.setattr(webapp, "_run_custom_url_install", lambda *a, **k: None)
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    webapp._engine_install_store.start("stockfish", "Stockfish", estimated_seconds=0)

    resp = client.post(
        "/api/engines/install-url",
        data=json.dumps({"id": "mine", "display_name": "Mine", "url": "https://example.com/e.tar.gz"}),
        content_type="application/json",
    )
    assert resp.status_code == 409
    assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Listing + uninstall of custom engines
# ---------------------------------------------------------------------------


def test_all_engines_includes_installed_custom_engine(client, custom_env, monkeypatch):
    """A registered custom engine appears in /api/engines/all with the UI fields.

    Why: the Settings page renders every engine from this payload; custom engines
    must carry the same required fields so they render and can be uninstalled.
    Manifestation if the merge regressed: the custom engine is absent from the UI.
    """
    from universalchess.services.custom_engine_registry import CustomEngine

    store, engines_dir = custom_env
    # Force catalog engines not-installed so the assertion is filesystem-stable.
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.is_installed",
        lambda self, name: False,
    )
    store.add(CustomEngine(id="mine", display_name="My Engine", source="upload"))
    (engines_dir / "mine").write_bytes(_elf("arm64"))
    (engines_dir / "mine").chmod(0o755)

    data = client.get("/api/engines/all").get_json()
    by_name = {e["name"]: e for e in data}
    assert "mine" in by_name
    entry = by_name["mine"]
    required = {
        "name", "display_name", "summary", "description", "installed",
        "is_system_package", "can_uninstall", "estimated_install_minutes",
        "has_prebuilt", "has_profiles", "supported", "unsupported_reason",
    }
    assert required.issubset(entry.keys())
    assert entry["installed"] is True
    assert entry["is_custom"] is True
    assert entry["can_uninstall"] is True
    assert entry["is_system_package"] is False


def test_uninstall_custom_engine_removes_binary_and_entry(client, custom_env):
    """Uninstalling a custom engine deletes its binary and registry entry.

    Why: the shared uninstall endpoint must recognize custom ids (which are not in
    the catalog). Manifestation if dropped: uninstall would 400 ('unknown
    engine') and the file/entry would persist.
    """
    from universalchess.services.custom_engine_registry import CustomEngine

    store, engines_dir = custom_env
    store.add(CustomEngine(id="mine", display_name="Mine", source="upload"))
    placed = engines_dir / "mine"
    placed.write_bytes(_elf("arm64"))
    placed.chmod(0o755)

    resp = client.post(
        "/api/engines/uninstall",
        data=json.dumps({"engine": "mine"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert not placed.exists()
    assert not store.exists("mine")
