"""Lichess service wrappers to orchestrate client, menus, and game start."""

import copy
import re
from typing import Optional, Callable

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.i18n import t
from universalchess.managers.menu import is_break_result, is_play_start

from .match import epaper_is_flipped, has_lichess_slot, lichess_account_id

_MISSING_SCOPE = re.compile(r"Missing scope:\s*([a-z0-9:_-]+)", re.IGNORECASE)


def _row(node_id: str, key: str) -> str:
    """Return a catalog string for a lobby row in the device's language.

    The lobby is hand-built (its rows carry live state: the signed-in user, the
    Rated value, lists fetched on selection), but its wording is the same
    wording the web card renders from these nodes. Reading them here keeps one
    source for both surfaces and one place to translate -- the board used to
    hold English copies of both the labels and the help, so a translated board
    showed an English lobby.
    """
    from universalchess.menus.catalog.loader import get_catalog

    return get_catalog().get_node(node_id)[key]


def _lichess_permission_panel_message(error_msg: str, fallback: str) -> Optional[str]:
    """E-paper copy for a missing OAuth scope or HTTP 401/403, else None.

    Listing challenges with ``board:play`` but without ``challenge:read`` is
    HTTP 403 ``Missing scope: challenge:read``, not 401. Mapping only 401
    showed a truncated HTTP dump instead of naming the scope to add.
    """
    match = _MISSING_SCOPE.search(error_msg)
    if match:
        return t("lichess.error.token_scope", scope=match.group(1))
    lowered = error_msg.lower()
    if (
        "401" in error_msg
        or "403" in error_msg
        or "unauthorized" in lowered
        or "forbidden" in lowered
    ):
        return fallback
    return None


def get_lichess_connection(token, log, host_id: str):
    """Authenticate a stored token for a lobby view, with error classification.

    Returns ``(connection, username, error)``. The connection owns an HTTP
    session and its pooled socket, so the caller closes it when the view it
    feeds is done. A failure closes it here and returns None in its place, so no
    caller has to unwind a connection it was never given. ``host_id`` is the
    credential's stored host; it is not assumed to be org.
    """
    from .hosts import HOST_BY_ID

    if not token or token == "tokenhere":  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret
        log.warning("[Lichess] No valid API token configured")
        return None, None, "no_token"
    if host_id not in HOST_BY_ID:
        log.warning("[Lichess] No Lichess host on the stored credential")
        return None, None, "unknown_host"
    connection = None
    try:
        from .match import create_lichess_connection

        connection = create_lichess_connection(token, host_id=host_id)
        user_info = connection.client.account.get()
        username = user_info.get("username", "")
        log.info(f"[Lichess] Authenticated as: {username}")
        return connection, username, None
    except ImportError:
        log.error("[Lichess] berserk library not installed")
        error = "no_berserk"
    except Exception as e:
        log.error(f"[Lichess] Failed to connect to Lichess: {e}")
        error = "network"
    if connection is not None:
        connection.close()
    return None, None, error


def resolve_lichess_identity(token, log=None, host_id: str = ""):
    """Authenticate an explicit Lichess token and return its account identity.

    Unlike :func:`get_lichess_connection`, which uses a stored token, this
    verifies a token supplied for a *new* credential (before it is saved) so
    the plugin can key it as ``org:alice``. ``host_id`` is the server the
    caller chose; it is not assumed to be org. Returns a
    :class:`account_store.ResolvedIdentity`.

    The connection is closed on every path: this asks one question and keeps
    nothing, so holding its socket open past the answer serves no one.
    """
    from universalchess.services.account_store import ResolvedIdentity
    from .hosts import HOST_BY_ID

    if not token or token == "tokenhere":  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret
        return ResolvedIdentity(error="no_token", message=t("lichess.identity.no_token"))
    if host_id not in HOST_BY_ID:
        return ResolvedIdentity(
            error="unknown_host",
            message=t("accounts.unknown_host", host=host_id or ""),
        )
    connection = None
    try:
        from .match import create_lichess_connection

        connection = create_lichess_connection(token, host_id=host_id)
        username = connection.client.account.get().get("username", "")
        if not username:
            return ResolvedIdentity(error="auth_failed", message=t("lichess.identity.unreadable"))
        return ResolvedIdentity(identity=username)
    except ImportError:
        if log:
            log.error("[Lichess] berserk library not installed")
        return ResolvedIdentity(error="no_berserk", message=t("lichess.identity.no_berserk"))
    except Exception as e:
        if log:
            log.error(f"[Lichess] Token verification failed: {e}")
        return ResolvedIdentity(error="auth_failed", message=t("lichess.identity.unverified"))
    finally:
        if connection is not None:
            connection.close()


DEFAULT_ACCOUNT_MENU_KEY = "Default"
ACCOUNTS_MENU_KEY = "Accounts"

def build_lichess_menu_entries(
    username: Optional[str], rated: bool = False, clock: str = "", color: str = ""
):
    """Build Lichess Settings rows (the lobby, not a nested Play page).

    Account is first and selectable: it opens the account picker. Rated follows
    it, because it decides what the account's rating is exposed to and applies
    to every seek this board posts -- a player slot could not hold it, since a
    lobby seek runs from a pairing no saved slot describes. Clock sits under
    Rated for the same reason: the Board API accepts only Rapid, Classical, and
    correspondence, so the Game clock (which still offers Blitz) is not sent.
    Color sits under Clock: White, Black, or Random for the seeking account,
    which is the side the human plays after remap. The Players colour control
    still swaps sides for engine games and is not sent. Ongoing Games and
    Challenges are always listed; selecting either shows how it works, then the
    live list. Seek New Game is last. Add or delete logins is Accounts on the
    picker, not a lobby sibling.

    That last row says Seek because it always posts a seek, whatever the Players
    slots are set to, unlike the New Game elsewhere that starts whichever game
    those slots describe.

    Labels and help come from the catalog nodes the web card renders, so the row
    reads the same on both surfaces and in the device's language.
    """
    from universalchess.menus.catalog.loader import get_catalog
    from .match import DEFAULT_LICHESS_CLOCK, DEFAULT_LICHESS_COLOR

    account = username or t("common.unknown")
    clock_key = clock or DEFAULT_LICHESS_CLOCK
    clock_label = get_catalog().option_label("lichess_clock", clock_key)
    color_key = color or DEFAULT_LICHESS_COLOR
    color_label = get_catalog().option_label("lichess_color", color_key)
    return [
        IconMenuEntry(
            key="Account",
            label=f"{_row('lichess.account', 'boardLabel')}\n{account}",
            icon_name="lichess",
        ),
        IconMenuEntry(
            key="Rated",
            label=(
                f"{_row('field.lichess.rated', 'boardLabel')}\n"
                f"{t('common.on') if rated else t('common.off')}"
            ),
            icon_name="checkbox_checked" if rated else "checkbox_empty",
            help=_row("field.lichess.rated", "help"),
        ),
        IconMenuEntry(
            key="Clock",
            label=f"{_row('field.lichess.clock', 'boardLabel')}\n{clock_label}",
            icon_name="timer" if clock_key == "none" else "timer_checked",
            help=_row("field.lichess.clock", "help"),
        ),
        IconMenuEntry(
            key="Color",
            label=f"{_row('field.lichess.color', 'boardLabel')}\n{color_label}",
            icon_name=_lichess_color_icon(color_key),
            help=_row("field.lichess.color", "help"),
        ),
        IconMenuEntry(
            key="Ongoing",
            label=_row("lichess.ongoing", "boardLabel"),
            icon_name="lichess",
            help=_row("lichess.ongoing", "help"),
        ),
        IconMenuEntry(
            key="Challenges",
            label=_row("lichess.challenges", "boardLabel"),
            icon_name="lichess",
            help=_row("lichess.challenges", "help"),
        ),
        IconMenuEntry(
            key="NewGame",
            label=_row("lichess.new_game", "boardLabel"),
            icon_name="play",
        ),
    ]


_LICHESS_COLOR_ICONS = {
    "white": "white_piece",
    "black": "black_piece",
    "random": "random",
}


def _lichess_color_icon(color_key: str) -> str:
    """King icon for White/Black, dice for Random."""
    from .match import DEFAULT_LICHESS_COLOR

    return _LICHESS_COLOR_ICONS.get(color_key, _LICHESS_COLOR_ICONS[DEFAULT_LICHESS_COLOR])


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
            label=_row("players.accounts", "label"),
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


def build_lichess_clock_picker_entries(selected: str):
    """Radio rows for the lobby Clock picker.

    Choices come from the catalog ``lichess_clock`` set, which is only the
    Board API clocks plus None for correspondence. Blitz is not listed.
    """
    from universalchess.menus.catalog.loader import get_catalog
    from .match import DEFAULT_LICHESS_CLOCK

    current = selected or DEFAULT_LICHESS_CLOCK
    entries = []
    for option in get_catalog().option_set("lichess_clock"):
        value = str(option["value"])
        entries.append(
            IconMenuEntry(
                key=value,
                label=option["label"],
                icon_name="timer" if value == "none" else "timer_checked",
                trailing_icon_name="radio_checked" if value == current else "radio_empty",
            )
        )
    return entries


def show_lichess_clock_picker(menu_manager, selected: str):
    """Show the clock picker and return the chosen key, or None on BACK.

    Break results are returned unchanged so Play can unwind.
    """
    entries = build_lichess_clock_picker_entries(selected)
    selected_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.trailing_icon_name == "radio_checked"
        ),
        0,
    )
    result = menu_manager.show_menu(entries, initial_index=selected_index)
    if result.is_break:
        return result
    if result.is_exit():
        return None
    return result.key


def build_lichess_color_picker_entries(selected: str):
    """Radio rows for the lobby Color picker.

    Choices come from the catalog ``lichess_color`` set: Random, White, Black.
    """
    from universalchess.menus.catalog.loader import get_catalog
    from .match import DEFAULT_LICHESS_COLOR

    current = selected or DEFAULT_LICHESS_COLOR
    entries = []
    for option in get_catalog().option_set("lichess_color"):
        value = str(option["value"])
        entries.append(
            IconMenuEntry(
                key=value,
                label=option["label"],
                icon_name=_lichess_color_icon(value),
                trailing_icon_name="radio_checked" if value == current else "radio_empty",
            )
        )
    return entries


def show_lichess_color_picker(menu_manager, selected: str):
    """Show the color picker and return the chosen key, or None on BACK.

    Break results are returned unchanged so Play can unwind.
    """
    entries = build_lichess_color_picker_entries(selected)
    selected_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.trailing_icon_name == "radio_checked"
        ),
        0,
    )
    result = menu_manager.show_menu(entries, initial_index=selected_index)
    if result.is_break:
        return result
    if result.is_exit():
        return None
    return result.key


def lichess_waiting_message(mode, seek=None, *, awaiting_opponent: bool = False) -> str:
    """Copy shown on the panel while a Lichess game is being found or joined."""
    from .match import lichess_waiting_message as _waiting

    return _waiting(mode, seek=seek, awaiting_opponent=awaiting_opponent)


def lichess_cancelling_message() -> str:
    """Copy shown after BACK while the seek is torn down."""
    from .match import lichess_cancelling_message as _cancelling

    return _cancelling()


def show_lichess_waiting_splash(
    panel_manager, mode, seek=None, *, awaiting_opponent: bool = False
) -> bool:
    """Paint the Lichess waiting splash and wait until it reaches the e-paper.

    Uses :func:`show_fullscreen_splash` so the frame is on the panel before the
    caller continues. A plain ``add_widget`` without waiting lost the race
    against ``DisplayManager._init_widgets`` (which ``clear_widgets``), so the
    seek wait showed an empty chess board instead of this message.
    """
    from universalchess.epaper.splash_screen import show_fullscreen_splash

    return show_fullscreen_splash(
        panel_manager,
        lichess_waiting_message(mode, seek=seek, awaiting_opponent=awaiting_opponent),
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


_NEXT_GAME_PROMPT_KEYS = {
    "ABORTED": "lichess.unfinished.aborted",
    "NOSTART": "lichess.unfinished.nostart",
    "RESIGN": "lichess.unfinished.resign",
    "CHECKMATE": "lichess.unfinished.checkmate",
    "TIMEOUT": "lichess.unfinished.timeout",
    "TIME_FORFEIT": "lichess.unfinished.timeout",
    "DRAW": "lichess.unfinished.draw",
    "STALEMATE": "lichess.unfinished.stalemate",
}


def lichess_next_game_prompt_key(reason: Optional[str] = None) -> str:
    """i18n key for the next-game menu header.

    Board-reset asks whether to seek, because the user put the pieces back.
    A remote end must name why the game stopped; reusing the reset prompt left
    the header as "Seek a new game?" after the opponent walked away, and
    resign never opened this menu at all.
    """
    if not reason:
        return "lichess.reset.prompt"
    return _NEXT_GAME_PROMPT_KEYS.get(reason, "lichess.unfinished.ended")


def choose_lichess_reset_action(menu_manager, *, reason: Optional[str] = None) -> str:
    """Ask what to do next: ``lobby``, ``seek`` or ``cancel``.

    Used when a Lichess game ended without an explicit PLAY / Seek New Game
    (board-reset to the start, or the opponent aborted). Seeking is only one
    of the things wanted -- an ongoing game, a challenge, a different account
    or a rated change are all in the lobby -- so the lobby is offered first.
    Lobby is highlighted so a stray TICK cannot register a seek. BACK (or any
    key that is not a row) is the refusal; a Cancel row duplicated that.
    ``reason`` selects the header: resign names that the opponent resigned;
    omitted is board-reset.
    """
    entries = [
        IconMenuEntry(
            key="prompt",
            label=t(lichess_next_game_prompt_key(reason)),
            icon_name="lichess",
            enabled=True,
            selectable=False,
            font_size=12,
        ),
        IconMenuEntry(
            key="Lobby",
            label=_row("players.lichess", "boardLabel"),
            icon_name="lichess",
            enabled=True,
        ),
        IconMenuEntry(
            key="Seek",
            label=_row("lichess.new_game", "boardLabel"),
            icon_name="play",
            enabled=True,
        ),
    ]
    result = menu_manager.show_menu(entries, initial_index=1)
    key = result.key if hasattr(result, "key") else result
    if key == "Lobby":
        return "lobby"
    if key == "Seek":
        return "seek"
    return "cancel"


def board_reset_rebuild_action(
    menu_manager, *, is_lichess: bool, reason: Optional[str] = None
) -> str:
    """Decide what a Lichess next-game prompt does.

    Setting the pieces back to the start, or a remote abort, rebuilds through
    ``_start_game_mode``, which posts a new seek. Those paths are not PLAY,
    lobby Seek New Game, or web New Game, so they ask first. BACK (or no menu)
    returns ``menu`` so the caller leaves the game without seeking, ``lobby``
    asks for the Lichess lobby instead, and ``seek`` means the caller stashes an
    explicit NEW join. Engine/human rebuilds return ``rebuild`` without a prompt.
    ``reason`` is the remote termination (``RESIGN``, ``ABORTED``, ...) when
    the opponent ended the game.
    """
    if not is_lichess:
        return "rebuild"
    if menu_manager is None:
        return "menu"
    choice = choose_lichess_reset_action(menu_manager, reason=reason)
    if choice == "cancel":
        return "menu"
    return choice


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


def effective_lichess_players(player1, player2, *, lobby_start: bool):
    """Player slots a start runs with, substituting a pairing for a lobby start.

    The Lichess lobby's buttons are an explicit request for a Lichess game, so
    they must not depend on the Players slots naming one: with no Lichess slot,
    Seek New Game used to build a local game from settings and post no seek at
    all. A lobby start therefore runs Human vs Lichess whatever the slots say.

    The human is kept wherever it already sits, so the player does not change
    sides; with no human at all, slot 1 takes it, because White stays on player
    1's physical side of the board. Which account the substituted slot plays as
    is not its business: the lobby holds that (:func:`lichess_account_id`).

    Copies are returned: these objects are the live settings centaur.ini is
    written from, and the pairing lasts for this game only.
    """
    if not lobby_start or has_lichess_slot(player1, player2):
        return (player1, player2)
    slot1_is_human = getattr(player1, "type", "") == "human"
    slot2_is_human = getattr(player2, "type", "") == "human"
    if slot2_is_human and not slot1_is_human:
        return (_as_lichess_slot(player1), player2)
    if slot1_is_human:
        return (player1, _as_lichess_slot(player2))
    return (_as_human_slot(player1), _as_lichess_slot(player2))


def _as_lichess_slot(player):
    """Copy of a slot switched to Lichess."""
    substitute = copy.copy(player)
    substitute.type = "lichess"
    return substitute


def _as_human_slot(player):
    """Copy of a slot switched to human."""
    substitute = copy.copy(player)
    substitute.type = "human"
    return substitute


def active_lichess_account(settings):
    """Bound (or default) Lichess credential, or None."""
    from .accounts import (
        default_lichess_credential,
        get_lichess_credential,
    )

    account_id = lichess_account_id(settings)
    if account_id:
        return get_lichess_credential(account_id)
    return default_lichess_credential()


def lichess_connection_from_settings(settings, log):
    """Authenticate the bound credential for lobby Ongoing/Challenges lists.

    Returns ``(connection, username, error)``; the caller closes the connection.
    """
    from .player import LichessPlayer, LichessPlayerConfig

    player = LichessPlayer(LichessPlayerConfig(account_id=lichess_account_id(settings)))
    token, _range = player._resolve_account()
    host_id = getattr(player, "_host_id", "")
    return get_lichess_connection(token, log, host_id=host_id)


def ongoing_game_summaries(raw_games) -> list:
    """Normalize ``GET /api/account/playing`` rows for board and web lobbies.

    Each item is ``id``, ``opponent``, ``rating``, ``color`` (``white``/``black``),
    ``fen``, ``lastMove``, and ``isMyTurn``. ``fen`` is the position to set up
    before Join; a missing FEN is an empty string so the row is still joinable.
    Rows without a game id are dropped so a truncated payload cannot be joined.
    """
    from .player import (
        lichess_player_display_name,
        lichess_side_is_white,
        ongoing_game_id,
    )

    rows = []
    for game in raw_games or []:
        game_id = ongoing_game_id(game)
        if not game_id:
            continue
        opponent = game.get("opponent") or {}
        rating = opponent.get("rating")
        last_move = game.get("lastMove") or game.get("last_move") or ""
        if "isMyTurn" in game:
            is_my_turn = bool(game.get("isMyTurn"))
        else:
            is_my_turn = bool(game.get("is_my_turn"))
        rows.append(
            {
                "id": game_id,
                "opponent": lichess_player_display_name(opponent) or "Unknown",
                "rating": "" if rating is None else rating,
                "color": "white" if lichess_side_is_white(game.get("color")) else "black",
                "fen": str(game.get("fen") or ""),
                "lastMove": str(last_move),
                "isMyTurn": is_my_turn,
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
    from .player import lichess_player_display_name

    return {
        "id": str(challenge_id),
        "direction": direction,
        "name": lichess_player_display_name(person) or "Unknown",
        "rating": "" if rating is None else rating,
    }


def _ongoing_row_label(row: dict) -> str:
    """Opponent, rating, and color for one nowPlaying row."""
    color = (
        t("chess.color.white_initial")
        if row.get("color") == "white"
        else t("chess.color.black_initial")
    )
    return f"{row['opponent']}\n({row['rating']}) {color}"


def _try_ongoing_summaries(client, log) -> list:
    """nowPlaying rows, or empty when the client cannot list them.

    A missing ``games.get_ongoing`` (tests that stub a bare client) must not
    block a seek: there is nothing to resume.
    """
    try:
        raw = client.games.get_ongoing(count=10)
    except Exception as e:
        if log is not None:
            log.warning(f"[Lichess] Could not list ongoing games: {e}")
        return []
    return ongoing_game_summaries(raw)


def _ongoing_board_preview(row: dict, player1_color: str = "white"):
    """128x128 1-bit diagram of ``row['fen']``, or ``(None, None)``.

    A throwaway ``ChessGameState`` is used so the live game's FEN is not
    rewritten while the lobby is on screen. The diagram is drawn from the
    e-paper end named by Player 1 Color, not the colour this account plays
    in the game: that is who sits where, and flipping from the game colour
    inverted a board already set up as Black.
    """
    fen = str((row or {}).get("fen") or "").strip()
    placement = fen.split()[0] if fen else ""
    if not placement:
        return None, None
    if len(fen.split()) < 4:
        fen = f"{placement} w - - 0 1"
    try:
        from PIL import Image

        from universalchess.epaper.chess_board import ChessBoardWidget
        from universalchess.state.chess_game import ChessGameState

        state = ChessGameState()
        state.set_position(fen)
        flip = epaper_is_flipped(player1_color)
        widget = ChessBoardWidget(0, 0, lambda *_a, **_k: None, state, flip)
        try:
            image = Image.new("1", (128, 128), 1)
            widget.render(image)
            mask = Image.new("1", (128, 128), 1)
            return image, mask
        finally:
            widget.cleanup()
    except Exception:
        return None, None


def ongoing_position_confirm_entries(row: dict, board_image=None, board_mask=None):
    """Diagram (non-selectable) then Join, so the pieces can be set up first."""
    return [
        IconMenuEntry(
            key="position",
            label=_ongoing_row_label(row),
            icon_name="lichess",
            selectable=False,
            height_ratio=3.0,
            layout="vertical",
            icon_size=128 if board_image is not None else None,
            icon_image=board_image,
            icon_mask=board_mask,
            font_size=12,
        ),
        IconMenuEntry(
            key="Join",
            label=t("lichess.ongoing.join"),
            icon_name="play",
        ),
    ]


def confirm_ongoing_game(menu_manager, row: dict, player1_color: str = "white") -> bool:
    """Show the position and return True only if Join is chosen."""
    board_image, board_mask = _ongoing_board_preview(row, player1_color)
    entries = ongoing_position_confirm_entries(
        row, board_image=board_image, board_mask=board_mask
    )
    result = menu_manager.show_menu(entries, initial_index=1)
    key = result.key if hasattr(result, "key") else result
    return key == "Join"


def ongoing_or_seek_menu_entries(summaries) -> list:
    """Prompt, nowPlaying rows, then Seek New Game."""
    entries = [
        IconMenuEntry(
            key="prompt",
            label=t("lichess.ongoing_or_seek.prompt"),
            icon_name="lichess",
            selectable=False,
            font_size=12,
        )
    ]
    for row in summaries:
        entries.append(
            IconMenuEntry(
                key=row["id"],
                label=_ongoing_row_label(row),
                icon_name="lichess",
                font_size=12,
            )
        )
    entries.append(
        IconMenuEntry(
            key="Seek",
            label=_row("lichess.new_game", "boardLabel"),
            icon_name="play",
        )
    )
    return entries


def choose_ongoing_or_seek(menu_manager, summaries, player1_color: str = "white") -> str:
    """``seek``, ``cancel``, or a nowPlaying game id after the position confirm.

    PLAY uses this picker. Empty ``summaries`` seeks without a menu: there
    is nothing to resume. BACK on the picker or on the diagram is ``cancel``.
    Choosing a game still requires Join on the diagram so the pieces can be
    set up first.
    """
    if not summaries:
        return "seek"
    while True:
        result = menu_manager.show_menu(
            ongoing_or_seek_menu_entries(summaries), initial_index=1
        )
        key = result.key if hasattr(result, "key") else result
        if getattr(result, "is_break", False) or key in ("BACK", "SHUTDOWN"):
            return "cancel"
        if key == "Seek":
            return "seek"
        row = next((item for item in summaries if item["id"] == key), None)
        if row is None:
            return "cancel"
        if confirm_ongoing_game(menu_manager, row, player1_color):
            return row["id"]


def _start_seek_or_resume(
    connection, menu_manager, start_game, log, player1_color: str = "white"
) -> bool:
    """PLAY in the lobby: join a chosen nowPlaying game, or seek.

    Ongoing Games and Seek New Game are explicit. PLAY is the mixed
    choice, so unfinished games are offered (with the position) first.
    """
    from .player import LichessPlayerConfig as LichessConfig, LichessGameMode

    summaries = _try_ongoing_summaries(connection.client, log)
    choice = choose_ongoing_or_seek(menu_manager, summaries, player1_color)
    if choice == "cancel":
        return False
    if choice == "seek":
        return bool(start_game(LichessConfig(mode=LichessGameMode.NEW)))
    return bool(
        start_game(LichessConfig(mode=LichessGameMode.ONGOING, game_id=choice))
    )


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


def show_lichess_ongoing_games(
    client, menu_manager, log, player1_color: str = "white"
) -> Optional[str]:
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

        entries = [
            IconMenuEntry(
                key=row["id"],
                label=_ongoing_row_label(row),
                icon_name="lichess",
                enabled=True,
                font_size=12,
            )
            for row in summaries
        ]

        while True:
            result = menu_manager.show_menu(entries)

            if result.is_break or result.key == "BACK":
                return None

            row = next((item for item in summaries if item["id"] == result.key), None)
            if row is None:
                return None
            if confirm_ongoing_game(menu_manager, row, player1_color):
                return row["id"]

    except AttributeError as e:
        log.error(f"[Lichess] berserk API method not found: {e}")
        show_lichess_error(menu_manager, "API Not Supported", t("lichess.error.ongoing_api"))
        return None
    except Exception as e:
        error_msg = str(e)
        log.error(f"[Lichess] Error fetching ongoing games: {e}")
        permission = _lichess_permission_panel_message(
            error_msg, t("lichess.error.board_scope")
        )
        if permission:
            show_lichess_error(menu_manager, "Auth Error", permission)
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            show_lichess_error(menu_manager, "Network Error", t("lichess.error.network"))
        else:
            short_error = error_msg[:40] + "..." if len(error_msg) > 40 else error_msg
            show_lichess_error(
                menu_manager, "Error", t("lichess.error.games_failed", error=short_error)
            )
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
                menu_manager, "API Not Supported", t("lichess.error.challenges_api")
            )
            return None

        incoming = list(challenges_data.get("in", []))
        outgoing = list(challenges_data.get("out", []))
        summaries = challenge_summaries({"in": incoming, "out": outgoing})

        if not summaries:
            return None

        entries = []
        for row in summaries:
            prefix = (
                t("lichess.challenge.incoming")
                if row["direction"] == "in"
                else t("lichess.challenge.outgoing")
            )
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
        show_lichess_error(menu_manager, "API Not Supported", t("lichess.error.challenges_api"))
        return None
    except Exception as e:
        error_msg = str(e)
        log.error(f"[Lichess] Error fetching challenges: {e}")
        permission = _lichess_permission_panel_message(
            error_msg, t("lichess.error.challenge_scope")
        )
        if permission:
            show_lichess_error(menu_manager, "Auth Error", permission)
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            show_lichess_error(menu_manager, "Network Error", t("lichess.error.network"))
        else:
            short_error = error_msg[:40] + "..." if len(error_msg) > 40 else error_msg
            show_lichess_error(
                menu_manager, "Error", t("lichess.error.challenges_failed", error=short_error)
            )
        return None


def handle_lichess_menu(
    get_lichess_connection_fn: Callable,
    menu_manager,
    start_lichess_game_fn: Callable,
    handle_accounts_menu_fn: Callable,
    log,
    list_account_choices_fn: Optional[Callable] = None,
    bind_account_fn: Optional[Callable] = None,
    rated_fn: Optional[Callable] = None,
    set_rated_fn: Optional[Callable] = None,
    clock_fn: Optional[Callable] = None,
    set_clock_fn: Optional[Callable] = None,
    color_fn: Optional[Callable] = None,
    set_color_fn: Optional[Callable] = None,
    player1_color_fn: Optional[Callable] = None,
):
    """Handle Lichess Settings: Account, Ongoing, Challenges, New Game.

    Owns the connection it is given for as long as the menu is on screen, and
    closes it on the way out -- including the one an account switch replaces. A
    game started from here does not use it; the player opens its own.

    Args:
        get_lichess_connection_fn: Callback for a (connection, username, error)
            tuple, where the connection carries the berserk client
        menu_manager: MenuManager instance
        start_lichess_game_fn: Callback to start a Lichess game with config
        handle_accounts_menu_fn: Callback to show accounts menu (picker Accounts)
        log: Logger instance
        list_account_choices_fn: ``() -> [(key, label, selected), ...]`` for the
            Account picker. Optional so start-only tests can omit it.
        bind_account_fn: ``(account_id) -> None``; ``""`` means Default.
        rated_fn: ``() -> bool``, the stored Rated setting. Read on every
            redraw so the row shows what the next seek will be.
        set_rated_fn: ``(bool) -> None``, persists a Rated change.
        clock_fn: ``() -> str``, the stored lobby clock key. Read on every redraw.
        set_clock_fn: ``(str) -> None``, persists a Clock pick.
        color_fn: ``() -> str``, the stored lobby color key. Read on every redraw.
        set_color_fn: ``(str) -> None``, persists a Color pick.
        player1_color_fn: ``() -> str``, Players → Player 1 Color. The ongoing
            diagram is drawn from that end of the board.

    Returns:
        ``"START_GAME"`` if a Lichess game was requested (join stashed; Settings
        starts it after menus unwind), a break result if a break action, or
        None otherwise.
    """
    from .player import LichessPlayerConfig as LichessConfig, LichessGameMode
    from universalchess.managers.menu import MenuSelection

    def _player1_color() -> str:
        if player1_color_fn is None:
            return "white"
        return str(player1_color_fn() or "white")

    connection, username, error = get_lichess_connection_fn()
    if connection is None:
        if error == "no_token":
            result = show_lichess_error(
                menu_manager, "No API Token", t("lichess.error.no_token"), True
            )
        elif error == "invalid_token":
            result = show_lichess_error(
                menu_manager, "Invalid Token", t("lichess.error.invalid_token"), True
            )
        elif error == "no_berserk":
            show_lichess_error(menu_manager, "Missing Library", t("lichess.error.no_berserk"))
            result = None
        else:
            show_lichess_error(
                menu_manager, "Connection Error", t("lichess.error.unreachable")
            )
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
            show_lichess_error(menu_manager, "Start Failed", t("lichess.error.start_failed"))
        return False

    def refresh_client() -> None:
        """Re-authenticate after an account bind so Account and lobby lists match.

        The connection being replaced is closed: it authenticated the account the
        user just switched away from, and nothing will ask it anything again.
        A failed switch keeps the working connection rather than closing it.
        """
        nonlocal connection, username
        new_connection, new_username, new_error = get_lichess_connection_fn()
        if new_connection is None:
            log.warning(f"[Lichess] Account switch failed: {new_error}")
            show_lichess_error(
                menu_manager, "Account", t("lichess.error.account_switch")
            )
            return
        connection.close()
        connection = new_connection
        username = new_username

    def _rated() -> bool:
        """Stored Rated setting; False when the caller wired no reader."""
        return bool(rated_fn()) if rated_fn is not None else False

    def _clock() -> str:
        """Stored lobby clock key; Rapid 10+0 when the caller wired no reader."""
        from .match import DEFAULT_LICHESS_CLOCK

        if clock_fn is None:
            return DEFAULT_LICHESS_CLOCK
        return str(clock_fn() or "") or DEFAULT_LICHESS_CLOCK

    def _color() -> str:
        """Stored lobby color key; Random when the caller wired no reader."""
        from .match import DEFAULT_LICHESS_COLOR

        if color_fn is None:
            return DEFAULT_LICHESS_COLOR
        return str(color_fn() or "") or DEFAULT_LICHESS_COLOR

    def handle_selection(result: MenuSelection):
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
        if result.key == "Rated":
            # Returning None keeps the loop, which rebuilds the rows through
            # rated_fn, so the checkbox shows the value that was just written.
            if set_rated_fn is not None:
                set_rated_fn(not _rated())
            return None
        if result.key == "Clock":
            if set_clock_fn is None:
                return None
            picked = show_lichess_clock_picker(menu_manager, _clock())
            if is_break_result(picked):
                return picked
            if picked is not None:
                set_clock_fn(picked)
            return None
        if result.key == "Color":
            if set_color_fn is None:
                return None
            picked = show_lichess_color_picker(menu_manager, _color())
            if is_break_result(picked):
                return picked
            if picked is not None:
                set_color_fn(picked)
            return None
        if result.key == "NewGame":
            if start_game(LichessConfig(mode=LichessGameMode.NEW)):
                return result
            return None
        if result.key == "Ongoing":
            show_lichess_help(
                menu_manager,
                _row("lichess.ongoing", "label"),
                _row("lichess.ongoing", "help"),
            )
            game_id = show_lichess_ongoing_games(
                connection.client, menu_manager, log, player1_color=_player1_color()
            )
            if game_id:
                config = LichessConfig(mode=LichessGameMode.ONGOING, game_id=game_id)
                if start_game(config):
                    return result
            return None
        if result.key == "Challenges":
            show_lichess_help(
                menu_manager,
                _row("lichess.challenges", "label"),
                _row("lichess.challenges", "help"),
            )
            challenge = show_lichess_challenges(connection.client, menu_manager, log)
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

    try:
        result = menu_manager.run_menu_loop(
            lambda: build_lichess_menu_entries(
                username, _rated(), _clock(), _color()
            ),
            handle_selection,
        )
        if is_play_start(result):
            # PLAY is the mixed start: unfinished games first (with the
            # position), or a new seek. A failed start still returns the
            # break, because PLAY is also how the user leaves. This must run
            # before the connection is closed: the picker lists nowPlaying.
            if _start_seek_or_resume(
                connection, menu_manager, start_game, log, player1_color=_player1_color()
            ):
                return "START_GAME"
    finally:
        # Leaving the menu ends the only thing this connection existed for. What
        # follows -- including a game start -- never asks it anything: the player
        # opens its own connection once the menus unwind.
        connection.close()

    if is_play_start(result):
        return result
    if is_break_result(result):
        return result
    if game_started:
        # Same token as Players → Start Game. The lobby only stashes the join;
        # Settings starts the game after nested menus have exited, so they cannot
        # redraw player rows over the board.
        return "START_GAME"
    return None

