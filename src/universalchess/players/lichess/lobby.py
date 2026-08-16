"""Lichess service wrappers to orchestrate client, menus, and game start."""

import re
from typing import Optional, Callable

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result
from universalchess.utils.token_display import mask_token

_MISSING_SCOPE = re.compile(r"Missing scope:\s*([a-z0-9:_-]+)", re.IGNORECASE)


def _lichess_permission_panel_message(error_msg: str, fallback: str) -> Optional[str]:
    """E-paper copy for a missing OAuth scope or HTTP 401/403, else None.

    Listing challenges with ``board:play`` but without ``challenge:read`` is
    HTTP 403 ``Missing scope: challenge:read``, not 401. Mapping only 401
    showed a truncated HTTP dump instead of naming the scope to add.
    """
    match = _MISSING_SCOPE.search(error_msg)
    if match:
        return f"Token needs\n{match.group(1)}"
    lowered = error_msg.lower()
    if (
        "401" in error_msg
        or "403" in error_msg
        or "unauthorized" in lowered
        or "forbidden" in lowered
    ):
        return fallback
    return None


def get_lichess_client(token, log, host_id: str = "org"):
    """Get a berserk client and username, with error classification."""
    if not token or token == "tokenhere":  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret
        log.warning("[Lichess] No valid API token configured")
        return None, None, "no_token"
    try:
        from .match import create_berserk_client

        client = create_berserk_client(token, host_id=host_id)
        user_info = client.account.get()
        username = user_info.get("username", "")
        log.info(f"[Lichess] Authenticated as: {username}")
        return client, username, None
    except ImportError:
        log.error("[Lichess] berserk library not installed")
        return None, None, "no_berserk"
    except Exception as e:
        log.error(f"[Lichess] Failed to connect to Lichess: {e}")
        return None, None, "network"


def resolve_lichess_identity(token, log=None, host_id: str = "org"):
    """Authenticate an explicit Lichess token and return its account identity.

    Unlike :func:`get_lichess_client`, which uses a stored token, this
    verifies a token supplied for a *new* credential (before it is saved) so
    the plugin can key it as ``org:alice``. ``host_id`` selects which Lichess
    server to ask. Returns a :class:`account_store.ResolvedIdentity`.
    """
    from universalchess.services.account_store import ResolvedIdentity

    if not token or token == "tokenhere":  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret
        return ResolvedIdentity(error="no_token", message="No API token provided")
    try:
        from .match import create_berserk_client

        client = create_berserk_client(token, host_id=host_id)
        username = client.account.get().get("username", "")
        if not username:
            return ResolvedIdentity(error="auth_failed", message="Could not read Lichess account")
        return ResolvedIdentity(identity=username)
    except ImportError:
        if log:
            log.error("[Lichess] berserk library not installed")
        return ResolvedIdentity(error="no_berserk", message="Lichess client library not installed")
    except Exception as e:
        if log:
            log.error(f"[Lichess] Token verification failed: {e}")
        return ResolvedIdentity(error="auth_failed", message="Could not verify token with Lichess")


def build_lichess_menu_entries(username: Optional[str], ongoing_games: bool, has_challenges: bool):
    """Build top-level Lichess menu entries.

    The username is a header row: shown, but non-selectable (``selectable=False``)
    rather than ``enabled=False`` -- the latter would render it faded/disabled,
    and previously hid it outright. Sections with nothing to open (no ongoing
    games, no challenges) are *omitted* rather than shown disabled, since hiding
    a row is done by leaving it out, never by ``enabled=False``.
    """
    user_label = f"User\n{username}" if username else "User\nUnknown"
    entries = [
        IconMenuEntry(key="User", label=user_label, icon_name="lichess", selectable=False),
        IconMenuEntry(key="NewGame", label="New Game", icon_name="play"),
    ]
    if ongoing_games:
        entries.append(IconMenuEntry(key="Ongoing", label="Ongoing\nGames", icon_name="lichess"))
    if has_challenges:
        entries.append(IconMenuEntry(key="Challenges", label="Challenges", icon_name="lichess"))
    entries.append(IconMenuEntry(key="Token", label="API Token", icon_name="lichess"))
    return entries


def lichess_waiting_message(mode) -> str:
    """Copy shown on the panel while a Lichess game is being found or joined."""
    from .match import lichess_waiting_message as _waiting

    return _waiting(mode)


def show_lichess_waiting_splash(panel_manager, mode) -> bool:
    """Paint the Lichess waiting splash and wait until it reaches the e-paper.

    Uses :func:`show_fullscreen_splash` so the frame is on the panel before the
    caller continues. A plain ``add_widget`` without waiting lost the race
    against ``DisplayManager._init_widgets`` (which ``clear_widgets``), so the
    seek wait showed an empty chess board instead of this message.
    """
    from universalchess.epaper.splash_screen import show_fullscreen_splash

    return show_fullscreen_splash(panel_manager, lichess_waiting_message(mode))


def show_lichess_started_splash(panel_manager, human_is_white: bool) -> bool:
    """Replace the waiting splash with 'Game started / You play White|Black'.

    Updates an existing SplashScreen when present so the e-paper does not clear
    to an empty board between waiting and started. Falls back to a new splash.
    """
    from universalchess.epaper.splash_screen import SplashScreen, show_fullscreen_splash
    from .match import lichess_started_message

    message = lichess_started_message(human_is_white)
    widgets = getattr(panel_manager, "_widgets", None) or []
    for widget in widgets:
        if isinstance(widget, SplashScreen):
            widget.set_message(message)
            if hasattr(panel_manager, "update"):
                panel_manager.update()
            return True
    return show_fullscreen_splash(panel_manager, message)


def show_lichess_error(menu_manager, title: str, message: str, show_accounts_button: bool = False):
    """Show a blocking error splash; any key dismisses it.

    ``message`` is the e-paper copy (pairing/clock/token). ``title`` is logged
    by the caller. Previously this was a one-row menu whose only entry was
    ``selectable=False``, so the copy was truncated and no key could dismiss it.
    """
    text = message or title
    board_mod = getattr(menu_manager, "_board", None)
    panel = getattr(board_mod, "display_manager", None) if board_mod is not None else None
    bind_keys = getattr(menu_manager, "_error_splash_binder", None)
    from universalchess.epaper.splash_screen import show_dismissible_splash

    if show_dismissible_splash(panel, text, bind_keys=bind_keys):
        return None
    entries = [
        IconMenuEntry(key="BACK", label=text, icon_name="cancel", enabled=True, selectable=True)
    ]
    return menu_manager.show_menu(entries)


def _lichess_slot_settings(settings):
    """PlayerSettings for the Lichess slot, or None if neither slot is Lichess."""
    p1 = settings.player1
    p2 = settings.player2
    if p1.type == "lichess":
        return p1
    if p2.type == "lichess":
        return p2
    return None


def active_lichess_account(settings):
    """Bound (or default) Lichess credential, or None."""
    from .accounts import (
        default_lichess_credential,
        get_lichess_credential,
    )

    ps = _lichess_slot_settings(settings)
    account_id = getattr(ps, "account", "") if ps is not None else ""
    if account_id:
        return get_lichess_credential(account_id)
    return default_lichess_credential()


def lichess_client_from_settings(settings, log):
    """Authenticate the bound credential for lobby Ongoing/Challenges lists."""
    from .player import LichessPlayer, LichessPlayerConfig

    ps = _lichess_slot_settings(settings)
    account_id = getattr(ps, "account", "") if ps is not None else ""
    player = LichessPlayer(LichessPlayerConfig(account_id=account_id))
    token, _range = player._resolve_account()
    host_id = getattr(player, "_host_id", "org")
    return get_lichess_client(token, log, host_id=host_id)


def ensure_token(
    menu_manager,
    keyboard_factory: Callable,
    get_token: Callable[[], str],
    set_token: Callable[[str], None],
    log,
    board,
    set_active_keyboard: Callable[[object], None],
    clear_active_keyboard: Callable[[], None],
):
    """Prompt for token entry.

    Registers the keyboard as the application's active keyboard widget so board
    key presses (BACK/TICK/UP/DOWN/PLAY) and piece placements are routed to it,
    mirroring the WiFi-password and player-name entry paths. Without this
    registration the keyboard is unresponsive - keys and typing never reach it,
    so even BACK does nothing (the "Back does not go back in Lichess" bug). The
    active keyboard is cleared in a finally so a cancel or exception cannot leave
    a stale keyboard swallowing later menu input.

    Calls ``keyboard_factory`` positionally as ``(update_fn, title, max_length)``,
    matching its documented contract and every other call site (e.g. wifi
    password entry). Passing the limit by keyword previously broke here because
    the app's factories name the parameter ``max_len``; positional invocation is
    immune to that parameter-name drift.
    """
    board.display_manager.clear_widgets(addStatusBar=False)
    keyboard = keyboard_factory(board.display_manager.update, "Lichess Token", 64)
    keyboard.text = get_token() or ""
    set_active_keyboard(keyboard)
    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:  # noqa: S110 # nosec B110 - best-effort wait for the widget to render; input is still accepted below
            pass
    try:
        result = keyboard.wait_for_input(timeout=300.0)
        if result is not None:
            set_token(result)
            log.info(f"[Accounts] Lichess token saved ({len(result)} chars)")
            board.beep(board.SOUND_GENERAL)
        return result
    finally:
        clear_active_keyboard()


def show_lichess_ongoing_games(client, menu_manager, log) -> Optional[str]:
    """Show list of ongoing Lichess games and return selected game ID.

    Args:
        client: berserk Lichess client
        menu_manager: Menu manager for displaying menu
        log: Logger instance

    Returns:
        Game ID if selected, None if cancelled
    """
    try:
        ongoing = client.games.get_ongoing(count=10)

        if not ongoing:
            show_lichess_error(menu_manager, "No Games", "No ongoing\ngames found")
            return None

        entries = []
        for game in ongoing:
            game_id = game.get("gameId", "")
            opponent = game.get("opponent", {})
            opponent_name = opponent.get("username", "Unknown")
            opponent_rating = opponent.get("rating", "")
            color = "W" if game.get("color") == "white" else "B"

            label = f"{opponent_name}\n({opponent_rating}) {color}"
            entries.append(
                IconMenuEntry(
                    key=game_id,
                    label=label,
                    icon_name="lichess",
                    enabled=True,
                    font_size=12,
                )
            )

        result = menu_manager.show_menu(entries)

        if result.is_break or result.key == "BACK":
            return None

        return result.key

    except AttributeError as e:
        log.error(f"[Lichess] berserk API method not found: {e}")
        show_lichess_error(
            menu_manager,
            "API Not Supported",
            "Ongoing games API\nnot available.\nUpdate berserk:\npip install -U berserk",
        )
        return None
    except Exception as e:
        error_msg = str(e)
        log.error(f"[Lichess] Error fetching ongoing games: {e}")
        permission = _lichess_permission_panel_message(
            error_msg, "Token does not have\nboard:play permission"
        )
        if permission:
            show_lichess_error(menu_manager, "Auth Error", permission)
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            show_lichess_error(menu_manager, "Network Error", "Could not connect\nto Lichess")
        else:
            short_error = error_msg[:40] + "..." if len(error_msg) > 40 else error_msg
            show_lichess_error(menu_manager, "Error", f"Games failed:\n{short_error}")
        return None


def show_lichess_challenges(client, menu_manager, log) -> Optional[dict]:
    """Show list of Lichess challenges and return selected challenge.

    Args:
        client: berserk Lichess client
        menu_manager: Menu manager for displaying menu
        log: Logger instance

    Returns:
        Dict with 'id' and 'direction' if selected, None if cancelled
    """
    try:
        challenges_data = None
        try:
            challenges_data = client.challenges.get_mine()
        except AttributeError:
            try:
                challenges_data = client.challenges.list()
            except AttributeError:  # noqa: S110 # nosec B110 - berserk API-name fallback; None is handled just below
                pass

        if challenges_data is None:
            log.error("[Lichess] berserk library does not support challenges API")
            show_lichess_error(
                menu_manager,
                "API Not Supported",
                "Challenges require\nberserk >= 0.13\nUpdate with:\npip install -U berserk",
            )
            return None

        incoming = list(challenges_data.get("in", []))
        outgoing = list(challenges_data.get("out", []))

        if not incoming and not outgoing:
            show_lichess_error(menu_manager, "No Challenges", "No pending\nchallenges")
            return None

        entries = []

        for challenge in incoming:
            c_id = challenge.get("id", "")
            challenger = challenge.get("challenger", {})
            name = challenger.get("name", "Unknown")
            rating = challenger.get("rating", "")

            label = f"IN: {name}\n({rating})"
            entries.append(
                IconMenuEntry(
                    key=f"in:{c_id}",
                    label=label,
                    icon_name="lichess",
                    enabled=True,
                    font_size=12,
                )
            )

        for challenge in outgoing:
            c_id = challenge.get("id", "")
            dest = challenge.get("destUser", {})
            name = dest.get("name", "Unknown")
            rating = dest.get("rating", "")

            label = f"OUT: {name}\n({rating})"
            entries.append(
                IconMenuEntry(
                    key=f"out:{c_id}",
                    label=label,
                    icon_name="lichess",
                    enabled=True,
                    font_size=12,
                )
            )

        result = menu_manager.show_menu(entries)

        if result.is_break or result.key == "BACK":
            return None

        direction, c_id = result.key.split(":", 1)
        return {"id": c_id, "direction": direction}

    except AttributeError as e:
        log.error(f"[Lichess] berserk API method not found: {e}")
        show_lichess_error(
            menu_manager,
            "API Not Supported",
            "Challenges require\nberserk >= 0.13\nUpdate with:\npip install -U berserk",
        )
        return None
    except Exception as e:
        error_msg = str(e)
        log.error(f"[Lichess] Error fetching challenges: {e}")
        permission = _lichess_permission_panel_message(
            error_msg, "Token does not have\nchallenge permissions"
        )
        if permission:
            show_lichess_error(menu_manager, "Auth Error", permission)
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            show_lichess_error(menu_manager, "Network Error", "Could not connect\nto Lichess")
        else:
            short_error = error_msg[:40] + "..." if len(error_msg) > 40 else error_msg
            show_lichess_error(menu_manager, "Error", f"Challenges failed:\n{short_error}")
        return None


def handle_lichess_menu(
    get_lichess_client_fn: Callable,
    get_settings_fn: Callable,
    menu_manager,
    keyboard_factory: Callable,
    start_lichess_game_fn: Callable,
    handle_accounts_menu_fn: Callable,
    centaur_module,
    board,
    log,
    set_active_keyboard: Callable,
    clear_active_keyboard: Callable,
):
    """Handle Lichess submenu with New Game, Ongoing, and Challenges options.

    Args:
        get_lichess_client_fn: Callback to get (client, username, error) tuple
        get_settings_fn: Callback to get AllSettings instance
        menu_manager: MenuManager instance
        keyboard_factory: Factory for KeyboardWidget(update_fn, title, max_length)
        start_lichess_game_fn: Callback to start a Lichess game with config
        handle_accounts_menu_fn: Callback to show accounts menu
        centaur_module: Unused; kept so callers of this function need not change.
        board: Board module
        log: Logger instance
        set_active_keyboard: Register a keyboard widget as active so board input
            is routed to it (forwarded to ensure_token for the Token entry).
        clear_active_keyboard: Clear the active keyboard registration.

    Returns:
        ``"START_GAME"`` if a Lichess game was requested (join stashed; Settings
        starts it after menus unwind), a break result if a break action, or
        None otherwise.
    """
    from .player import LichessPlayerConfig as LichessConfig, LichessGameMode
    from universalchess.managers.menu import MenuSelection

    client, username, error = get_lichess_client_fn()
    if client is None:
        if error == "no_token":
            result = show_lichess_error(menu_manager, "No API Token", "Configure in\nSystem > Accounts", True)
        elif error == "invalid_token":
            result = show_lichess_error(menu_manager, "Invalid Token", "Token expired or\nrevoked", True)
        elif error == "no_berserk":
            show_lichess_error(menu_manager, "Missing Library", "berserk package\nnot installed")
            result = None
        else:
            show_lichess_error(menu_manager, "Connection Error", "Could not reach\nLichess server")
            result = None
        if result == "accounts":
            handle_accounts_menu_fn()
        return None

    entries = build_lichess_menu_entries(username, ongoing_games=True, has_challenges=True)

    # Track if game was started successfully
    game_started = False

    def start_game(config):
        """Start a Lichess game; swallow failures so the menu thread stays alive.

        ``start_lichess_game_fn`` runs on the menu thread. An uncaught exception
        there ends the main loop, runs cleanup, and leaves the last e-paper frame
        on screen -- the "freeze" from pressing New Game when an import path was
        stale. The error is logged and shown; the menu remains usable.
        """
        nonlocal game_started
        try:
            if start_lichess_game_fn(config):
                game_started = True
                return True
        except Exception as e:
            log.error(f"[Lichess] Failed to start game: {e}", exc_info=True)
            show_lichess_error(menu_manager, "Start Failed", "Could not start\nLichess game")
        return False

    def handle_selection(result: MenuSelection):
        if result.key == "NewGame":
            config = LichessConfig(mode=LichessGameMode.NEW)
            if start_game(config):
                return result
            return None
        elif result.key == "Ongoing":
            game_id = show_lichess_ongoing_games(client, menu_manager, log)
            if game_id:
                config = LichessConfig(mode=LichessGameMode.ONGOING, game_id=game_id)
                if start_game(config):
                    return result
            return None
        elif result.key == "Challenges":
            challenge = show_lichess_challenges(client, menu_manager, log)
            if challenge:
                config = LichessConfig(
                    mode=LichessGameMode.CHALLENGE,
                    challenge_id=challenge["id"],
                    challenge_direction=challenge["direction"],
                )
                if start_game(config):
                    return result
            return None
        elif result.key == "Token":
            from universalchess.services import account_store

            account = active_lichess_account(get_settings_fn())
            if account is None:
                show_lichess_error(
                    menu_manager,
                    "No Account",
                    "Add an account\nin Accounts",
                )
                return None

            def get_token():
                return account.get("api_token", "")

            def set_token(value):
                account.values["api_token"] = value
                account_store.save_account(account)

            ensure_token(
                menu_manager=menu_manager,
                keyboard_factory=keyboard_factory,
                get_token=get_token,
                set_token=set_token,
                log=log,
                board=board,
                set_active_keyboard=set_active_keyboard,
                clear_active_keyboard=clear_active_keyboard,
            )
        return None

    result = menu_manager.run_menu_loop(lambda: entries, handle_selection)

    if is_break_result(result):
        return result
    if game_started:
        # Same token as Players → Start Game. The lobby only stashes the join;
        # Settings starts the game after nested menus have exited, so they cannot
        # redraw player rows over the board.
        return "START_GAME"
    return None

