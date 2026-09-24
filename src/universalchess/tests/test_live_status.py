"""The web UI's repeated status polls collapse onto one live_status snapshot.

Why these tests exist:
  Activity, Wi-Fi, Bluetooth, and update status were each fetched on a timer
  by every open browser. The persistent /events connection already carries
  game, battery, and clock updates, so those four reads belong on it too:
  one sample per source while a client is connected, published only when the
  snapshot changes, and a missing radio is not read at all.

Regression manifestation:
  - an unchanged second sample is published again (the stream chatters while idle)
  - a Wi-Fi or Bluetooth read runs on a board whose capability says that radio
    is absent
  - a source whose interval has not elapsed is read again on the next tick
"""

from universalchess.web.live_status import LiveStatusPump, LiveStatusSchedule


class _Capability:
    def __init__(self, has_wifi, has_bluetooth):
        self.has_wifi = has_wifi
        self.has_bluetooth = has_bluetooth


def test_schedule_skips_radios_the_board_does_not_have():
    """A board with neither radio must not read Wi-Fi or Bluetooth.

    Why: those reads shell out. On a plain Pi Zero they describe hardware that
    is not fitted, and the old navbar polls did them anyway until the
    capability probe returned. The snapshot reports both as None.
    Regression: the calls list contains "wifi" or "bluetooth".
    """
    calls = []
    clock = {"now": 0.0}
    schedule = LiveStatusSchedule(
        read_activity=lambda: calls.append("activity") or {"active": False, "activities": []},
        read_engine=lambda: calls.append("engine") or {"active": False, "percent": 0},
        read_wifi=lambda: calls.append("wifi") or {"ssid": "net"},
        read_bluetooth=lambda: calls.append("bluetooth") or {"enabled": True},
        read_updates=lambda: calls.append("updates") or {"auto_update": False},
        read_capability=lambda: _Capability(False, False),
        clock=lambda: clock["now"],
        activity_interval_seconds=4,
        radio_interval_seconds=10,
        updates_interval_seconds=10,
    )

    snapshot = schedule.snapshot()

    assert calls == ["activity", "engine", "updates"]
    assert snapshot["wifi"] is None
    assert snapshot["bluetooth"] is None
    assert snapshot["type"] == "live_status"
    assert snapshot["activity"] == {"active": False, "activities": []}


def test_schedule_rereads_only_sources_whose_interval_elapsed():
    """Each source keeps its own cadence inside the one snapshot.

    Why: activity needs to move every few seconds during an install, while
    Wi-Fi and updates shell out and must not be read on that same tick.
    Regression: the second snapshot's calls include "wifi" or "updates"
    before their 10s interval, or omit "activity" after 4s.
    """
    calls = []
    clock = {"now": 0.0}

    def reader(name, payload):
        def read():
            calls.append(name)
            return payload
        return read

    schedule = LiveStatusSchedule(
        read_activity=reader("activity", {"active": False, "activities": []}),
        read_engine=reader("engine", {"active": False, "percent": 0}),
        read_wifi=reader("wifi", {"ssid": "net"}),
        read_bluetooth=reader("bluetooth", {"enabled": True}),
        read_updates=reader("updates", {"auto_update": True}),
        read_capability=lambda: _Capability(True, True),
        clock=lambda: clock["now"],
        activity_interval_seconds=4,
        radio_interval_seconds=10,
        updates_interval_seconds=10,
    )

    schedule.snapshot()
    calls.clear()
    clock["now"] = 4.0
    second = schedule.snapshot()

    assert calls == ["activity", "engine"]
    assert second["wifi"] == {"ssid": "net"}
    assert second["updates"] == {"auto_update": True}

    calls.clear()
    clock["now"] = 10.0
    schedule.snapshot()
    assert calls == ["activity", "engine", "wifi", "bluetooth", "updates"]


def test_engine_status_is_reread_every_two_seconds():
    """The install dict is sampled on its own 2s cadence.

    Why: percent is computed from the clock at read time, so the Settings
    build bar only moves when this source is read. Activity stays on 4s and
    must not be pulled forward to match. Regression: the calls list at t=2
    omits "engine" or includes "activity".
    """
    calls = []
    clock = {"now": 0.0}
    schedule = LiveStatusSchedule(
        read_activity=lambda: calls.append("activity") or {"active": False, "activities": []},
        read_engine=lambda: calls.append("engine") or {"active": True, "percent": 10},
        read_wifi=lambda: calls.append("wifi") or {"ssid": "net"},
        read_bluetooth=lambda: {"enabled": True},
        read_updates=lambda: calls.append("updates") or {"auto_update": False},
        read_capability=lambda: _Capability(False, False),
        clock=lambda: clock["now"],
    )

    first = schedule.snapshot()
    calls.clear()
    clock["now"] = 2.0
    second = schedule.snapshot()

    assert calls == ["engine"]
    assert first["engine"] == {"active": True, "percent": 10}
    assert second["engine"] == {"active": True, "percent": 10}
    assert second["activity"] == {"active": False, "activities": []}


def test_pump_publishes_only_when_the_snapshot_changes():
    """An identical sample must not be written to the event stream.

    Why: the pump ticks faster than the sources change. Publishing every tick
    would put the same payload on /events once a second while the board is
    idle. Regression: published grows on the second run_once.
    """
    snapshots = [
        {"type": "live_status", "n": 1},
        {"type": "live_status", "n": 1},
        {"type": "live_status", "n": 2},
    ]
    published = []
    pump = LiveStatusPump(snapshot=lambda: snapshots.pop(0), publish=published.append)

    assert pump.run_once() is True
    assert pump.run_once() is False
    assert pump.run_once() is True
    assert [item["n"] for item in published] == [1, 2]
    assert pump.latest()["n"] == 2
