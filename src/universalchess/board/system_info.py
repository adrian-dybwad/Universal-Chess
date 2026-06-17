"""System telemetry: CPU temperature, load, memory, disk, and uptime.

Replaces the old ``board.temp()`` helper, which shelled out to ``vcgencmd`` and
only returned the CPU temperature. This module reads the same (and more) data
through :mod:`psutil`, returning a single :class:`SystemInfo` object so the
e-paper About screen and the web Settings "System" card render identical numbers
from one source of truth.

Design:
  All OS/psutil access is isolated behind :class:`SystemInfoSource`, a bundle of
  injectable readers. Assembly (:func:`collect_system_info`) and the display
  formatters are therefore pure functions, unit-testable without a Raspberry Pi
  or a psutil-backed sensor. :func:`default_source` adapts psutil into the
  source; :func:`get_system_info` is the convenience entry point for callers.

Assumptions / pitfalls guarded here:
  - Absent sensors (no thermal zone on a dev host, no per-platform load average)
    are represented as ``None``, never a fabricated ``0`` -- a fake-but-plausible
    value would mislead the UI (e.g. "0 C" implies a frozen CPU).
  - ``psutil.cpu_percent`` needs a sampling window to return a meaningful value;
    the default source uses a short blocking interval so a single call yields a
    real reading (see :data:`_CPU_SAMPLE_INTERVAL_SECONDS`).
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional


# psutil.cpu_percent(interval=None) returns 0.0 until it has two samples to
# compare. A single About-screen / API call must produce a real number, so the
# default source blocks briefly to take an immediate sample. Kept small because
# it is on the e-paper render path.
_CPU_SAMPLE_INTERVAL_SECONDS = 0.2

_EM_DASH = "\u2014"
_DEGREE = "\u00b0"
_BYTES_PER_GIB = 1024 ** 3


@dataclass(frozen=True)
class MemorySnapshot:
    """RAM usage at a point in time. ``percent`` is 0-100."""

    used_bytes: int
    total_bytes: int
    percent: float


@dataclass(frozen=True)
class DiskSnapshot:
    """Filesystem usage for one mount point. ``percent`` is 0-100."""

    used_bytes: int
    total_bytes: int
    percent: float


@dataclass(frozen=True)
class SystemInfo:
    """Aggregated system telemetry shared by the e-paper and web surfaces.

    ``cpu_temperature_celsius`` and ``load_average_1m`` are ``None`` when the
    platform does not expose them (e.g. a development host).
    """

    hostname: str
    cpu_percent: float
    cpu_temperature_celsius: Optional[float]
    memory: MemorySnapshot
    disk: DiskSnapshot
    uptime_seconds: float
    load_average_1m: Optional[float]

    def to_dict(self) -> dict:
        """Flat, JSON-serializable contract consumed by the web client.

        Keys are read by name in the React Settings page; nested snapshots are
        flattened (``memory_used_bytes`` etc.) and ``None`` sensors serialize to
        JSON ``null`` so the UI can render a dash instead of a bogus number.
        """
        return {
            "hostname": self.hostname,
            "cpu_percent": self.cpu_percent,
            "cpu_temperature_celsius": self.cpu_temperature_celsius,
            "memory_used_bytes": self.memory.used_bytes,
            "memory_total_bytes": self.memory.total_bytes,
            "memory_percent": self.memory.percent,
            "disk_used_bytes": self.disk.used_bytes,
            "disk_total_bytes": self.disk.total_bytes,
            "disk_percent": self.disk.percent,
            "uptime_seconds": self.uptime_seconds,
            "load_average_1m": self.load_average_1m,
        }


@dataclass(frozen=True)
class SystemInfoSource:
    """Injectable boundary between assembly and OS/psutil side effects.

    Each reader is a zero-argument callable returning one piece of telemetry.
    Tests pass fakes; production uses :func:`default_source`.
    """

    hostname: Callable[[], str]
    cpu_percent: Callable[[], float]
    cpu_temperature_celsius: Callable[[], Optional[float]]
    memory: Callable[[], MemorySnapshot]
    disk: Callable[[], DiskSnapshot]
    uptime_seconds: Callable[[], float]
    load_average_1m: Callable[[], Optional[float]]


def collect_system_info(source: SystemInfoSource) -> SystemInfo:
    """Assemble a :class:`SystemInfo` by invoking each reader on ``source``.

    Pure with respect to its argument: all side effects live in the injected
    callables, so this is fully deterministic under a fake source.
    """
    return SystemInfo(
        hostname=source.hostname(),
        cpu_percent=source.cpu_percent(),
        cpu_temperature_celsius=source.cpu_temperature_celsius(),
        memory=source.memory(),
        disk=source.disk(),
        uptime_seconds=source.uptime_seconds(),
        load_average_1m=source.load_average_1m(),
    )


def default_source(root_path: str = "/") -> SystemInfoSource:
    """Build the production source backed by psutil and the standard library.

    Imports psutil lazily so this module can be imported (e.g. for the pure
    formatters or for type references) on hosts without psutil installed.
    ``root_path`` is the filesystem whose usage is reported.
    """
    import psutil

    def read_cpu_temperature_celsius() -> Optional[float]:
        # psutil.sensors_temperatures() is missing on some platforms (macOS,
        # Windows) and returns an empty/uninteresting dict on others. Treat any
        # of those as "no sensor" -> None rather than guessing a value.
        read_sensors = getattr(psutil, "sensors_temperatures", None)
        if read_sensors is None:
            return None
        sensors = read_sensors()
        if not sensors:
            return None
        # Prefer the Raspberry Pi SoC sensor; otherwise the first reading that
        # reports a current temperature.
        preferred = sensors.get("cpu_thermal") or sensors.get("coretemp")
        candidates = preferred if preferred else _flatten(sensors)
        for reading in candidates:
            current = getattr(reading, "current", None)
            if current is not None:
                return float(current)
        return None

    def read_load_average_1m() -> Optional[float]:
        # os.getloadavg() is unavailable on some platforms; absence -> None.
        getloadavg = getattr(os, "getloadavg", None)
        if getloadavg is None:
            return None
        return float(getloadavg()[0])

    def read_memory() -> MemorySnapshot:
        vm = psutil.virtual_memory()
        return MemorySnapshot(used_bytes=vm.used, total_bytes=vm.total, percent=vm.percent)

    def read_disk() -> DiskSnapshot:
        du = psutil.disk_usage(root_path)
        return DiskSnapshot(used_bytes=du.used, total_bytes=du.total, percent=du.percent)

    def read_uptime_seconds() -> float:
        # Clamp to >= 0 so a clock skew between boot_time and now never yields a
        # nonsensical negative uptime.
        return max(0.0, time.time() - psutil.boot_time())

    return SystemInfoSource(
        hostname=socket.gethostname,
        cpu_percent=lambda: float(psutil.cpu_percent(interval=_CPU_SAMPLE_INTERVAL_SECONDS)),
        cpu_temperature_celsius=read_cpu_temperature_celsius,
        memory=read_memory,
        disk=read_disk,
        uptime_seconds=read_uptime_seconds,
        load_average_1m=read_load_average_1m,
    )


def get_system_info(root_path: str = "/") -> SystemInfo:
    """Collect current system telemetry using the production psutil source."""
    return collect_system_info(default_source(root_path))


def _flatten(sensors: dict) -> list:
    """Flatten psutil's ``{label: [readings]}`` sensor map into one list."""
    flattened: list = []
    for readings in sensors.values():
        flattened.extend(readings)
    return flattened


# ---------------------------------------------------------------------------
# Display formatters (pure). Shared so the e-paper rows and any server-side
# rendering produce identical strings. The web client may also format from the
# raw to_dict() values; these are the canonical short forms.
# ---------------------------------------------------------------------------


def format_temperature_celsius(celsius: Optional[float]) -> str:
    """Whole-degree temperature (e.g. "48 C"), or an em dash when unknown."""
    if celsius is None:
        return _EM_DASH
    return f"{round(celsius)}{_DEGREE}C"


def format_percent(value: Optional[float]) -> str:
    """Whole-number percentage (e.g. "38%"), or an em dash when unknown."""
    if value is None:
        return _EM_DASH
    return f"{round(value)}%"


def format_uptime(seconds: float) -> str:
    """Short uptime string.

    Under a day it reads ``"{h}h {m}m"`` (or ``"{m}m"`` when under an hour);
    a day or more switches to ``"{d}d {h}h"`` to stay short on the e-paper row.
    Minutes/hours are floored (never rounded up) so a value never reports more
    elapsed time than has actually passed.
    """
    total_minutes = int(seconds // 60)
    minutes = total_minutes % 60
    total_hours = total_minutes // 60
    hours = total_hours % 24
    days = total_hours // 24
    if days >= 1:
        return f"{days}d {hours}h"
    if total_hours >= 1:
        return f"{total_hours}h {minutes}m"
    return f"{minutes}m"


def format_gibibytes(num_bytes: int) -> str:
    """Bytes as GiB to one decimal (e.g. "3.5 GiB")."""
    return f"{num_bytes / _BYTES_PER_GIB:.1f} GiB"
