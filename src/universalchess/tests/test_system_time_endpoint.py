"""Tests for the /api/system/time and /api/system/ntp endpoints.

GET reports the device clock and its network-sync state (unauthenticated, like
the timezone read); the two POSTs change them (authenticated). The
system_time_service is patched so no privileged `timedatectl` call happens, and
auth is forced so the route logic is exercised directly.

The route's own job is thin but load-bearing: classifying a manual clock set that
cannot proceed (409, sync is on) apart from one that is nonsense (400, epoch out
of range), so the UI can say which. Both are covered here.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import make_test_client

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

from universalchess.services.system_time_service import (  # noqa: E402
    EPOCH_MAX_SECONDS,
    EPOCH_MIN_SECONDS,
    NetworkTimeSyncEnabledError,
    TimeStatus,
)

_EPOCH_IN_RANGE = 1800000000  # 2027-01-15T08:00:00Z


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


def _patch_status(monkeypatch, *, ntp_enabled, ntp_synchronised=False, epoch=_EPOCH_IN_RANGE):
    monkeypatch.setattr(
        "universalchess.services.system_time_service.get_status",
        lambda **kwargs: TimeStatus(
            epoch_seconds=epoch,
            ntp_enabled=ntp_enabled,
            ntp_synchronised=ntp_synchronised,
        ),
    )


def test_get_time_reports_the_clock_and_both_sync_flags(client, monkeypatch):
    """GET returns the device epoch, the timezone, and both NTP flags.

    This is what surfaces a wrong board clock in the UI at all. The two flags are
    reported separately because "sync is switched on" and "sync has actually
    happened" are different answers for a board with no route to a time server --
    exactly the case that made a five-minute clock error go unnoticed.
    """
    _patch_status(monkeypatch, ntp_enabled=True, ntp_synchronised=False)
    monkeypatch.setattr(
        "universalchess.services.timezone_service.get_timezone", lambda: "Europe/Oslo"
    )
    resp = client.get("/api/system/time")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "epoch_seconds": _EPOCH_IN_RANGE,
        "timezone": "Europe/Oslo",
        "ntp_enabled": True,
        "ntp_synchronised": False,
    }


def test_get_time_passes_unknown_sync_state_through_as_null(client, monkeypatch):
    """An undeterminable sync state is reported as null, not as false.

    The UI renders null as "unknown" and hides the manual-set control. If the
    route coerced it to a bool, the toggle would show a definite position the
    device never reported. Manifests as false in place of null here.
    """
    _patch_status(monkeypatch, ntp_enabled=None, ntp_synchronised=None)
    monkeypatch.setattr(
        "universalchess.services.timezone_service.get_timezone", lambda: "UTC"
    )
    resp = client.get("/api/system/time")
    body = json.loads(resp.data)
    assert body["ntp_enabled"] is None
    assert body["ntp_synchronised"] is None


@pytest.mark.parametrize("enabled,expected", [(True, "True"), (False, "False")])
def test_settings_payload_reports_the_live_network_sync_state(
    client, monkeypatch, enabled, expected
):
    """/api/settings reports system.ntp_enabled from the live OS, not the ini.

    Why: the Settings page's Network Time toggle initialises from this payload,
    and the state is owned by systemd -- nothing is persisted in centaur.ini, so
    there is no stored value that could be shown instead. How a regression
    manifests: the toggle sits in the wrong position on load and only corrects
    itself once the user moves it, which reads as the setting not sticking.
    """
    _patch_status(monkeypatch, ntp_enabled=enabled)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert json.loads(resp.data)["system"]["ntp_enabled"] == expected


def test_settings_payload_omits_network_sync_state_when_it_is_unknown(client, monkeypatch):
    """An undeterminable state leaves the key out rather than guessing.

    Why: the payload is stringly-typed with no null, so reporting "False" for
    "could not tell" would put a definite position on a toggle the device never
    reported -- the Device Clock card is what states "Unknown" honestly. How a
    regression manifests: a board whose state could not be read shows Network
    Time as off while the card beside it says Unknown.
    """
    _patch_status(monkeypatch, ntp_enabled=None)
    resp = client.get("/api/settings")
    assert "ntp_enabled" not in json.loads(resp.data)["system"]


@pytest.mark.parametrize("path,payload", [
    ("/api/system/ntp", {"enabled": False}),
    ("/api/system/time", {"epoch_seconds": _EPOCH_IN_RANGE}),
])
def test_post_requires_auth(client, path, payload):
    """Both writes reject an unauthenticated caller with 401.

    These change the device clock, which invalidates TLS certificates and
    reorders the event log. A regression dropping @requires_auth would let
    anyone on the network do that. Manifests as a non-401 status.
    """
    resp = client.post(path, json=payload)
    assert resp.status_code == 401


@pytest.mark.parametrize("enabled", [True, False])
def test_post_ntp_applies_and_notifies_the_board(client, authed, monkeypatch, enabled):
    """Toggling sync calls the service and tells the main process to re-read.

    The board menu shows the same toggle, so without the notify the e-paper
    would keep displaying the previous position until something else refreshed
    it. Manifests as a missing notify or a dropped/inverted enabled flag.
    """
    calls = {}
    notified = {"count": 0}
    monkeypatch.setattr(
        "universalchess.services.system_time_service.set_ntp_enabled",
        lambda value: calls.setdefault("enabled", value) or True,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: notified.__setitem__("count", notified["count"] + 1),
    )

    resp = client.post("/api/system/ntp", json={"enabled": enabled})
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "success": True, "ntp_enabled": enabled, "applied": True
    }
    assert calls["enabled"] is enabled
    assert notified["count"] == 1


@pytest.mark.parametrize("payload", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}])
def test_post_ntp_rejects_a_non_boolean_enabled(client, authed, monkeypatch, payload):
    """Anything but a JSON boolean is a 400 and applies nothing.

    Guards against truthiness coercion: the string "false" and the integer 0 are
    both plausible client bugs, and one of them would silently mean the opposite
    of what was sent. Manifests as a 200 with the wrong state applied.
    """
    applied = {"count": 0}
    monkeypatch.setattr(
        "universalchess.services.system_time_service.set_ntp_enabled",
        lambda value: applied.__setitem__("count", applied["count"] + 1) or True,
    )
    resp = client.post("/api/system/ntp", json=payload)
    assert resp.status_code == 400
    assert applied["count"] == 0


def test_post_ntp_apply_failure_reports_not_applied(client, authed, monkeypatch):
    """A failed privileged apply still returns 200 with applied=false.

    Mirrors the timezone contract: a hand-installed board missing the sudo grant
    should be told the change did not take, not handed a 500.
    """
    monkeypatch.setattr(
        "universalchess.services.system_time_service.set_ntp_enabled", lambda value: False
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: True,
    )
    resp = client.post("/api/system/ntp", json={"enabled": True})
    assert resp.status_code == 200
    assert json.loads(resp.data)["applied"] is False


def test_post_time_sets_the_clock_from_the_supplied_epoch(client, authed, monkeypatch):
    """With sync off, the epoch is handed to the service and applied.

    This is the path that fixes a board with no time source: the browser posts
    its own clock. Guards that the route forwards the epoch and the sync state
    it read, rather than letting the service re-read it.
    """
    _patch_status(monkeypatch, ntp_enabled=False)
    calls = {}

    def fake_set_clock(epoch_seconds, *, ntp_enabled):
        calls["epoch"] = epoch_seconds
        calls["ntp_enabled"] = ntp_enabled
        return True

    monkeypatch.setattr(
        "universalchess.services.system_time_service.set_clock", fake_set_clock
    )
    resp = client.post("/api/system/time", json={"epoch_seconds": _EPOCH_IN_RANGE + 0.5})
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"success": True, "applied": True}
    assert calls == {"epoch": _EPOCH_IN_RANGE + 0.5, "ntp_enabled": False}


def test_post_time_is_409_while_network_sync_is_enabled(client, authed, monkeypatch):
    """A refused manual set is 409, distinct from a malformed one.

    409 is what lets the UI say "turn network time sync off first" instead of
    "invalid request". Collapsing it into 400 or 500 loses the only actionable
    part of the message. Manifests as the wrong status for a board with sync on.
    """
    _patch_status(monkeypatch, ntp_enabled=True)

    def fake_set_clock(epoch_seconds, *, ntp_enabled):
        raise NetworkTimeSyncEnabledError("sync is on")

    monkeypatch.setattr(
        "universalchess.services.system_time_service.set_clock", fake_set_clock
    )
    resp = client.post("/api/system/time", json={"epoch_seconds": _EPOCH_IN_RANGE})
    assert resp.status_code == 409


@pytest.mark.parametrize("payload", [
    {},
    {"epoch_seconds": "1800000000"},
    {"epoch_seconds": None},
    {"epoch_seconds": True},
    {"epoch_seconds": EPOCH_MIN_SECONDS - 1},
    {"epoch_seconds": EPOCH_MAX_SECONDS + 1},
])
def test_post_time_rejects_a_missing_or_out_of_range_epoch(
    client, authed, monkeypatch, payload
):
    """Non-numeric, absent, or out-of-range epochs are 400 and step nothing.

    `True` is in the list because Python treats bools as ints, so a naive
    isinstance check would accept it and try to step the clock to 1970.
    Manifests as a 200, or as a privileged call for a value the helper would
    refuse anyway.
    """
    _patch_status(monkeypatch, ntp_enabled=False)
    stepped = {"count": 0}
    monkeypatch.setattr(
        "universalchess.services.system_time_service._run_helper",
        lambda run, helper_args: stepped.__setitem__("count", stepped["count"] + 1) or True,
    )
    resp = client.post("/api/system/time", json=payload)
    assert resp.status_code == 400
    assert stepped["count"] == 0
