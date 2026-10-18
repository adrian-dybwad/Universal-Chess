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

import pytest

from universalchess.web.live_status import LiveStatusPump, LiveStatusSchedule, build_id_from_html


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
        read_syzygy=lambda: calls.append("syzygy") or {"downloading": False},
        read_engine_defaults=lambda: calls.append("engine_defaults") or {"hash": 16},
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

    assert calls == ["activity", "engine", "syzygy", "engine_defaults", "updates"]
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
        read_syzygy=reader("syzygy", {"downloading": False}),
        read_engine_defaults=reader("engine_defaults", {"hash": 16}),
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
    assert calls == ["activity", "engine", "engine_defaults", "wifi", "bluetooth", "updates"]


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
        read_syzygy=lambda: calls.append("syzygy") or {"downloading": False},
        read_engine_defaults=lambda: calls.append("engine_defaults") or {"hash": 16},
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


def test_syzygy_directory_walk_runs_every_second_only_while_downloading():
    """The table directory is listed every second only during a download.

    Why: status() stats every table file. The progress bar needs that while
    bytes are landing, and an idle board must not do it on the one-second
    engine cadence. Regression: calls at t=1 include "syzygy" when
    downloading is false, or omit it when downloading is true.
    """
    calls = []
    clock = {"now": 0.0}
    downloading = {"value": False}

    def read_syzygy():
        calls.append("syzygy")
        return {"downloading": downloading["value"], "percent": 0}

    schedule = LiveStatusSchedule(
        read_activity=lambda: {"active": False, "activities": []},
        read_engine=lambda: calls.append("engine") or {"active": False},
        read_syzygy=read_syzygy,
        read_engine_defaults=lambda: {"hash": 16},
        read_wifi=lambda: {"ssid": "net"},
        read_bluetooth=lambda: {"enabled": True},
        read_updates=lambda: {"auto_update": False},
        read_capability=lambda: _Capability(False, False),
        clock=lambda: clock["now"],
    )

    schedule.snapshot()
    calls.clear()
    clock["now"] = 1.0
    schedule.snapshot()
    assert "syzygy" not in calls

    downloading["value"] = True
    clock["now"] = 30.0
    schedule.snapshot()
    calls.clear()
    clock["now"] = 31.0
    schedule.snapshot()
    assert calls == ["syzygy"]


def test_syzygy_reread_as_soon_as_a_download_starts(monkeypatch):
    """A download start is not stuck behind the idle directory interval.

    Why: an engine install republishes the whole snapshot every two seconds.
    If the cached Syzygy slice still says idle, those events carry a frozen
    tablebase bar until the 30s walk. The in-memory download flag must force
    a fresh read on the next tick. Regression: calls at t=1 omit "syzygy"
    after the flag flips.
    """
    calls = []
    clock = {"now": 0.0}
    active = {"value": False}

    def read_syzygy():
        calls.append("syzygy")
        return {"downloading": active["value"], "percent": 10 if active["value"] else 0}

    monkeypatch.setattr(
        "universalchess.services.syzygy.download_active",
        lambda: active["value"],
    )
    schedule = LiveStatusSchedule(
        read_activity=lambda: {"active": False, "activities": []},
        read_engine=lambda: {"active": True, "percent": 1},
        read_syzygy=read_syzygy,
        read_engine_defaults=lambda: {"hash": 16},
        read_wifi=lambda: {"ssid": "net"},
        read_bluetooth=lambda: {"enabled": True},
        read_updates=lambda: {"auto_update": False},
        read_capability=lambda: _Capability(False, False),
        clock=lambda: clock["now"],
    )

    schedule.snapshot()
    calls.clear()
    active["value"] = True
    clock["now"] = 1.0
    second = schedule.snapshot()

    assert calls == ["syzygy"]
    assert second["syzygy"] == {"downloading": True, "percent": 10}


def test_web_build_rides_the_update_cadence():
    """The deployed build id is sampled with update status, not every tick.

    Why: the build id on /events is how an open tab learns a new frontend was
    published, with or without a service worker. Reading index.html on the
    activity tick would read the file every few seconds.
    Regression: the calls list at t=4 includes "web_build", or the t=10
    snapshot omits the new id.
    """
    calls = []
    clock = {"now": 0.0}
    deployed = {"id": "1111aaaa2222bbbb"}
    schedule = LiveStatusSchedule(
        read_activity=lambda: calls.append("activity") or {"active": False, "activities": []},
        read_engine=lambda: {"active": False, "percent": 0},
        read_syzygy=lambda: {"downloading": False},
        read_engine_defaults=lambda: {"hash": 16},
        read_wifi=lambda: {"ssid": "net"},
        read_bluetooth=lambda: {"enabled": True},
        read_updates=lambda: {"auto_update": False},
        read_web_build=lambda: calls.append("web_build") or deployed["id"],
        read_capability=lambda: _Capability(False, False),
        clock=lambda: clock["now"],
        activity_interval_seconds=4,
        updates_interval_seconds=10,
    )

    first = schedule.snapshot()
    assert first["web_build"] == "1111aaaa2222bbbb"
    calls.clear()
    clock["now"] = 4.0
    schedule.snapshot()
    assert "web_build" not in calls

    deployed["id"] = "3333cccc4444dddd"
    calls.clear()
    clock["now"] = 10.0
    second = schedule.snapshot()
    assert "web_build" in calls
    assert second["web_build"] == "3333cccc4444dddd"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<meta name="uc-build" content="1111aaaa2222bbbb" />', "1111aaaa2222bbbb"),
        ('<meta content="1111aaaa2222bbbb" name="uc-build">', "1111aaaa2222bbbb"),
        ('<meta name="uc-build" content="__BUILD_ID__" />', None),
        ('<meta name="theme-color" content="#aa44aa" />', None),
        ("", None),
    ],
)
def test_build_id_from_html(html, expected):
    """The build id is the uc-build meta tag, and only once it is stamped.

    Why: this is the id an open tab compares with its own. Attribute order is
    not fixed, and an unstamped placeholder is not a build.
    Regression: a stamped tag in either order returns None (tabs never see a
    new build), or the placeholder is returned (a dev deploy looks like a
    build that never matches).
    """
    assert build_id_from_html(html) == expected


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
