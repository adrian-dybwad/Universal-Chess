"""About menu telemetry helpers.

The About screen itself is data-driven (the ``about`` catalog container rendered
through the menu engine); this module holds only the pure, well-tested telemetry
formatting it reuses. The board's ``system_telemetry`` provider turns
:func:`build_system_info_entries` into engine rows, and :func:`read_system_info_safely`
degrades a failed sensor read to "no telemetry rows" so the menu never crashes.
"""

from typing import Optional, List

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.board.system_info import (
    SystemInfo,
    format_gibibytes,
    format_percent,
    format_temperature_celsius,
    format_uptime,
)


def build_system_info_entries(system_info: Optional[SystemInfo]) -> List[IconMenuEntry]:
    """Build the read-only telemetry rows shown beneath Version/Updates.

    Returns an empty list when ``system_info`` is ``None`` (telemetry could not
    be read, e.g. psutil missing on a dev host), so the About screen still shows
    Version and Updates rather than failing. All rows are non-selectable: they
    are informational, mirroring the existing Version row.
    """
    if system_info is None:
        return []

    cpu_value = format_percent(system_info.cpu_percent)
    temp_value = format_temperature_celsius(system_info.cpu_temperature_celsius)
    memory_value = (
        f"{format_percent(system_info.memory.percent)} "
        f"({format_gibibytes(system_info.memory.used_bytes)})"
    )
    disk_value = (
        f"{format_percent(system_info.disk.percent)} "
        f"({format_gibibytes(system_info.disk.used_bytes)})"
    )

    return [
        IconMenuEntry(
            key="SysCpu",
            label=f"CPU\n{cpu_value} / {temp_value}",
            icon_name="engine",
            enabled=True,
            selectable=False,
        ),
        IconMenuEntry(
            key="SysMemory",
            label=f"Memory\n{memory_value}",
            icon_name="system",
            enabled=True,
            selectable=False,
        ),
        IconMenuEntry(
            key="SysDisk",
            label=f"Storage\n{disk_value}",
            icon_name="info",
            enabled=True,
            selectable=False,
        ),
        IconMenuEntry(
            key="SysUptime",
            label=f"Uptime\n{format_uptime(system_info.uptime_seconds)}",
            icon_name="timer",
            enabled=True,
            selectable=False,
        ),
    ]


def read_system_info_safely(log=None) -> Optional[SystemInfo]:
    """Collect telemetry, returning ``None`` on any failure.

    The About screen must never crash because a sensor read failed or psutil is
    unavailable, so this swallows errors and degrades to "no telemetry rows".
    Logs at debug so the cause is still discoverable without spamming the menu.
    """
    try:
        from universalchess.board.system_info import get_system_info

        return get_system_info()
    except Exception as e:
        if log is not None:
            log.debug(f"System telemetry unavailable: {e}")
        return None
