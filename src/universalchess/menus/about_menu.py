"""About menu telemetry helpers.

The About screen itself is data-driven (the ``about`` catalog container rendered
through the menu engine); this module holds only the pure, well-tested telemetry
formatting it reuses. The board's ``system_telemetry`` provider returns
:func:`build_system_info_entries` directly as engine rows, and
:func:`read_system_info_safely` degrades a failed sensor read to "no telemetry
rows" so the menu never crashes.
"""

from typing import Optional, List

from universalchess.menus.engine import MenuRow
from universalchess.i18n import t
from universalchess.board.system_info import (
    SystemInfo,
    format_gibibytes,
    format_percent,
    format_temperature_celsius,
    format_uptime,
)


def build_system_info_entries(system_info: Optional[SystemInfo]) -> List[MenuRow]:
    """Build the read-only telemetry rows shown beneath Version.

    Returns engine ``MenuRow``s (the menu engine's row type, consumed directly by
    the ``system_telemetry`` provider). Returns an empty list when ``system_info``
    is ``None`` (telemetry could not be read, e.g. psutil missing on a dev host),
    so the About screen still shows Version rather than failing. All rows are
    non-selectable: they are informational, mirroring the Version row.
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
        MenuRow(key="SysCpu", label=t("about.cpu", value=f"{cpu_value} / {temp_value}"), icon="engine", selectable=False),
        MenuRow(key="SysMemory", label=t("about.memory", value=memory_value), icon="system", selectable=False),
        MenuRow(key="SysDisk", label=t("about.storage", value=disk_value), icon="info", selectable=False),
        MenuRow(key="SysUptime", label=t("about.uptime", value=format_uptime(system_info.uptime_seconds)), icon="timer", selectable=False),
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
