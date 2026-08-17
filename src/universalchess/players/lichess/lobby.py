"""Lichess service wrappers to orchestrate client, menus, and game start."""

import re
from typing import Optional, Callable

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result

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


DEFAULT_ACCOUNT_MENU_KEY = "Default"
ACCOUNTS_MENU_KEY = "Accounts"

ONGOING_GAMES_HELP = (
    "Continue a Lichess game this account already started, on this board, "
    "the website, or another device.\n\n"
    "Select a game to resume it here. The clock and your color come from "
    "that match.\n\n"
    "If none are listed, this account has no unfinished games. Use New Game "
    "to seek a new opponent."
)

CHALLENGES_HELP = (
    "A challenge is a game offered to this account, or that this account "
    "offered to someone else.\n\n"
    "Incoming: select one to accept it on this board. A challenge that "
    "arrives during a seek also shows Accept or Decline.\n\n"
    "Outgoing: waiting for the other player. New Game posts a public seek "
    "instead of challenging one person."
)


def build_lichess_menu_entries(username: Optional[str]):
    """Build Lichess Settings rows (the lobby, not a nested Play page).

    Account is first and selectable: it opens the account picker for the
    Lichess slot. Ongoing Games and Challenges are always listed; selecting
    either shows how it works, then the live list. New Game is last. Add or
    delete logins is Accounts on the picker, not a lobby sibling.
    """
    account_label = f"Account\n{username}" if username else "Account\nUnknown"
    return [
        IconMenuEntry(key="Account", label=account_label, icon_name="lichess"),
        IconMenuEntry(
            key="Ongoing",
            label="Ongoing\nGames",
            icon_name="lichess",
            help=ONGOING_GAMES_HELP,
        ),
        IconMenuEntry(
            key="Challenges",
            label="Challenges",
            icon_name="lichess",
            help=CHALLENGES_HELP,
        ),
        IconMenuEntry(key="NewGame", label="New Game", icon_name="play"),
    ]


def build_lichess_account_picker_entries(choices):
    """Radio rows for the Play account picker, plus Accounts last.

    ``choices`` is ``(key, label, selected)`` from
    :func:`lichess_account_picker_choices`. Unbound Default uses key ``""`` in
    that list; the menu row uses :data:`DEFAULT_ACCOUNT_MENU_KEY` so the widget
    has a non-empty key. Accounts is not a radio; it opens the credential
    manager. The row is always present so an empty credential list can still
    add a login.
    """
    entries = []
    for key, label, selected in choices:
        menu_key = DEFAULT_ACCOUNT_MENU_KEY if key == "" else key
        entries.append(
            IconMenuEntry(
                key=menu_key,
                label=label,
                icon_name="lichess",
                trailing_icon_name="radio_checked" if selected else "radio_empty",
            )
        )
    entries.append(
        IconMenuEntry(
            key=ACCOUNTS_MENU_KEY,
            label="Accounts",
            icon_name="account",
        )
    )
    return entries


def show_lichess_account_picker(menu_manager, choices):
    """Show the account picker and return the selected slot value, or None.

    The slot value is ``""`` for Default, otherwise the account id. Selecting
    Accounts returns :data:`ACCOUNTS_MENU_KEY` so the caller can open the
    credential manager without binding. BACK, SHUTDOWN, and HELP return None.
    Break results are returned unchanged so Play can unwind.
    """
    entries = build_lichess_account_picker_entries(choices)
    selected = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.trailing_icon_name == "radio_checked"
        ),
        0,
    )
    result = menu_manager.show_menu(entries, initial_index=selected)
    if result.is_break:
        return result
    if result.is_exit():
        return None
    if result.key == DEFAULT_ACCOUNT_MENU_KEY:
        return ""
    return result.key


def lichess_waiting_message(mode, seek=None) -> str:
    """Copy shown on the panel while a Lichess game is being found or joined."""
    from .match import lichess_waiting_message as _waiting

    return _waiting(mode, seek=seek)


def lichess_cancelling_message() -> str:
    """Copy shown after BACK while the seek is torn down."""
    from .match import lichess_cancelling_message as _cancelling

    return _cancelling()


def show_lichess_waiting_splash(panel_manager, mode, seek=None) -> bool:
    """Paint the Lichess waiting splash and wait until it reaches the e-paper.

    Uses :func:`show_fullscreen_splash` so the frame is on the panel before the
    caller continues. A plain ``add_widget`` without waiting lost the race
    against ``DisplayManager._init_widgets`` (which ``clear_widgets``), so the
    seek wait showed an empty chess board instead of this message.
    """
    from universalchess.epaper.splash_screen import show_fullscreen_splash

    return show_fullscreen_splash(
        panel_manager, lichess_waiting_message(mode, seek=seek)
    )


def show_lichess_cancelling_splash(panel_manager, timeout: float = 5.0) -> bool:
    """Change the waiting splash to 'Exiting...' and wait for that frame.

    Updates an existing SplashScreen so the panel does not flash empty. Waits
    for the paint Future: ``stop_players`` after BACK takes seconds, and
    clearing widgets first would drop this message before it reached e-paper.
    """
    from universalchess.board.logging import log
    from universalchess.epaper.splash_screen import SplashScreen, show_fullscreen_splash
    from .match import lichess_cancelling_message

    message = lichess_cancelling_message()
    widgets = getattr(panel_manager, "_widgets", None) or []
    for widget in widgets:
        if isinstance(widget, SplashScreen):
            widget.set_message(message)
            promise = panel_manager.update() if hasattr(panel_manager, "update") else None
            if promise:
                try:
                    promise.result(timeout=timeout)
                except Exception as e:
                    log.debug(f"[Lichess] Cancelling splash wait failed: {e}")
            return True
    return show_fullscreen_splash(panel_manager, message, timeout=timeout)


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


def confirm_lichess_seek(menu_manager) -> bool:
    """Show a Seek/Cancel confirmation and return True only if Seek is chosen.

    Used when setting the pieces back to the start would post a new Lichess
    seek. PLAY, lobby New Game, and web New Game are explicit and skip this.
    Defaults the highlight to Cancel so a stray TICK cannot register a seek;
    any non-Seek outcome (Cancel, BACK, break) is a refusal.
    """
    entries = [
        IconMenuEntry(
            key="prompt",
            label="Seek a\nnew game?",
            icon_name="lichess",
            enabled=True,
            selectable=False,
            font_size=12,
        ),
        IconMenuEntry(key="Seek", label="Seek", icon_name="play", enabled=True),
        IconMenuEntry(key="Cancel", label="Cancel", icon_name="undo", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=2)
    key = result.key if hasattr(result, "key") else result
    return key == "Seek"


def board_reset_rebuild_action(menu_manager, *, is_lichess: bool) -> str:
    """Decide whether a board-reset player rebuild may start.

    Setting the pieces back to the start rebuilds a Lichess game through
    ``_start_game_mode``, which posts a new seek. That gesture is not PLAY,
    lobby New Game, or web New Game, so it needs confirmation. Cancel (or no
    menu) returns ``menu`` so the caller leaves the game without seeking.
    Engine/human rebuilds return ``rebuild`` without a prompt. Confirmed
    Lichess returns ``seek`` so the caller stashes an explicit NEW join.
    """
    if not is_lichess:
        return "rebuild"
    if menu_manager is None or not confirm_lichess_seek(menu_manager):
        return "menu"
    return "seek"


def explicit_lichess_seek_join() -> dict:
    """Join stash that posts a new seek (PLAY, New Game, confirmed board-reset)."""
    from .player import LichessGameMode

    return {
        "mode": LichessGameMode.NEW,
        "game_id": "",
        "challenge_id": "",
        "challenge_direction": "in",
    }


def skip_unsolicited_lichess_start(*, is_lichess: bool, join, explicit_seek: bool) -> bool:
    """True when a Lichess slot would start without PLAY / New Game / a join.

    Piece lift, client connect, and boot piece events used to call
    ``_enter_game`` with join None, which posted a seek. A lobby join or an
    explicit PLAY/New Game must still start.
    """
    if not is_lichess:
        return False
    if join is not None:
        return False
    return not explicit_seek


def back_cancels_unready_game_start(*, has_game_managers: bool) -> bool:
    """True when BACK arrives in GAME before ProtocolManager exists.

    The waiting splash is painted first. Keys in that window were logged
    unhandled, then start() still posted the seek.
    """
    return not has_game_managers


def show_lichess_help(menu_manager, title: str, body: str) -> None:
    """Show how a Lichess Settings row works, then return to the caller.

    Uses the same help dialog as the HELP key when MenuManager has a presenter.
    Tests and a missing presenter fall back to the dismissible splash.
    """
    presenter = getattr(menu_manager, "_help_presenter", None)
    if presenter is not None:
        presenter(title, body)
        return
    board_mod = getattr(menu_manager, "_board", None)
    panel = getattr(board_mod, "display_manager", None) if board_mod is not None else None
    bind_keys = getattr(menu_manager, "_error_splash_binder", None)
    from universalchess.epaper.splash_screen import show_dismissible_splash

    show_dismissible_splash(panel, body, bind_keys=bind_keys)


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


def ongoing_game_summaries(raw_games) -> list:
    """Normalize ``GET /api/account/playing`` rows for board and web lobbies.

    Each item is ``id``, ``opponent``, ``rating``, ``color`` (``white``/``black``).
    Rows without a game id are dropped so a truncated payload cannot be joined.
    """
    from .player import ongoing_game_id

    rows = []
    for game in raw_games or []:
        game_id = ongoing_game_id(game)
        if not game_id:
            continue
        opponent = game.get("opponent") or {}
        rating = opponent.get("rating")
        rows.append(
            {
                "id": game_id,
                "opponent": opponent.get("username") or "Unknown",
                "rating": "" if rating is None else rating,
                "color": "white" if game.get("color") == "white" else "black",
            }
        )
    return rows


def challenge_summaries(payload) -> list:
    """Normalize ``challenges.get_mine()`` into lobby rows.

    Incoming first, then outgoing. Each item is ``id``, ``direction`` (``in``/
    ``out``), ``name``, ``rating``. Rows without an id are dropped.
    """
    payload = payload or {}
    rows = []
    for challenge in payload.get("in") or []:
        row = _challenge_summary(challenge, "in")
        if row is not None:
            rows.append(row)
    for challenge in payload.get("out") or []:
        row = _challenge_summary(challenge, "out")
        if row is not None:
            rows.append(row)
    return rows


def _challenge_summary(challenge: dict, direction: str) -> Optional[dict]:
    challenge_id = (challenge or {}).get("id")
    if not challenge_id:
        return None
    person = (
        challenge.get("challenger") if direction == "in" else challenge.get("destUser")
    ) or {}
    rating = person.get("rating")
    return {
        "id": str(challenge_id),
        "direction": direction,
        "name": person.get("name") or person.get("username") or "Unknown",
        "rating": "" if rating is None else rating,
    }


def lichess_join_from_web_params(params: dict) -> Optional[dict]:
    """Parse a ``lichess_start`` board command into the ``_lichess_join`` stash.

    ``mode`` is ``new``, ``ongoing``, or ``challenge``. Ongoing requires
    ``game_id``; challenge requires ``challenge_id`` and ``in``/``out``.
    Returns None when the payload cannot start a join, so the board ignores it
    instead of seeking with a truncated id.
    """
    from .player import LichessGameMode

    if not isinstance(params, dict):
        return None
    modes = {
        "new": LichessGameMode.NEW,
        "ongoing": LichessGameMode.ONGOING,
        "challenge": LichessGameMode.CHALLENGE,
    }
    mode = modes.get(str(params.get("mode") or "").strip().lower())
    if mode is None:
        return None
    game_id = str(params.get("game_id") or "").strip()
    challenge_id = str(params.get("challenge_id") or "").strip()
    direction = str(params.get("challenge_direction") or "in").strip().lower()
    if mode is LichessGameMode.ONGOING and not game_id:
        return None
    if mode is LichessGameMode.CHALLENGE:
        if not challenge_id or direction not in ("in", "out"):
            return None
    return {
        "mode": mode,
        "game_id": game_id,
        "challenge_id": challenge_id,
        "challenge_direction": direction if mode is LichessGameMode.CHALLENGE else "in",
    }


def show_lichess_ongoing_games(client, menu_manager, log) -> Optional[str]:
    """Show list of ongoing Lichess games and return selected game ID.

    An empty list returns None without a splash: the caller already showed
    how Ongoing Games work.

    Args:
        client: berserk Lichess client
        menu_manager: Menu manager for displaying menu
        log: Logger instance

    Returns:
        Game ID if selected, None if cancelled or none exist
    """
    try:
        ongoing = client.games.get_ongoing(count=10)
        summaries = ongoing_game_summaries(ongoing)

        if not summaries:
            return None

        entries = []
        for row in summaries:
            color = "W" if row["color"] == "white" else "B"
            label = f"{row['opponent']}\n({row['rating']}) {color}"
            entries.append(
                IconMenuEntry(
                    key=row["id"],
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

    An empty list returns None without a splash: the caller already showed
    how Challenges work.

    Args:
        client: berserk Lichess client
        menu_manager: Menu manager for displaying menu
        log: Logger instance

    Returns:
        Dict with 'id' and 'direction' if selected, None if cancelled or none exist
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
        summaries = challenge_summaries({"in": incoming, "out": outgoing})

        if not summaries:
            return None

        entries = []
        for row in summaries:
            prefix = "IN" if row["direction"] == "in" else "OUT"
            label = f"{prefix}: {row['name']}\n({row['rating']})"
            entries.append(
                IconMenuEntry(
                    key=f"{row['direction']}:{row['id']}",
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
    menu_manager,
    start_lichess_game_fn: Callable,
    handle_accounts_menu_fn: Callable,
    log,
    list_account_choices_fn: Optional[Callable] = None,
    bind_account_fn: Optional[Callable] = None,
):
    """Handle Lichess Settings: Account, Ongoing, Challenges, New Game.

    Args:
        get_lichess_client_fn: Callback to get (client, username, error) tuple
        menu_manager: MenuManager instance
        start_lichess_game_fn: Callback to start a Lichess game with config
        handle_accounts_menu_fn: Callback to show accounts menu (picker Accounts)
        log: Logger instance
        list_account_choices_fn: ``() -> [(key, label, selected), ...]`` for the
            Account picker. Optional so start-only tests can omit it.
        bind_account_fn: ``(account_id) -> None``; ``""`` means Default.

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

    def refresh_client() -> None:
        """Re-authenticate after an account bind so Account and lobby lists match."""
        nonlocal client, username
        new_client, new_username, new_error = get_lichess_client_fn()
        if new_client is None:
            log.warning(f"[Lichess] Account switch failed: {new_error}")
            show_lichess_error(
                menu_manager, "Account", "Could not sign in\nwith that account"
            )
            return
        client = new_client
        username = new_username

    def handle_selection(result: MenuSelection):
        nonlocal client
        if result.key == "Account":
            while True:
                choices = (
                    list_account_choices_fn()
                    if list_account_choices_fn is not None
                    else []
                )
                picked = show_lichess_account_picker(menu_manager, choices)
                if is_break_result(picked):
                    return picked
                if picked is None:
                    return None
                if picked == ACCOUNTS_MENU_KEY:
                    handle_accounts_menu_fn()
                    refresh_client()
                    continue
                if bind_account_fn is None:
                    return None
                bind_account_fn(picked)
                refresh_client()
                return None
        if result.key == "NewGame":
            config = LichessConfig(mode=LichessGameMode.NEW)
            if start_game(config):
                return result
            return None
        if result.key == "Ongoing":
            show_lichess_help(menu_manager, "Ongoing Games", ONGOING_GAMES_HELP)
            game_id = show_lichess_ongoing_games(client, menu_manager, log)
            if game_id:
                config = LichessConfig(mode=LichessGameMode.ONGOING, game_id=game_id)
                if start_game(config):
                    return result
            return None
        if result.key == "Challenges":
            show_lichess_help(menu_manager, "Challenges", CHALLENGES_HELP)
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
        return None

    result = menu_manager.run_menu_loop(
        lambda: build_lichess_menu_entries(username),
        handle_selection,
    )

    if is_break_result(result):
        return result
    if game_started:
        # Same token as Players → Start Game. The lobby only stashes the join;
        # Settings starts the game after nested menus have exited, so they cannot
        # redraw player rows over the board.
        return "START_GAME"
    return None

