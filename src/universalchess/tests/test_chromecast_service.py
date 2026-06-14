"""Tests for ChromecastService multi-device bookkeeping.

The service owns one connection thread per device. These tests inject a fake
stream factory so the manager's add/stop/route logic is verified without
pychromecast or real threads (the connection loop itself talks to the network
and is exercised on hardware, not here).

Each test documents the regression it guards and how a failure manifests.
"""

import pytest

from universalchess.state.chromecast import reset_chromecast
from universalchess.services.chromecast import (
    ChromecastService,
    media_state_action,
    stream_path_for_source,
)


def test_stream_path_uses_live_board_source_when_enabled():
    """Live Board mode must be explicit in the Chromecast stream URL.

    Regression manifestation: the Chromecast keeps loading the unqualified
    /video endpoint and cannot distinguish the refreshed Live Board layout from
    Classic mode when the user toggles the setting.
    """
    assert stream_path_for_source(True) == "/video?source=live_board"


def test_stream_path_uses_classic_source_when_disabled():
    """Classic mode must be preserved for the e-paper side-by-side layout.

    Regression manifestation: unchecking the web/e-paper option still streams
    the Live Board layout, removing the only way to see the e-paper image beside
    the board.
    """
    assert stream_path_for_source(False) == "/video?source=classic"


@pytest.mark.parametrize(
    ("player_state", "expected_action"),
    [
        ("PLAYING", "keep"),
        ("BUFFERING", "keep"),
        ("PAUSED", "play"),
        ("IDLE", "reconnect"),
        (None, "keep"),
    ],
)
def test_media_state_action_keeps_receiver_awake(player_state, expected_action):
    """Paused Chromecast media must be resumed before screensaver starts.

    The Cast socket can stay connected while the media controller is PAUSED. If
    that is treated as healthy, the receiver eventually shows its screensaver.
    IDLE means playback has already ended, so the stream loop must reconnect.
    """
    assert media_state_action(player_state) == expected_action


class FakeStream:
    """Stand-in for a per-device connection: records start/stop, no threads."""

    def __init__(self, name, state):
        self.name = name
        self._state = state
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        # A real stream advances to streaming; emulate so aggregate views work.
        self._state.set_streaming(self.name)

    def stop(self):
        self.stopped += 1


@pytest.fixture
def service():
    state = reset_chromecast()
    created = []

    def factory(name):
        s = FakeStream(name, state)
        created.append(s)
        return s

    svc = ChromecastService(stream_factory=factory)
    svc._created = created  # expose for assertions
    return svc


def test_start_tracks_device_and_starts_one_stream(service):
    # Regression: a start that did not record the device (or started 0/2
    # threads) would leave active_devices wrong, so stop could not target it.
    assert service.start_streaming("A") is True
    assert service.active_devices == ["A"]
    assert len(service._created) == 1
    assert service._created[0].started == 1


def test_starting_same_device_twice_refreshes_stale_stream(service):
    # Starting an already tracked device refreshes that device. A previous no-op
    # left the UI stuck on reconnecting when the receiver had abandoned playback
    # but the board still held a stale stream object.
    service.start_streaming("A")
    assert service.start_streaming("A") is True
    assert service.active_devices == ["A"]
    assert len(service._created) == 2
    assert service._created[0].stopped == 1
    assert service._created[1].started == 1


def test_starting_second_device_keeps_the_first(service):
    # The whole point of multi-device: B must not stop A.
    # Regression: single-slot behaviour shows up as A's stream being stopped
    # (A.stopped == 1) and active_devices == ["B"].
    service.start_streaming("A")
    service.start_streaming("B")
    assert service.active_devices == ["A", "B"]
    assert service._created[0].stopped == 0  # A not stopped


def test_stop_one_device_leaves_others(service):
    # Per-device stop must target exactly one stream.
    # Regression: stopping "A" also tearing down "B" shows as B.stopped==1 and
    # an empty active_devices.
    service.start_streaming("A")
    service.start_streaming("B")
    service.stop_streaming("A")
    assert service.active_devices == ["B"]
    assert service._created[0].stopped == 1  # A stopped
    assert service._created[1].stopped == 0  # B untouched


def test_stop_all_stops_every_stream(service):
    # "Stop all" path. Regression: a leftover stream after stop-all keeps a
    # thread alive and the status icon visible; surfaces as non-empty
    # active_devices or a stream with stopped==0.
    service.start_streaming("A")
    service.start_streaming("B")
    service.stop_streaming()
    assert service.active_devices == []
    assert all(s.stopped == 1 for s in service._created)


def test_stop_unknown_device_is_noop(service):
    # Regression: stopping a device that was never started must not raise or
    # disturb existing streams.
    service.start_streaming("A")
    service.stop_streaming("ghost")
    assert service.active_devices == ["A"]
    assert service._created[0].stopped == 0


def test_start_requires_a_device_name(service):
    # Regression: an empty device name must be rejected, not spawn a stream
    # that can never connect.
    assert service.start_streaming("") is False
    assert service.start_streaming(None) is False
    assert service.active_devices == []
    assert service._created == []
