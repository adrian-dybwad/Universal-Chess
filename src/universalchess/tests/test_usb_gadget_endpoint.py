"""Tests for the /api/system/usb-gadget endpoints.

GET reports desired/live/prepared/expected-state (unauthenticated, like the
timezone and clock reads). POST changes the mode (authenticated) through
usb_gadget_service.set_mode. The service is patched so no privileged helper
runs; auth is forced so the route logic is exercised directly.
"""

import importlib
import sys

import pytest

from universalchess.tests.webapp_fixture import make_test_client

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *_a, **_k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp
finally:
    Image.open = _orig_image_open

from universalchess.services.usb_gadget_service import UsbGadgetStatus  # noqa: E402


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


def _patch_status(monkeypatch, **overrides):
    status = UsbGadgetStatus(
        desired=overrides.get("desired", "client"),
        live=overrides.get("live", "client"),
        prepared=overrides.get("prepared", True),
        in_expected_state=overrides.get("in_expected_state", True),
        reboot_required=overrides.get("reboot_required", False),
        attachment=overrides.get("attachment", "attached"),
        ipv4=overrides.get("ipv4"),
        dhcp_lease_count=overrides.get("dhcp_lease_count"),
        auto_switching=overrides.get("auto_switching"),
    )
    monkeypatch.setattr(
        "universalchess.services.usb_gadget_service.get_status",
        lambda **_kwargs: status,
    )
    return status


def test_get_usb_gadget_reports_desired_live_and_flags(client, monkeypatch):
    """GET returns the full status shape the System status card renders.

    Why: without prepared / in_expected_state / reboot_required / attachment the
    UI cannot tell a working Client link from one that still needs a reboot or
    has no host on the cable. How a regression manifests: a field is missing
    from the JSON or the route 500s.
    """
    _patch_status(
        monkeypatch,
        desired="shared",
        live="off",
        prepared=True,
        in_expected_state=False,
        reboot_required=False,
        attachment="not_attached",
        dhcp_lease_count=0,
        auto_switching=False,
    )
    resp = client.get("/api/system/usb-gadget")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {
        "desired": "shared",
        "live": "off",
        "prepared": True,
        "in_expected_state": False,
        "reboot_required": False,
        "attachment": "not_attached",
        "ipv4": None,
        "dhcp_lease_count": 0,
        "auto_switching": False,
    }


def test_get_usb_gadget_reports_the_switcher_state_alongside_live_mode(
    client, monkeypatch
):
    """Auto is reported as the switcher state plus whichever mode it holds.

    Why: ``live`` can only ever say Client or Shared, so on an Auto board the
    card needs ``auto_switching`` to explain why Desired Auto and Live Shared
    still match. Failure: the flag is missing and Auto looks like a mismatch that
    the user cannot resolve.
    """
    _patch_status(
        monkeypatch,
        desired="auto",
        live="shared",
        in_expected_state=True,
        ipv4="10.12.194.1",
        auto_switching=True,
    )
    body = client.get("/api/system/usb-gadget").get_json()
    assert body["desired"] == "auto"
    assert body["live"] == "shared"
    assert body["auto_switching"] is True
    assert body["in_expected_state"] is True


def test_post_usb_gadget_requires_auth(client, monkeypatch):
    """Unauthenticated POST is rejected.

    Why: changing gadget mode is privileged and can drop the USB session.
    Failure: POST succeeds without auth.
    """
    monkeypatch.setattr(
        "universalchess.services.usb_gadget_service.set_mode",
        lambda _mode, **_kwargs: True,
    )
    resp = client.post("/api/system/usb-gadget", json={"mode": "off"})
    assert resp.status_code in (401, 403)


@pytest.mark.parametrize("mode", ["off", "auto", "client", "shared"])
@pytest.mark.usefixtures("authed")
def test_post_usb_gadget_applies_each_mode(client, monkeypatch, mode):
    """POST accepts each catalog mode and returns applied.

    Why: the Connectivity select maps 1:1 onto these four helper verbs. Failure:
    a valid mode is rejected (400) or applied is missing.
    """
    seen = {}

    def _set(m, **_kwargs):
        seen["mode"] = m
        return True

    monkeypatch.setattr("universalchess.services.usb_gadget_service.set_mode", _set)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: True,
    )
    resp = client.post("/api/system/usb-gadget", json={"mode": mode})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["mode"] == mode
    assert body["applied"] is True
    assert seen["mode"] == mode


@pytest.mark.usefixtures("authed")
def test_post_usb_gadget_rejects_invalid_mode(client, monkeypatch):
    """Non-catalog modes are 400 and never call set_mode.

    Why: only off/auto/client/shared are safe helper verbs. Failure: 200 or
    set_mode invoked with garbage.
    """
    called = {"n": 0}
    monkeypatch.setattr(
        "universalchess.services.usb_gadget_service.set_mode",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1) or True,
    )
    resp = client.post("/api/system/usb-gadget", json={"mode": "bridge"})
    assert resp.status_code == 400
    assert called["n"] == 0


@pytest.mark.usefixtures("authed")
def test_post_usb_gadget_reports_unapplied(client, monkeypatch):
    """A failed privileged apply returns applied: false, not 500.

    Why: a hand-installed board without the sudoers grant must surface that the
    preference may have been stored but the OS did not change -- matching NTP.
    Failure: 500 or applied omitted / true.
    """
    monkeypatch.setattr(
        "universalchess.services.usb_gadget_service.set_mode",
        lambda _mode, **_kwargs: False,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: True,
    )
    resp = client.post("/api/system/usb-gadget", json={"mode": "client"})
    assert resp.status_code == 200
    assert resp.get_json()["applied"] is False
