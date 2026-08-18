"""Bring the board up, in the one order that works.

Each step here exists because the step after it depends on it having happened:

1. **The previous-shutdown audit** reads the OS logs before the controller is
   initialised, because initialising the controller is what eventually cuts the
   power whose aftermath the audit is reading.
2. **Resources** are loaded before any widget is constructed: widgets read their
   sprites and fonts from module-level state that the loader injects, so a
   widget built first draws nothing.
3. **The panel** comes up next and gets a splash immediately, so the user sees
   the board wake during the seconds of imports and handshaking that follow.
4. **The board-init callback** is registered before the board module is
   imported, so retries during the controller handshake can report progress on
   that splash.
5. **The controller** is initialised last, and only then does the application
   module load.

Nothing here runs at import time. A test, a widget preview or the web process
can import any application module without a panel, a controller, or a Pi.
"""

from dataclasses import dataclass
from typing import Optional

from universalchess.app import display_boot, startup_splash
from universalchess.board.logging import log
from universalchess.epaper import Manager, SplashScreen


@dataclass(frozen=True)
class BootResult:
    """What the bring-up produced. Both are None on a board with no panel."""

    display_manager: Optional[Manager]
    splash: Optional[SplashScreen]


def initialize_resources() -> None:
    """Load resources and inject them into the widget modules.

    Must run before any widget is created: widgets read their sprites and logos
    from module-level state set here, and one built earlier would draw blanks
    for the rest of the session.
    """
    try:
        from universalchess.resources import ResourceLoader
        from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR
        from universalchess.epaper import chess_board as chess_board_module
        from universalchess.epaper import splash_screen as splash_screen_module
        from universalchess.epaper import icon_button as icon_button_module

        # Create resource loader using paths (supports both installed and dev environments)
        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)

        # Register as the app-wide singleton so the display menu's sprite selector
        # and the DisplayManager's hot-reload reuse this loader (and its caches).
        from universalchess import resources as resources_module
        resources_module.set_resource_loader(loader)

        # Load and set the chess sprite sheet selected in settings (falls back to
        # default). Read via Settings.read rather than the application module's
        # settings helpers: those live in a module this one must not import.
        from universalchess.board.settings import Settings
        selected_sheet = Settings.read('game', 'chess_sprites', loader.DEFAULT_SPRITE_SHEET)
        sprites = loader.get_chess_sprites(selected_sheet)
        if sprites is None and selected_sheet != loader.DEFAULT_SPRITE_SHEET:
            log.warning(f"[Startup] Chess sprite sheet '{selected_sheet}' not found, using default")
            sprites = loader.get_chess_sprites(loader.DEFAULT_SPRITE_SHEET)
        if sprites:
            chess_board_module.set_chess_sprites(sprites)

        # Square head logo for buttons (menu buttons at 80, icon buttons at 36/24/20).
        for size in [80, 36, 24, 20]:
            logo, mask = loader.get_knight_logo(size)
            if logo and mask:
                icon_button_module.set_knight_logo(size, logo, mask)

        # Full piece (portrait) for the splash screen, sized to its logo band.
        splash_logo, splash_mask = loader.get_knight_logo_full(
            splash_screen_module.SplashScreen.LOGO_HEIGHT)
        if splash_logo and splash_mask:
            splash_screen_module.set_knight_logo(splash_logo, splash_mask)

        log.info("[Startup] Resources loaded and injected into widget modules")
    except Exception as e:
        log.error(f"[Startup] Failed to initialize resources: {e}", exc_info=True)


def boot() -> BootResult:
    """Run the bring-up sequence and return what it produced.

    Ends with the controller ready and a splash on the panel, so the caller can
    import the application module (whose own slow imports report progress on
    that splash) and run it.
    """
    from universalchess.board import boot_report
    boot_report.audit_previous_shutdown()

    initialize_resources()

    display_manager, splash = display_boot.init_display()
    startup_splash.set_splash(splash)

    # Registered before the board module is imported so the controller handshake
    # (which retries, and can take seconds) can report progress on the splash.
    from universalchess.board import init_callback
    init_callback.set_callback(_report_board_init_status)

    from universalchess.board import board
    if display_manager is not None:
        board.display_manager = display_manager

    board.init_board()
    startup_splash.note("splash.loading")

    return BootResult(display_manager=display_manager, splash=splash)


def _report_board_init_status(message: str) -> None:
    """Show a board-initialization status message on the startup splash."""
    splash = startup_splash.current()
    if splash is not None:
        splash.set_message(message)
