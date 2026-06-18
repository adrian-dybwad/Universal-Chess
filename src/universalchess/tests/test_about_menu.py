#!/usr/bin/env python3
"""Tests for the About telemetry formatters.

Why these tests exist:
  The About screen is now data-driven (the ``about`` catalog container rendered
  through the engine); this module keeps only the pure telemetry helpers it
  reuses. These tests pin (a) that telemetry rows render in a stable order with
  the expected non-selectable info rows and labels formatted by the shared
  system_info formatters, and (b) that missing telemetry degrades to no rows
  rather than raising -- the on-board menu must never crash because a sensor read
  failed. The Version/Updates composition now lives in the catalog container and
  is covered by test_about_menu_engine.
"""

from universalchess.board.system_info import (
    DiskSnapshot,
    MemorySnapshot,
    SystemInfo,
)
from universalchess.menus import about_menu


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


class TestBuildSystemInfoEntries:

    def test_none_yields_no_rows(self):
        """Missing telemetry must produce zero rows so the About screen can fall
        back to Version/Updates only.

        Regression manifestation: returning a placeholder row (or raising) when
        telemetry is None would either show garbage or crash the menu on a host
        without psutil/sensors.
        """
        assert about_menu.build_system_info_entries(None) == []

    def test_rows_have_expected_keys_labels_icons(self):
        """The four telemetry rows must render in a fixed order with labels
        formatted by the shared system_info formatters, all non-selectable.

        Regression manifestation: a wrong divisor/format or a selectable=True row
        would change a label string or let the user "click" an info row; the full
        assertions below catch any of those.
        """
        rows = about_menu.build_system_info_entries(_SAMPLE_INFO)

        assert [r.key for r in rows] == ["SysCpu", "SysMemory", "SysDisk", "SysUptime"]
        assert [r.icon_name for r in rows] == ["engine", "system", "info", "timer"]
        assert all(r.selectable is False for r in rows)
        assert [r.label for r in rows] == [
            "CPU\n38% / 48\u00b0C",
            "Memory\n38% (3.0 GiB)",
            "Storage\n31% (10.0 GiB)",
            "Uptime\n1d 1h",
        ]


class TestReadSystemInfoSafely:

    def test_returns_none_when_collection_raises(self, monkeypatch):
        """A failing telemetry read must degrade to None, not propagate.

        Regression manifestation: if the helper let the exception escape, opening
        the About menu on a board with a transient sensor error would crash the
        menu loop instead of simply hiding the telemetry rows.
        """
        import universalchess.board.system_info as system_info

        def _boom(*a, **k):
            raise RuntimeError("sensor read failed")

        monkeypatch.setattr(system_info, "get_system_info", _boom)
        assert about_menu.read_system_info_safely(log=None) is None
