"""Lichess service wrappers to orchestrate client, menus, and game start."""

from dataclasses import dataclass
from typing import Optional, Callable, Tuple

import chess

from universalchess.menus.accounts_menu import mask_token
from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result


def get_lichess_client(centaur_module, log):
    """Get a berserk client and username, with error classification."""
    token = centaur_module.get_lichess_api()
    if not token or token == "tokenhere":
        log.warning("[Lichess] No valid API token configured")
        return None, None, "no_token"
    try:
        import berserk

        session = berserk.TokenSession(token)
        client = berserk.Client(session=session)
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


def build_lichess_menu_entries(username: Optional[str], ongoing_games: bool, has_challenges: bool):
    """Build top-level Lichess menu entries."""
    user_label = f"User\n{username}" if username else "User\nUnknown"
    entries = [
        IconMenuEntry(key="User", label=user_label, icon_name="lichess", enabled=False, selectable=False),
        IconMenuEntry(key="NewGame", label="New Game", icon_name="play", enabled=True),
        IconMenuEntry(key="Ongoing", label="Ongoing\nGames", icon_name="lichess", enabled=ongoing_games),
        IconMenuEntry(key="Challenges", label="Challenges", icon_name="lichess", enabled=has_challenges),
        IconMenuEntry(key="Token", label="API Token", icon_name="lichess", enabled=True),
    ]
    return entries


def show_lichess_error(menu_manager, title: str, message: str, show_accounts_button: bool = False):
    """Show a blocking error message."""
    entries = [IconMenuEntry(key="BACK", label=title, icon_name="cancel", enabled=True, selectable=False)]
    return menu_manager.show_menu(entries)


def show_lichess_mode_menu(menu_manager, find_entry_index, current_mode_key: str = "seek") -> str:
    """Mode chooser for Lichess new game."""
    entries = [
        IconMenuEntry(key="seek", label="Seek\nGame", icon_name="play", enabled=True),
        IconMenuEntry(key="challenge", label="Challenge", icon_name="lichess", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=find_entry_index(entries, current_mode_key))
    return result.key if hasattr(result, "key") else result


def build_new_game_entries(time_options, default_time: int, default_increment: int, rated: bool):
    """Build new game seek entries."""
    rating_label = "Rated" if rated else "Casual"
    return [
        IconMenuEntry(
            key="Time",
            label=f"Time\n{default_time}+{default_increment}",
            icon_name="timer_checked",
            enabled=True,
        ),
        IconMenuEntry(
            key="Rated",
            label=rating_label,
            icon_name="checkbox_checked" if rated else "checkbox_empty",
            enabled=True,
        ),
        IconMenuEntry(
            key="Start",
            label="Start\nSeek",
            icon_name="play",
            enabled=True,
        ),
    ]


def show_time_control_menu(menu_manager, find_entry_index, time_options, current_minutes, current_increment):
    """Select time control for Lichess seek."""
    entries = []
    current_key = f"{current_minutes}+{current_increment}"
    for minutes, inc in time_options:
        key = f"{minutes}+{inc}"
        label = f"{minutes}+{inc}"
        icon = "checkbox_checked" if key == current_key else "checkbox_empty"
        entries.append(IconMenuEntry(key=key, label=label, icon_name=icon, enabled=True))
    result = menu_manager.show_menu(entries, initial_index=find_entry_index(entries, current_key))
    return result.key if hasattr(result, "key") else result


def ensure_token(menu_manager, keyboard_factory: Callable, get_token: Callable[[], str], set_token: Callable[[str], None], log, board):
    """Prompt for token entry.

    Calls ``keyboard_factory`` positionally as ``(update_fn, title, max_length)``,
    matching its documented contract and every other call site (e.g. wifi
    password entry). Passing the limit by keyword previously broke here because
    the app's factories name the parameter ``max_len``; positional invocation is
    immune to that parameter-name drift.
    """
    keyboard = keyboard_factory(board.display_manager.update, "Lichess Token", 64)
    keyboard.text = get_token() or ""
    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:
            pass
    result = keyboard.wait_for_input(timeout=300.0)
    if result is not None:
        set_token(result)
        log.info(f"[Accounts] Lichess token saved ({len(result)} chars)")
        board.beep(board.SOUND_GENERAL)
    return result


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
        if "401" in error_msg or "unauthorized" in error_msg.lower():
            show_lichess_error(menu_manager, "Auth Error", "Token does not have\nboard:play permission")
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
            except AttributeError:
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
        if "401" in error_msg or "unauthorized" in error_msg.lower():
            show_lichess_error(menu_manager, "Auth Error", "Token does not have\nchallenge permissions")
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            show_lichess_error(menu_manager, "Network Error", "Could not connect\nto Lichess")
        else:
            short_error = error_msg[:40] + "..." if len(error_msg) > 40 else error_msg
            show_lichess_error(menu_manager, "Error", f"Challenges failed:\n{short_error}")
        return None


@dataclass
class LichessStartResult:
    success: bool
    protocol_manager: Optional[object] = None
    display_manager: Optional[object] = None
    controller_manager: Optional[object] = None


def start_lichess_game_service(
    lichess_config,
    game_settings: dict,
    board,
    log,
    menu_manager,
    connection_manager,
    return_to_menu_fn: Callable[[str], None],
    cleanup_game_fn: Callable[[], None],
    set_app_state_fn: Callable[[object], None],
    app_state_game,
    app_state_settings,
    get_engine_path: Callable[[str], str],
) -> LichessStartResult:
    """Start a Lichess game and wire callbacks."""
    from universalchess.display_manager import DisplayManager
    from universalchess.epaper import SplashScreen, InfoOverlayWidget
    from universalchess.protocol.protocol_manager import ProtocolManager
    from universalchess.players import HumanPlayer, PlayerManager
    from universalchess.players.lichess import LichessPlayer, LichessPlayerConfig
    from universalchess.managers.game import GameManager
    from universalchess.managers.controller import ControllerManager
    from universalchess.managers import EVENT_WHITE_TURN, EVENT_BLACK_TURN
    import chess

    set_app_state_fn(app_state_game)

    analysis_mode = game_settings.get("analysis_mode", False)
    analysis_engine = game_settings.get("analysis_engine")
    show_analysis = game_settings.get("show_analysis", False)
    show_board = game_settings.get("show_board", True)
    show_clock = True
    show_graph = game_settings.get("show_graph", False)
    analysis_engine_path = get_engine_path(analysis_engine) if analysis_mode else None

    display_manager = DisplayManager(
        flip_board=False,
        show_analysis=show_analysis,
        analysis_engine_path=analysis_engine_path,
        on_exit=lambda: return_to_menu_fn("Lichess exit"),
        initial_fen=None,
        time_control=0,
        show_board=show_board,
        show_clock=show_clock,
        show_graph=show_graph,
        analysis_mode=analysis_mode,
    )

    waiting_message = "Finding Game..."
    from universalchess.players.lichess import LichessGameMode

    if lichess_config.mode == LichessGameMode.ONGOING:
        waiting_message = "Connecting..."
    elif lichess_config.mode == LichessGameMode.CHALLENGE:
        waiting_message = "Loading\nChallenge..."

    waiting_splash = SplashScreen(board.display_manager.update, message=waiting_message)
    board.display_manager.add_widget(waiting_splash)

    game_connected = False
    user_cancelled = False

    def on_game_connected():
        nonlocal game_connected
        if game_connected:
            return
        game_connected = True
        board.display_manager.remove_widget(waiting_splash)

    def on_back_during_waiting():
        nonlocal user_cancelled
        if game_connected:
            display_manager.show_back_menu(_on_lichess_back_menu_result, is_two_player=False)
        else:
            user_cancelled = True
            protocol_manager.stop_lichess()
            cleanup_game_fn()
            set_app_state_fn(app_state_settings)

    lichess_player_config = LichessPlayerConfig(
        name="Lichess",
        mode=lichess_config.mode,
        time_minutes=lichess_config.time_minutes,
        increment_seconds=lichess_config.increment_seconds,
        rated=lichess_config.rated,
        color_preference=getattr(lichess_config, "color_preference", "random"),
        game_id=getattr(lichess_config, "game_id", ""),
        challenge_id=getattr(lichess_config, "challenge_id", ""),
        challenge_direction=getattr(lichess_config, "challenge_direction", "in"),
    )
    lichess_player = LichessPlayer(lichess_player_config)
    human_player = HumanPlayer()

    white_player = human_player
    black_player = lichess_player

    game_manager = GameManager(save_to_database=True)
    protocol_manager = ProtocolManager(game_manager=game_manager)

    player_manager = PlayerManager(
        white_player=white_player,
        black_player=black_player,
        status_callback=lambda msg: log.info(f"[Player] {msg}"),
    )
    protocol_manager.set_player_manager(player_manager)

    controller_manager = ControllerManager(game_manager)
    local_controller = controller_manager.create_local_controller()
    local_controller.set_player_manager(player_manager)
    player_manager.set_ready_callback(local_controller.on_all_players_ready)
    protocol_manager.set_on_promotion_needed(display_manager.show_promotion_menu)

    def _get_lichess_player():
        if player_manager:
            for player in [player_manager.white_player, player_manager.black_player]:
                if isinstance(player, LichessPlayer):
                    return player
        return None

    lichess_player_instance = _get_lichess_player()
    if lichess_player_instance:
        lichess_player_instance.set_on_game_connected(on_game_connected)

    _info_overlay = InfoOverlayWidget(0, 216, 128, 80, board.display_manager.update)
    board.display_manager.add_widget(_info_overlay)

    def _on_lichess_game_over(result: str, termination: str, winner):
        log.info(f"[App] Lichess game over: result={result}, termination={termination}, winner={winner}")
        display_manager.stop_clock()
        from universalchess.state import get_chess_game
        get_chess_game().set_result(result, termination)

    def _on_lichess_takeback_offer(accept_fn, decline_fn):
        log.info("[App] Lichess takeback offer received")
        board.beep(board.SOUND_GENERAL)
        entries = [
            IconMenuEntry(key="accept", label="Accept\nTakeback", icon_name="undo"),
            IconMenuEntry(key="decline", label="Decline", icon_name="cancel"),
        ]
        result = menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            log.info("[App] User accepted takeback")
            accept_fn()
        else:
            log.info("[App] User declined takeback")
            decline_fn()

    def _on_lichess_draw_offer(accept_fn, decline_fn):
        log.info("[App] Lichess draw offer received")
        board.beep(board.SOUND_GENERAL)
        entries = [
            IconMenuEntry(key="accept", label="Accept\nDraw", icon_name="draw"),
            IconMenuEntry(key="decline", label="Decline", icon_name="cancel"),
        ]
        result = menu_manager.show_menu(entries)
        if hasattr(result, "key") and result.key == "accept":
            log.info("[App] User accepted draw")
            accept_fn()
        else:
            log.info("[App] User declined draw")
            decline_fn()

    def _on_lichess_info_message(message: str):
        log.info(f"[App] Lichess info message: {message}")
        _info_overlay.show_message(message, duration_seconds=5.0)

    lichess_player_instance = _get_lichess_player()
    if lichess_player_instance:
        lichess_player_instance.set_game_over_callback(_on_lichess_game_over)
        lichess_player_instance.set_takeback_offer_callback(_on_lichess_takeback_offer)
        lichess_player_instance.set_draw_offer_callback(_on_lichess_draw_offer)
        lichess_player_instance.set_info_message_callback(_on_lichess_info_message)

    def _on_lichess_back_menu_result(action: str):
        lichess_player_local = _get_lichess_player()
        if not lichess_player_local:
            log.warning("[App] No LichessPlayer found for action")
            return
        if action == "resign":
            log.info("[App] User resigned Lichess game")
            lichess_player_local.on_resign(chess.WHITE)
            return_to_menu_fn("Lichess resign")
        elif action == "abort":
            log.info("[App] User aborted Lichess game")
            lichess_player_local.abort_game()
            return_to_menu_fn("Lichess abort")
        elif action == "draw":
            log.info("[App] User offered draw in Lichess game")
            lichess_player_local.on_draw_offer()

    protocol_manager.set_on_back_pressed(on_back_during_waiting)

    _clock_started = False

    def _on_lichess_game_event(event):
        nonlocal _clock_started
        if event == EVENT_WHITE_TURN or event == EVENT_BLACK_TURN:
            if not _clock_started:
                display_manager.start_clock()
                _clock_started = True
                log.debug("[App] Lichess clock started")
            _info_overlay.hide()

    local_controller.set_external_event_callback(_on_lichess_game_event)
    controller_manager.activate_local()
    connection_manager.set_controller_manager(controller_manager)

    if not protocol_manager.start_lichess():
        log.error("[App] Failed to start Lichess connection")
        cleanup_game_fn()
        show_lichess_error(menu_manager, "Connection Failed", "Could not connect\nto Lichess")
        set_app_state_fn(app_state_settings)
        return LichessStartResult(False)

    log.info("[App] Lichess connection started - waiting for game match")
    return LichessStartResult(
        success=True,
        protocol_manager=protocol_manager,
        display_manager=display_manager,
        controller_manager=controller_manager,
    )


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
):
    """Handle Lichess submenu with New Game, Ongoing, and Challenges options.

    Args:
        get_lichess_client_fn: Callback to get (client, username, error) tuple
        get_settings_fn: Callback to get AllSettings instance
        menu_manager: MenuManager instance
        keyboard_factory: Factory for KeyboardWidget(update_fn, title, max_length)
        start_lichess_game_fn: Callback to start a Lichess game with config
        handle_accounts_menu_fn: Callback to show accounts menu
        centaur_module: Centaur module for token get/set
        board: Board module
        log: Logger instance

    Returns:
        True if a Lichess game was started, break result if break action,
        None/False otherwise.
    """
    from universalchess.players.lichess import LichessPlayerConfig as LichessConfig, LichessGameMode
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

    def handle_selection(result: MenuSelection):
        nonlocal game_started
        if result.key == "NewGame":
            settings = get_settings_fn()
            color = settings.player1.color
            time_minutes = settings.game.time_control or 10
            config = LichessConfig(
                mode=LichessGameMode.NEW,
                time_minutes=time_minutes,
                increment_seconds=0,
                rated=False,
                color_preference=color,
            )
            if start_lichess_game_fn(config):
                game_started = True
                return result
            return None
        elif result.key == "Ongoing":
            game_id = show_lichess_ongoing_games(client, menu_manager, log)
            if game_id:
                config = LichessConfig(mode=LichessGameMode.ONGOING, game_id=game_id)
                if start_lichess_game_fn(config):
                    game_started = True
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
                if start_lichess_game_fn(config):
                    game_started = True
                    return result
            return None
        elif result.key == "Token":
            ensure_token(
                menu_manager=menu_manager,
                keyboard_factory=keyboard_factory,
                get_token=centaur_module.get_lichess_api,
                set_token=centaur_module.set_lichess_api,
                log=log,
                board=board,
            )
        return None

    result = menu_manager.run_menu_loop(lambda: entries, handle_selection)

    if is_break_result(result):
        return result

    return game_started

