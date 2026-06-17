#!/usr/bin/env python3
"""Tests for the system telemetry module.

Why these tests exist:
  ``board.system_info`` is the single source of the numbers shown on BOTH the
  e-paper About screen and the web Settings "System" card. If assembly,
  serialization, or display formatting drift, the two surfaces silently disagree
  or show garbage. These tests pin:
    - that ``collect_system_info`` copies every value from the injected source
      into the ``SystemInfo`` object unchanged (no field dropped/swapped),
    - that ``to_dict`` emits the exact JSON contract the web endpoint relies on,
    - that the human-readable formatters handle the edge cases that actually
      occur on a board (no temperature sensor, sub-minute / multi-day uptime,
      zero/partial bytes).

  The OS/psutil side effects are injected via ``SystemInfoSource`` so these are
  pure, deterministic unit tests that need neither a Raspberry Pi nor psutil.
"""

import pytest

from universalchess.board.system_info import (
    DiskSnapshot,
    MemorySnapshot,
    SystemInfo,
    SystemInfoSource,
    collect_system_info,
    format_gibibytes,
    format_percent,
    format_temperature_celsius,
    format_uptime,
)


GIB = 1024 ** 3


def _fake_source(
    *,
    hostname="dgt-test",
    cpu_percent=37.5,
    cpu_temperature_celsius=48.3,
    memory=None,
    disk=None,
    uptime_seconds=90061.0,
    load_average_1m=0.42,
):
    """Build a fully-deterministic source for assembly assertions.

    Every reader returns a fixed value so the test can assert the assembled
    object equals exactly those inputs.
    """
    memory = memory or MemorySnapshot(used_bytes=3 * GIB, total_bytes=8 * GIB, percent=37.5)
    disk = disk or DiskSnapshot(used_bytes=10 * GIB, total_bytes=32 * GIB, percent=31.25)
    return SystemInfoSource(
        hostname=lambda: hostname,
        cpu_percent=lambda: cpu_percent,
        cpu_temperature_celsius=lambda: cpu_temperature_celsius,
        memory=lambda: memory,
        disk=lambda: disk,
        uptime_seconds=lambda: uptime_seconds,
        load_average_1m=lambda: load_average_1m,
    )


class TestCollectSystemInfo:

    def test_copies_every_field_from_source(self):
        """Each reader's value must land in the matching SystemInfo field.

        Regression manifestation: if a field is dropped or wired to the wrong
        reader (e.g. disk percent assigned from memory), the asserted value for
        that field would differ from the source input.
        """
        memory = MemorySnapshot(used_bytes=3 * GIB, total_bytes=8 * GIB, percent=37.5)
        disk = DiskSnapshot(used_bytes=10 * GIB, total_bytes=32 * GIB, percent=31.25)
        source = _fake_source(memory=memory, disk=disk)

        info = collect_system_info(source)

        assert info == SystemInfo(
            hostname="dgt-test",
            cpu_percent=37.5,
            cpu_temperature_celsius=48.3,
            memory=memory,
            disk=disk,
            uptime_seconds=90061.0,
            load_average_1m=0.42,
        )

    def test_missing_sensors_propagate_as_none(self):
        """A dev host (or a board with no thermal sensor / no load avg) yields
        ``None`` for those readings, not a fabricated 0.

        Regression manifestation: substituting 0.0 for an absent CPU temperature
        would make the UI claim the board is at 0 °C; this asserts None survives.
        """
        source = _fake_source(cpu_temperature_celsius=None, load_average_1m=None)

        info = collect_system_info(source)

        assert info.cpu_temperature_celsius is None
        assert info.load_average_1m is None


class TestToDict:

    def test_emits_exact_json_contract(self):
        """``to_dict`` is the web endpoint's payload; its keys/values are a
        contract the React client reads by name.

        Regression manifestation: renaming a key or nesting it differently would
        break the web "System" card silently (undefined values); this asserts
        the full flat shape the client expects.
        """
        info = collect_system_info(_fake_source())

        assert info.to_dict() == {
            "hostname": "dgt-test",
            "cpu_percent": 37.5,
            "cpu_temperature_celsius": 48.3,
            "memory_used_bytes": 3 * GIB,
            "memory_total_bytes": 8 * GIB,
            "memory_percent": 37.5,
            "disk_used_bytes": 10 * GIB,
            "disk_total_bytes": 32 * GIB,
            "disk_percent": 31.25,
            "uptime_seconds": 90061.0,
            "load_average_1m": 0.42,
        }

    def test_none_sensors_serialize_as_null(self):
        """Absent sensors must serialize as JSON null (Python None), so the
        client can show a dash rather than a bogus number.

        Regression manifestation: if ``to_dict`` coerced None to 0, the JSON
        would carry 0 and the UI would display "0 °C" / "0.00 load".
        """
        info = collect_system_info(
            _fake_source(cpu_temperature_celsius=None, load_average_1m=None)
        )

        payload = info.to_dict()
        assert payload["cpu_temperature_celsius"] is None
        assert payload["load_average_1m"] is None


class TestFormatTemperatureCelsius:

    def test_rounds_to_whole_degrees(self):
        """The About screen is narrow; temperature is shown as whole degrees.

        Regression manifestation: dropping the rounding would print a long
        float ("48.3°C" -> "48.30000001°C") that overflows the e-paper row.
        """
        assert format_temperature_celsius(48.3) == "48\u00b0C"

    def test_none_renders_dash(self):
        """No sensor -> em dash, never "0°C".

        Regression manifestation: a missing sensor formatted as 0 would imply a
        frozen/cold CPU; this guards the explicit "unknown" rendering.
        """
        assert format_temperature_celsius(None) == "\u2014"


class TestFormatPercent:

    def test_rounds_to_whole_percent(self):
        """CPU/memory/disk percentages are shown as whole numbers.

        Regression manifestation: without rounding, "37.5" would render with a
        trailing decimal that does not fit the e-paper label.
        """
        assert format_percent(37.5) == "38%"

    def test_none_renders_dash(self):
        """A missing percentage renders as an em dash, not "0%"."""
        assert format_percent(None) == "\u2014"


class TestFormatUptime:

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0m"),          # just booted: floor to whole minutes
            (59, "0m"),         # sub-minute still reads 0m (no rounding up)
            (90, "1m"),         # 1.5 min floors to 1m
            (3600, "1h 0m"),    # exactly one hour shows the trailing 0m
            (3661, "1h 1m"),    # hours + minutes
            (90061, "1d 1h"),   # >= 1 day switches to day/hour granularity
            (172800, "2d 0h"),  # exactly two days keeps the trailing 0h
        ],
    )
    def test_uptime_buckets(self, seconds, expected):
        """Uptime formatting changes granularity at the day boundary so the
        string stays short on the e-paper row while remaining informative.

        Regression manifestation: an off-by-one in the divmod math (e.g. using
        round instead of floor, or wrong unit thresholds) would shift a value
        into the wrong bucket -- caught by the boundary cases above.
        """
        assert format_uptime(seconds) == expected


class TestFormatGibibytes:

    @pytest.mark.parametrize(
        "num_bytes,expected",
        [
            (0, "0.0 GiB"),
            (GIB, "1.0 GiB"),
            (3 * GIB + GIB // 2, "3.5 GiB"),
        ],
    )
    def test_bytes_to_gib(self, num_bytes, expected):
        """Memory/disk are reported in GiB to one decimal.

        Regression manifestation: dividing by 1000**3 (GB) instead of 1024**3
        (GiB), or the wrong precision, would change these exact strings.
        """
        assert format_gibibytes(num_bytes) == expected


class TestDefaultSourceIntegration:
    """Smoke test of the real psutil-backed source (skipped if psutil absent).

    Why it exists: the injected unit tests above can pass while the real reader
    is broken (wrong psutil attribute, exception on this platform). This runs the
    actual collection once and asserts the values are structurally sane.

    Regression manifestation: a typo'd psutil call or a non-optional sensor
    access would raise here, or produce out-of-range values (e.g. negative
    uptime, percent > 100).
    """

    def test_real_collection_is_sane(self):
        pytest.importorskip("psutil")
        from universalchess.board.system_info import get_system_info

        info = get_system_info()

        assert isinstance(info.hostname, str) and info.hostname
        assert 0.0 <= info.cpu_percent <= 100.0
        assert info.cpu_temperature_celsius is None or info.cpu_temperature_celsius > 0
        assert info.memory.total_bytes > 0
        assert 0.0 <= info.memory.percent <= 100.0
        assert info.disk.total_bytes > 0
        assert info.uptime_seconds >= 0
        assert info.load_average_1m is None or info.load_average_1m >= 0
