"""About menu helpers."""

from typing import Callable, Optional, List

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.board.system_info import (
    SystemInfo,
    format_gibibytes,
    format_percent,
    format_temperature_celsius,
    format_uptime,
)
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.services.update_service import get_update_service


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


def build_about_entries(
    get_installed_version: Callable[[], str],
    system_info: Optional[SystemInfo] = None,
) -> List[IconMenuEntry]:
    """Build about menu entries: version, update status, and system telemetry.

    Args:
        get_installed_version: Function returning installed version string
        system_info: Current system telemetry, or ``None`` to omit the telemetry
            rows (e.g. when it could not be read). Passed in (rather than read
            here) so this builder stays pure and the hardware/OS read happens at
            the call site.

    Returns:
        List of menu entries
    """
    version = get_installed_version()
    version_label = f"Version\n{version}" if version else "Version\nUnknown"
    
    update_service = get_update_service()
    status = update_service.get_status_dict()
    
    # Determine update status label
    if status["has_pending_update"]:
        update_label = "Updates\nReady!"
        update_icon = "update"
    elif status["available_version"]:
        update_label = f"Updates\nv{status['available_version']}"
        update_icon = "update"
    elif status["auto_update"]:
        update_label = "Updates\nAuto"
        update_icon = "checkbox_checked"
    else:
        update_label = "Updates\nManual"
        update_icon = "checkbox_empty"

    return [
        IconMenuEntry(
            key="Version",
            label=version_label,
            icon_name="info",
            enabled=True,
            selectable=False,
        ),
        IconMenuEntry(
            key="Updates",
            label=update_label,
            icon_name=update_icon,
            enabled=True,
        ),
        *build_system_info_entries(system_info),
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


def handle_about_menu(
    ctx,
    menu_manager,
    board,
    log,
    get_installed_version: Callable[[], str],
    handle_update_menu: Callable,
    show_menu: Callable,
    find_entry_index: Callable,
    get_system_info: Optional[Callable[[], Optional[SystemInfo]]] = None,
) -> Optional[MenuSelection]:
    """Handle About menu - show version info, system telemetry, and updates.
    
    Args:
        ctx: Menu context
        menu_manager: Menu manager instance
        board: Board instance
        log: Logger instance
        get_installed_version: Function returning installed version
        handle_update_menu: Function to handle update submenu
        show_menu: Function to display menu
        find_entry_index: Function to find entry index
        get_system_info: Optional telemetry provider; defaults to a safe psutil
            reader that returns ``None`` when telemetry cannot be read. Re-read on
            each menu rebuild so the displayed values stay current.
        
    Returns:
        MenuSelection if breaking out, None otherwise
    """
    read_telemetry = get_system_info or (lambda: read_system_info_safely(log))

    def build_entries():
        return build_about_entries(get_installed_version, system_info=read_telemetry())

    def handle_selection(result: MenuSelection):
        if result.key == "Version":
            # Version is display-only, not selectable
            return None
        elif result.key == "Updates":
            ctx.enter_menu("Updates", 0)
            sub_result = handle_update_menu(
                show_menu=show_menu,
                find_entry_index=find_entry_index,
                board=board,
                log=log,
                initial_index=ctx.current_index(),
            )
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=ctx.current_index())
