"""Menu helper exports."""

from .main_menu import create_main_menu_entries
from .settings_menu import create_settings_entries
from .settings_menu import _get_player_type_label
from .system_menu import create_system_entries, handle_system_menu
from .positions_menu import handle_positions_menu
from .engine_menu import (
    handle_engine_selection,
    handle_elo_selection,
)
from .chromecast_menu import handle_chromecast_menu
from .connectivity_menu import create_connectivity_entries, handle_connectivity_menu
from .inactivity_menu import handle_inactivity_timeout
from .wifi_menu import handle_wifi_settings_menu
from .wifi_menu import handle_wifi_scan_menu
from .bluetooth_menu import (
    handle_bluetooth_menu,
    handle_keyboard_pairing_menu,
    handle_paired_devices_menu,
)
from .accounts_menu import handle_accounts_menu, mask_token
from .about_menu import handle_about_menu
from .engine_manager_menu import (
    handle_engine_manager_menu,
    handle_engine_detail_menu,
    show_engine_install_progress,
)
from .reset_menu import handle_reset_settings
from .analysis_menu import handle_analysis_mode_menu, handle_analysis_engine_selection
from .update_menu import (
    handle_update_menu,
    handle_local_deb_install,
    find_local_deb_files,
)
from universalchess.services.lichess_service import (
    get_lichess_client,
    build_lichess_menu_entries,
    show_lichess_error,
    show_lichess_mode_menu,
    build_new_game_entries,
    show_time_control_menu,
    ensure_token,
    start_lichess_game_service,
    LichessStartResult,
)

__all__ = [
    "create_main_menu_entries",
    "create_settings_entries",
    "create_system_entries",
    "handle_system_menu",
    "handle_positions_menu",
    "handle_chromecast_menu",
    "create_connectivity_entries",
    "handle_connectivity_menu",
    "handle_inactivity_timeout",
    "handle_wifi_settings_menu",
    "handle_wifi_scan_menu",
    "handle_bluetooth_menu",
    "handle_keyboard_pairing_menu",
    "handle_paired_devices_menu",
    "handle_accounts_menu",
    "mask_token",
    "handle_update_menu",
    "handle_local_deb_install",
    "find_local_deb_files",
    "get_lichess_client",
    "build_lichess_menu_entries",
    "show_lichess_error",
    "show_lichess_mode_menu",
    "build_new_game_entries",
    "show_time_control_menu",
    "ensure_token",
    "start_lichess_game_service",
    "LichessStartResult",
    "_get_player_type_label",
    "handle_engine_selection",
    "handle_elo_selection",
    "handle_about_menu",
    "handle_engine_manager_menu",
    "handle_engine_detail_menu",
    "show_engine_install_progress",
    "handle_reset_settings",
    "handle_analysis_mode_menu",
    "handle_analysis_engine_selection",
]

