"""Tests for the multi-device ChromecastState.

These guard the per-device model that lets the board stream to several
Chromecasts at once while still exposing a single aggregate view for the
e-paper status-bar icon (which can only show one indicator).

Each test documents the regression it protects against and how a failure
manifests, so an unrelated breakage surfacing through the same test can be
distinguished from the specific behaviour under test.
"""

from universalchess.state.chromecast import (
    ChromecastState,
    STATE_IDLE,
    STATE_CONNECTING,
    STATE_STREAMING,
    STATE_RECONNECTING,
    STATE_ERROR,
)


def _names(snapshot):
    return [d["name"] for d in snapshot]


def test_fresh_state_is_idle_with_no_devices():
    # Guards the empty/initial contract the status bar and web rely on.
    # Regression: if a fresh state reported active or carried a phantom
    # device, the status icon would render and the web would show a stream
    # that does not exist.
    state = ChromecastState()
    assert state.is_idle is True
    assert state.is_active is False
    assert state.is_streaming is False
    assert state.is_error is False
    assert state.state == STATE_IDLE
    assert state.device_name is None
    assert state.error_message is None
    assert state.snapshot() == []
    assert state.active_device_names() == []


def test_two_devices_tracked_independently():
    # The core multi-device guarantee: starting a second device must NOT
    # evict the first. Regression: a single-slot model would drop "A" when
    # "B" starts, so snapshot() would show one device instead of two.
    state = ChromecastState()
    state.set_streaming("A")
    state.set_connecting("B")

    snap = state.snapshot()
    assert _names(snap) == ["A", "B"]
    assert state.active_device_names() == ["A", "B"]
    assert state.is_active is True
    assert state.is_streaming is True  # A is streaming


def test_aggregate_state_prefers_streaming_for_status_icon():
    # The status bar shows ONE icon, so the aggregate must pick the most
    # "connected" state. Regression: if aggregation returned CONNECTING while
    # another device is STREAMING, the filled icon would flip to an outline
    # even though a live stream exists.
    state = ChromecastState()
    state.set_connecting("A")
    state.set_streaming("B")
    assert state.state == STATE_STREAMING
    # device_name (back-compat single value) points at an active device.
    assert state.device_name in ("A", "B")


def test_aggregate_state_connecting_when_none_streaming():
    # Regression: with one connecting and one reconnecting device and none
    # streaming, the icon must still indicate in-progress (outline), not idle.
    state = ChromecastState()
    state.set_connecting("A")
    state.set_reconnecting("B")
    assert state.state in (STATE_CONNECTING, STATE_RECONNECTING)
    assert state.is_active is True


def test_error_does_not_mask_a_live_stream():
    # A failed device must not pull is_active false while another streams.
    # Regression: if any-error short-circuited is_active, the status icon
    # would vanish mid-stream when one of several casts errors.
    state = ChromecastState()
    state.set_streaming("A")
    state.set_error("B", "boom")
    assert state.is_active is True
    assert state.is_error is False  # not ALL devices are errored
    assert state.state == STATE_STREAMING
    assert state.error_message == "boom"


def test_is_error_only_when_all_devices_errored():
    # Regression: is_error must reflect that there is nothing else working, so
    # the error icon only shows when every tracked device has failed.
    state = ChromecastState()
    state.set_error("A", "x")
    state.set_error("B", "y")
    assert state.is_error is True
    assert state.is_active is False
    assert state.state == STATE_ERROR


def test_set_idle_removes_single_device():
    # Stopping one stream must leave the others intact.
    # Regression: a global set_idle() would clear "B" too, silently killing a
    # stream the user did not stop.
    state = ChromecastState()
    state.set_streaming("A")
    state.set_streaming("B")
    state.set_idle("A")
    assert _names(state.snapshot()) == ["B"]
    assert state.is_active is True


def test_set_idle_all_clears_everything():
    # "Stop all" path. Regression: a lingering device after a global stop
    # would keep the status icon visible and the web showing a phantom stream.
    state = ChromecastState()
    state.set_streaming("A")
    state.set_connecting("B")
    state.set_idle()
    assert state.snapshot() == []
    assert state.is_idle is True


def test_restarting_a_device_replaces_its_entry_not_duplicates():
    # Re-setting an existing device updates it in place.
    # Regression: appending instead of updating would list the same device
    # twice, doubling rows in the web UI and confusing "stop" routing.
    state = ChromecastState()
    state.set_connecting("A")
    state.set_streaming("A")
    snap = state.snapshot()
    assert _names(snap) == ["A"]
    assert snap[0]["state"] == STATE_STREAMING


def test_observers_notified_on_every_mutation():
    # The status widget and web mirror repaint via observer callbacks.
    # Regression: a missing _notify on any mutation would freeze the icon /
    # web state on a stale value. Counts every mutation kind once.
    state = ChromecastState()
    calls = []
    state.add_observer(lambda: calls.append(1))
    state.set_connecting("A")
    state.set_streaming("A")
    state.set_reconnecting("A")
    state.set_error("A", "e")
    state.set_idle("A")  # removes A -> empty
    state.set_connecting("B")  # a real change again
    assert len(calls) == 6


def test_redundant_stop_all_does_not_notify():
    # set_idle() on an already-empty state is a no-op and must NOT notify, so
    # the status icon does not needlessly repaint.
    # Regression: notifying on an empty stop-all would cause spurious e-paper
    # refreshes (each refresh is visible and costs a partial update).
    state = ChromecastState()
    calls = []
    state.add_observer(lambda: calls.append(1))
    state.set_idle()
    state.set_idle("nonexistent")
    assert calls == []
