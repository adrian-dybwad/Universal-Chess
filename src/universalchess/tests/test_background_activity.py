"""Tests for the background-activity aggregation behind the web banner.

The web UI shows a top-of-screen banner while the board does long-running work
(an engine install, a BlueZ self-heal). ``services/background_activity`` turns
the two existing structured status sources into one uniform list the banner
renders generically. These tests pin:

1. Each source surfaces as a banner row only while it is *actually running*
   (active install / running heal) and is absent otherwise, so the banner never
   claims work is happening when the board is idle.
2. The row carries the fields the banner needs (id, kind, label, message,
   percent), with percent coerced to a usable 0-100 int or ``None``
   (indeterminate) -- never a fabricated determinate position from a bad value.
3. ``GET /api/system/activity`` wires the live sources into the aggregator and
   returns the ``{"active", "activities"}`` contract the frontend consumes.
"""

import importlib
import sys

import pytest

from universalchess.managers.bluez_patch_status import (
    HEAL_BUILDING,
    make_progress,
)
from universalchess.services.background_activity import (
    ACTIVITY_BLUEZ_SELFHEAL,
    ACTIVITY_CENTAUR_IMPORT,
    ACTIVITY_ENGINE_INSTALL,
    KIND_BLUEZ_SELFHEAL,
    KIND_CENTAUR_IMPORT,
    KIND_ENGINE_INSTALL,
    activity_snapshot,
    build_activities,
)
from universalchess.tests.webapp_fixture import make_test_client


def _engine_status(active=True, display_name="Koivisto",
                   message="Building Koivisto...", percent=42):
    """An engine-install status dict shaped like engine_install_state.status_dict."""
    return {
        "active": active,
        "installing": active,
        "engine": "koivisto",
        "display_name": display_name,
        "stage": "building",
        "message": message,
        "percent": percent,
        "interrupted": False,
        "result": None,
    }


def _idle_engine_status():
    """No install: the shape status_dict returns when the store is empty."""
    return {
        "active": False,
        "installing": False,
        "engine": None,
        "display_name": None,
        "stage": None,
        "message": "",
        "percent": 0,
        "interrupted": False,
        "result": None,
    }


def _centaur_import_status(active=True, message="Installing 32-bit support...",
                           percent=60, interrupted=False):
    """A Centaur-import status dict shaped like import_state.status_dict."""
    return {
        "active": active,
        "stage": "installing_armhf",
        "message": message,
        "percent": percent,
        "interrupted": interrupted,
        "started_at": 1.0,
        "result": None,
    }


def _idle_centaur_import_status():
    """No import running: the shape import_state.status_dict returns when empty."""
    return {
        "active": False,
        "stage": None,
        "message": "",
        "percent": 0,
        "interrupted": False,
        "started_at": None,
        "result": None,
    }


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def test_no_activity_when_both_idle():
    # Guards the idle baseline: with no install and no heal the banner must show
    # nothing. A regression that surfaced an idle source would pin a permanent
    # banner on screen, so active must be False AND the list empty.
    snapshot = activity_snapshot(_idle_engine_status(), make_progress(running=False))
    assert snapshot == {"active": False, "activities": []}


def test_active_engine_install_surfaces_one_row():
    # The headline case: an active install must produce exactly one row carrying
    # the headline label, the live stage message, and the server-computed
    # percent. A missing field here is what the banner would render blank.
    activities = build_activities(_engine_status(percent=42), make_progress(running=False))
    assert activities == [{
        "id": ACTIVITY_ENGINE_INSTALL,
        "kind": KIND_ENGINE_INSTALL,
        "label": "Installing Koivisto",
        "message": "Building Koivisto...",
        "percent": 42,
    }]


def test_interrupted_install_is_not_surfaced():
    # An install that was interrupted (active False, interrupted True) is no
    # longer "going on" -- Settings owns its resume UI. If this leaked into the
    # banner it would falsely claim a build is running, so the list must be empty.
    status = _engine_status(active=True)
    status["active"] = False
    status["interrupted"] = True
    assert build_activities(status, make_progress(running=False)) == []


def test_active_centaur_import_surfaces_one_row():
    # A running Centaur import must produce exactly one banner row carrying the
    # headline label, the live stage message (e.g. "Installing 32-bit support..."),
    # and the server-computed percent -- the whole point of this feature is that
    # the long post-upload install is visible, not a bar frozen at 100%.
    activities = build_activities(
        _idle_engine_status(), make_progress(running=False),
        _centaur_import_status(percent=60),
    )
    assert activities == [{
        "id": ACTIVITY_CENTAUR_IMPORT,
        "kind": KIND_CENTAUR_IMPORT,
        "label": "Importing original Centaur",
        "message": "Installing 32-bit support...",
        "percent": 60,
    }]


def test_interrupted_centaur_import_is_not_surfaced():
    # An import interrupted by a restart (active False, interrupted True) is no
    # longer running; surfacing it would falsely claim an import is in progress.
    status = _centaur_import_status(active=False, interrupted=True)
    assert build_activities(_idle_engine_status(), make_progress(running=False), status) == []


def test_idle_centaur_import_is_not_surfaced():
    # The idle import shape must contribute no row, so a completed/never-run import
    # does not pin a permanent banner.
    activities = build_activities(
        _idle_engine_status(), make_progress(running=False), _idle_centaur_import_status()
    )
    assert activities == []


def test_all_three_active_keep_fixed_order():
    # With engine install, Centaur import, and self-heal all running, the rows
    # appear in a fixed order so the banner does not reorder between polls. Count
    # check catches a dropped row; id-order check catches reordering.
    progress = make_progress(running=True, phase=HEAL_BUILDING, started_at="2026-06-27T06:54:56Z")
    activities = build_activities(_engine_status(), progress, _centaur_import_status())
    assert [a["id"] for a in activities] == [
        ACTIVITY_ENGINE_INSTALL,
        ACTIVITY_CENTAUR_IMPORT,
        ACTIVITY_BLUEZ_SELFHEAL,
    ]


def test_running_selfheal_surfaces_indeterminate_row():
    # A running heal must surface with the shared phase label and NO percent
    # (the rebuild reports none -> indeterminate bar). Regression: passing a
    # percent here would render a fake determinate bar that never completes.
    progress = make_progress(running=True, phase=HEAL_BUILDING, started_at="2026-06-27T06:54:56Z")
    activities = build_activities(_idle_engine_status(), progress)
    assert activities == [{
        "id": ACTIVITY_BLUEZ_SELFHEAL,
        "kind": KIND_BLUEZ_SELFHEAL,
        "label": "Building Bluetooth fix (up to 45 min)...",
        "message": None,
        "percent": None,
    }]


def test_idle_selfheal_is_not_surfaced():
    # heal_label returns None for an idle progress record, so no row appears.
    # Guards against a stale phase in an idle record pinning a heal banner.
    assert build_activities(_idle_engine_status(), make_progress(running=False)) == []


def test_both_active_keep_fixed_order():
    # When both run concurrently (the exact situation that motivated the banner),
    # both rows appear in a fixed order (engine install, then self-heal) so the
    # banner does not reorder between polls. The count check catches a dropped
    # row; the id-order check catches reordering.
    progress = make_progress(running=True, phase=HEAL_BUILDING, started_at="2026-06-27T06:54:56Z")
    activities = build_activities(_engine_status(), progress)
    assert [a["id"] for a in activities] == [ACTIVITY_ENGINE_INSTALL, ACTIVITY_BLUEZ_SELFHEAL]


@pytest.mark.parametrize(
    "raw_percent, expected",
    [
        (0, 0),
        (42, 42),
        (100, 100),
        (150, 100),     # over-range clamps to the ceiling, not passed through
        (-5, 0),        # under-range clamps to the floor
        (None, None),   # missing percent -> indeterminate
        ("oops", None), # non-numeric must not become 0
        (True, None),   # bool must not be read as 1 (it is not a real percent)
    ],
)
def test_percent_is_coerced_to_usable_value(raw_percent, expected):
    # Percent drives a determinate bar; a bad value must degrade to None
    # (indeterminate) rather than a fabricated position. Each case targets a
    # distinct corruption mode the bar would otherwise misrender.
    activities = build_activities(_engine_status(percent=raw_percent), make_progress(running=False))
    assert activities[0]["percent"] == expected


def test_engine_display_name_falls_back_when_missing():
    # display_name can be absent on a malformed status; the label must still be
    # meaningful ("Installing engine") rather than "Installing None".
    status = _engine_status(display_name=None)
    activities = build_activities(status, make_progress(running=False))
    assert activities[0]["label"] == "Installing engine"


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def webapp():
    """Import the Flask app with the same shims the other web-endpoint tests use.

    The app module builds a DB engine against /opt and opens a packaged logo at
    import time, neither present in a checkout; stubbing both lets the module
    import in CI/dev.
    """
    pytest.importorskip("flask")
    pytest.importorskip("sqlalchemy")
    from PIL import Image

    import universalchess.db.uri as uri
    uri.get_database_uri = lambda: "sqlite:///:memory:"
    orig_open = Image.open
    Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
    try:
        if "universalchess.web.app" in sys.modules:
            module = importlib.reload(sys.modules["universalchess.web.app"])
        else:
            import universalchess.web.app as module
    finally:
        Image.open = orig_open
    return module


def test_activity_endpoint_wires_both_sources(webapp, monkeypatch):
    # End-to-end contract: the endpoint must read the engine store AND the heal
    # progress and return the aggregated snapshot. Faking both live sources to a
    # running state proves the wiring -- a regression that read only one source
    # would drop the other row.
    client = make_test_client(webapp)

    class _FakeStore:
        def status_dict(self):
            return _engine_status(percent=42)

    monkeypatch.setattr(webapp, "_engine_install_store", _FakeStore())
    monkeypatch.setattr(
        "universalchess.managers.bluez_patch_status.read_progress",
        lambda: make_progress(running=True, phase=HEAL_BUILDING, started_at="2026-06-27T06:54:56Z"),
    )

    resp = client.get("/api/system/activity")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["active"] is True
    assert [a["id"] for a in body["activities"]] == [
        ACTIVITY_ENGINE_INSTALL,
        ACTIVITY_BLUEZ_SELFHEAL,
    ]
    assert body["activities"][0]["percent"] == 42
    assert body["activities"][1]["percent"] is None


def test_activity_endpoint_idle_returns_empty(webapp, monkeypatch):
    # The idle path the frontend uses to hide the banner: no install, no heal ->
    # active False, empty list. Guards against the endpoint emitting a phantom
    # activity when nothing is running.
    client = make_test_client(webapp)

    class _IdleStore:
        def status_dict(self):
            return _idle_engine_status()

    monkeypatch.setattr(webapp, "_engine_install_store", _IdleStore())
    monkeypatch.setattr(
        "universalchess.managers.bluez_patch_status.read_progress",
        lambda: make_progress(running=False),
    )

    resp = client.get("/api/system/activity")
    assert resp.status_code == 200
    assert resp.get_json() == {"active": False, "activities": []}
