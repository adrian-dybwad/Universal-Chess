"""Accounts (Lichess token) menu helper.

The Accounts menu shows which Lichess account the stored token belongs to so a
user with more than one account can tell them apart. The account name is shown
above the masked token. The name is resolved two ways that complement each other:

  * ``get_lichess_username`` returns the name cached in config (populated on the
    last successful authentication). It is cheap and used for the instant first
    paint, even offline.
  * ``fetch_lichess_username`` performs a live network lookup for the current
    token. It runs on a background worker so it never blocks the e-paper UI, and
    when it lands it refreshes the menu so the freshly resolved name replaces the
    cached one.

The Lichess row opens a submenu to edit or delete the token. Deletion mirrors the
AI-agent "clear saved key" action: it is only offered when a token exists and it
confirms before removing the credential.
"""

import threading
from typing import Callable, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result


def mask_token(token: str) -> str:
    """Mask a token for display."""
    if not token:
        return "Not set"
    if len(token) <= 8:
        return token[:2] + "..." + token[-2:] if len(token) > 4 else "****"
    return token[:6] + "..." + token[-4:]


def lichess_row_detail(username: Optional[str], token: str) -> str:
    """Build the detail lines shown under the "Lichess" heading.

    Shows the account name above the masked token when the name is known so
    multiple accounts are distinguishable; falls back to the masked token alone
    (or "Not set") so behaviour degrades gracefully when the name is unknown.
    """
    if not token:
        return "Not set"
    masked = mask_token(token)
    if username:
        return f"{username}\n{masked}"
    return masked


def _default_run_in_background(worker: Callable[[], None]) -> None:
    """Run ``worker`` on a daemon thread (injection seam for tests)."""
    threading.Thread(target=worker, name="lichess-username-lookup", daemon=True).start()


def confirm_delete_token(menu_manager) -> bool:
    """Show a Delete/Cancel confirmation and return True only if Delete is chosen.

    Defaults the highlight to Cancel so a stray confirmation press cannot delete
    the credential; any non-Delete outcome (Cancel, BACK, break) is treated as a
    refusal, mirroring the confirm step the web app uses before clearing an
    agent's API key.
    """
    entries = [
        IconMenuEntry(
            key="prompt",
            label="Delete\nLichess token?",
            icon_name="cancel",
            enabled=True,
            selectable=False,
            font_size=12,
        ),
        IconMenuEntry(key="Delete", label="Delete", icon_name="cancel", enabled=True),
        IconMenuEntry(key="Cancel", label="Cancel", icon_name="undo", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=2)
    key = result.key if hasattr(result, "key") else result
    return key == "Delete"


def handle_lichess_account_menu(
    menu_manager,
    get_lichess_api: Callable[[], str],
    handle_lichess_token_fn: Callable[[], MenuSelection],
    delete_lichess_token_fn: Optional[Callable[[], None]] = None,
) -> Optional[MenuSelection]:
    """Submenu to edit or delete the stored Lichess token.

    "Edit Token" opens the on-screen keyboard (``handle_lichess_token_fn``);
    "Delete Token" is offered only when a token is stored and confirms before
    calling ``delete_lichess_token_fn``. Both actions loop back so the submenu
    re-renders with the updated state; the user presses BACK to return.
    """

    def build_entries():
        token = get_lichess_api()
        entries = [
            IconMenuEntry(
                key="Edit",
                label="Edit Token" if token else "Set Token",
                icon_name="lichess",
                enabled=True,
            ),
        ]
        if token and delete_lichess_token_fn is not None:
            entries.append(
                IconMenuEntry(
                    key="Delete",
                    label="Delete Token",
                    icon_name="cancel",
                    enabled=True,
                )
            )
        return entries

    def handle_selection(result: MenuSelection):
        if result.key == "Edit":
            sub_result = handle_lichess_token_fn()
            if is_break_result(sub_result):
                return sub_result
            return None
        if result.key == "Delete":
            if confirm_delete_token(menu_manager):
                delete_lichess_token_fn()
            return None
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection)


def handle_accounts_menu(
    menu_manager,
    get_lichess_api: Callable[[], str],
    handle_lichess_token_fn: Callable[[], MenuSelection],
    get_lichess_username: Optional[Callable[[], Optional[str]]] = None,
    fetch_lichess_username: Optional[Callable[[], Optional[str]]] = None,
    delete_lichess_token_fn: Optional[Callable[[], None]] = None,
    run_in_background: Optional[Callable[[Callable[[], None]], None]] = None,
) -> MenuSelection:
    """Handle Accounts submenu for online service credentials.

    See the module docstring for the two-tier (cached + live) username resolution
    and the edit/delete submenu behaviour.

    Args:
        menu_manager: MenuManager driving the loop; also used to refresh the menu
            when the background live lookup lands.
        get_lichess_api: Returns the stored Lichess token.
        handle_lichess_token_fn: Opens the token-entry keyboard.
        get_lichess_username: Returns the config-cached username (instant), or
            None when unknown. Read on each redraw (cheap) so a refresh after the
            live lookup reflects the newly cached name.
        fetch_lichess_username: Live network lookup for the current token, run on
            ``run_in_background`` exactly once per distinct token. Returns the
            username or None.
        delete_lichess_token_fn: Clears the stored token (and its cached name).
        run_in_background: Executor for the live lookup; defaults to a daemon
            thread. Injected as a synchronous runner in tests.
    """
    if run_in_background is None:
        run_in_background = _default_run_in_background

    # Shared with the background worker; the lock guards concurrent access
    # between the worker (writer) and build_entries (reader).
    lookup = {
        "lock": threading.Lock(),
        # nosec B105/B106: these keys hold API-token values for de-duplication
        # state, they are not credentials being assigned a hardcoded default.
        "started_token": None,  # nosec B105 - token a lookup was already started for
        "name": None,  # freshly fetched username
        "name_token": None,  # nosec B105 - token the fetched name belongs to
    }

    def start_live_lookup(token: str) -> None:
        if fetch_lichess_username is None or not token:
            return
        with lookup["lock"]:
            if lookup["started_token"] == token:
                return
            lookup["started_token"] = token

        def worker():
            name = fetch_lichess_username()
            with lookup["lock"]:
                # Drop a result whose token was superseded mid-flight.
                if lookup["started_token"] != token:
                    return
                lookup["name"] = name or None
                lookup["name_token"] = token
            if name:
                menu_manager.refresh_menu()

        run_in_background(worker)

    def resolve_username(token: str) -> Optional[str]:
        with lookup["lock"]:
            if lookup["name_token"] == token and lookup["name"]:
                return lookup["name"]
        if get_lichess_username is not None:
            return get_lichess_username() or None
        return None

    def invalidate_lookup() -> None:
        with lookup["lock"]:
            lookup["started_token"] = None
            lookup["name"] = None
            lookup["name_token"] = None

    def build_entries():
        token = get_lichess_api()
        start_live_lookup(token)
        username = resolve_username(token)
        detail = lichess_row_detail(username, token)
        return [
            IconMenuEntry(
                key="Lichess",
                label=f"Lichess\n{detail}",
                icon_name="lichess",
                enabled=True,
                font_size=12,
                height_ratio=2.0,
            ),
        ]

    def handle_selection(result: MenuSelection):
        if result.key == "Lichess":
            sub_result = handle_lichess_account_menu(
                menu_manager=menu_manager,
                get_lichess_api=get_lichess_api,
                handle_lichess_token_fn=handle_lichess_token_fn,
                delete_lichess_token_fn=delete_lichess_token_fn,
            )
            # The token may have changed (edited or deleted); allow a fresh live
            # lookup and drop any stale name on the next redraw.
            invalidate_lookup()
            if is_break_result(sub_result):
                return sub_result
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection)
