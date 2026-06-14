#!/usr/bin/env python3
"""Tests for the UI-agnostic Chromecast helpers (connectivity.chromecast).

discover() runs an mDNS scan via pychromecast; status_payload() maps the board's
observable state to a JSON dict for the web. Why these matter:
  * discover must de-duplicate and sort device names and always stop the browser
    (a leaked browser keeps an mDNS listener alive). It must also degrade to []
    when pychromecast is missing rather than 500 the endpoint.
  * status_payload must map every numeric state to a stable string and fall back
    to "idle" for an unknown value, so the web never receives an undefined state.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from universalchess.connectivity import chromecast as cast


class _FakeDevice:
    def __init__(self, name, cast_type="cast"):
        self.friendly_name = name
        self.cast_type = cast_type


class _FakeCast:
    def __init__(self, name, cast_type="cast"):
        self.device = _FakeDevice(name, cast_type)


def _install_fake_pychromecast(casts, browser):
    """Install a fake pychromecast module returning the given casts/browser."""
    module = types.ModuleType("pychromecast")
    module.get_chromecasts = lambda timeout=None: (casts, browser)
    sys.modules["pychromecast"] = module


class TestDiscover(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("pychromecast", None)

    def test_dedupes_sorts_and_stops_browser(self):
        """discover returns unique sorted names and stops the discovery browser.

        Failure manifestation: a duplicate "Living Room" would appear twice, an
        unsorted list would reorder the picker, and a leaked browser would keep
        an mDNS listener running. Asserts the result and the stop call.
        """
        browser = MagicMock()
        _install_fake_pychromecast(
            [_FakeCast("Living Room"), _FakeCast("Bedroom"), _FakeCast("Living Room")],
            browser,
        )
        names = cast.discover(log=MagicMock())
        assert names == ["Bedroom", "Living Room"]
        browser.stop_discovery.assert_called_once()

    def test_excludes_audio_and_group_devices(self):
        """Audio-only devices and audio groups are filtered out of discovery.

        Failure manifestation: a Google Home ("audio") or an audio "group" would
        appear in the picker but cannot display the board's video feed, so
        selecting it would silently fail to stream. Asserts only the video "cast"
        device survives.
        """
        browser = MagicMock()
        _install_fake_pychromecast(
            [
                _FakeCast("Living Room TV", cast_type="cast"),
                _FakeCast("Kitchen Speaker", cast_type="audio"),
                _FakeCast("Whole House", cast_type="group"),
            ],
            browser,
        )
        assert cast.discover(log=MagicMock()) == ["Living Room TV"]

    def test_keeps_device_with_unknown_cast_type(self):
        """A device whose cast_type is unknown/None is kept, not dropped.

        Failure manifestation: an older pychromecast that does not report
        cast_type would otherwise have every video device filtered out, leaving
        the picker empty. Only the known audio types should be excluded.
        """
        browser = MagicMock()
        _install_fake_pychromecast([_FakeCast("Mystery Cast", cast_type=None)], browser)
        assert cast.discover(log=MagicMock()) == ["Mystery Cast"]

    def test_missing_pychromecast_returns_empty(self):
        """A missing pychromecast yields [] (import guarded), not an exception.

        Failure manifestation: the discover endpoint would 500 on a host without
        pychromecast instead of reporting no devices.
        """
        sys.modules.pop("pychromecast", None)
        # Force import to fail by inserting a sentinel that raises on attribute use
        # is unnecessary: simply ensure the real module is absent in this env.
        if "pychromecast" in sys.modules:
            self.skipTest("pychromecast installed in this environment")
        assert cast.discover(log=MagicMock()) == []


class TestStatusPayload(unittest.TestCase):
    """status_payload over the real multi-device ChromecastState.

    Uses the real state object (it is pure) so the payload is verified against
    the actual snapshot/aggregate contract the web consumes.
    """

    def _state(self):
        from universalchess.state.chromecast import ChromecastState

        return ChromecastState()

    def test_idle_state_has_empty_device_list(self):
        """An idle board reports no devices and aggregate "idle".

        Failure manifestation: a phantom device entry would make the web show a
        stream that does not exist.
        """
        payload = cast.status_payload(self._state())
        assert payload == {
            "state": "idle",
            "device": None,
            "error": None,
            "devices": [],
        }

    def test_single_streaming_device(self):
        """A streaming device appears in devices[] and as the aggregate.

        Failure manifestation: a wrong mapping would show "idle" while streaming,
        misleading the web card.
        """
        state = self._state()
        state.set_streaming("Living Room")
        payload = cast.status_payload(state)
        assert payload["state"] == "streaming"
        assert payload["device"] == "Living Room"
        assert payload["devices"] == [
            {"name": "Living Room", "state": "streaming", "error": None}
        ]

    def test_multiple_devices_each_listed_with_own_state(self):
        """Each active device is listed with its own state and order preserved.

        Failure manifestation: a single-device payload would drop the second
        cast, so the web could neither show nor stop it. The aggregate stays
        "streaming" because one device streams.
        """
        state = self._state()
        state.set_streaming("Living Room")
        state.set_connecting("Bedroom")
        payload = cast.status_payload(state)
        assert payload["state"] == "streaming"
        assert payload["devices"] == [
            {"name": "Living Room", "state": "streaming", "error": None},
            {"name": "Bedroom", "state": "connecting", "error": None},
        ]

    def test_error_device_carries_message(self):
        """A device error keeps its message in its own entry.

        Failure manifestation: dropping the message would leave the user with no
        indication of why that device failed.
        """
        state = self._state()
        state.set_error("Office", "Device not found")
        payload = cast.status_payload(state)
        assert payload["devices"] == [
            {"name": "Office", "state": "error", "error": "Device not found"}
        ]
        assert payload["state"] == "error"
        assert payload["error"] == "Device not found"


if __name__ == "__main__":
    unittest.main()
