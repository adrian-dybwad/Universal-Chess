"""Accounts menu helper (board, multi-account).

The Accounts menu lists every saved online account (e.g. Lichess logins) so a
user with more than one can tell them apart, and manages them:

  * Each row shows the account type, its resolved identity (the username the
    account is keyed on, stored when the account was added), and the masked
    secret. No network call is needed to paint the list because the identity is
    persisted per account.
  * "Add Account" picks an account type (from the catalog's ``accountTypes``
    definition) and collects that type's fields one at a time, then saves the
    account (which authenticates the credential to resolve/uniquely key it).
  * Selecting an account opens a detail submenu offering Delete, which confirms
    before removing the credential (mirroring the AI-agent clear-key flow).

The board-specific pieces (on-screen keyboard field capture, the account store,
the identity resolver) are injected so this module stays UI-logic only and is
unit-testable with a scripted menu manager.
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.utils.token_display import mask_token  # re-exported for callers


@dataclass
class AccountView:
    """Display model for one saved account row on the board.

    ``identity`` is the account's resolved player name (shown so accounts are
    distinguishable); ``masked_secret`` is the redacted credential for context.
    """

    type_id: str
    account_id: str
    identity: str
    type_label: str
    masked_secret: str = ""
    icon: str = "account"


def account_row_key(type_id: str, account_id: str) -> str:
    """Build the menu-entry key that encodes an account row's (type, id)."""
    return f"account:{type_id}:{account_id}"


def parse_account_row_key(key: str) -> Optional[Tuple[str, str]]:
    """Return ``(type_id, account_id)`` for an account row key, else None."""
    if not key.startswith("account:"):
        return None
    _, _, rest = key.partition(":")
    type_id, sep, account_id = rest.partition(":")
    if not sep or not type_id or not account_id:
        return None
    return type_id, account_id


def account_list_entries(accounts: List[AccountView]) -> List[IconMenuEntry]:
    """Build the Accounts menu rows: one per account plus an "Add Account" row.

    Kept pure (no menu manager) so the exact row composition -- type label,
    identity, masked secret, and the trailing Add row -- can be asserted directly.
    """
    entries: List[IconMenuEntry] = []
    for account in accounts:
        detail = account.identity or account.account_id
        label = f"{account.type_label}\n{detail}"
        if account.masked_secret:
            label += f"\n{account.masked_secret}"
        entries.append(
            IconMenuEntry(
                key=account_row_key(account.type_id, account.account_id),
                label=label,
                icon_name=account.icon,
                enabled=True,
                font_size=12,
                height_ratio=2.0,
            )
        )
    entries.append(
        IconMenuEntry(key="ADD", label="Add\nAccount", icon_name="account", enabled=True)
    )
    return entries


def confirm_delete_account(menu_manager, identity: str = "") -> bool:
    """Show a Delete/Cancel confirmation and return True only if Delete is chosen.

    Defaults the highlight to Cancel so a stray confirmation press cannot delete a
    credential; any non-Delete outcome (Cancel, BACK, break) is a refusal.
    """
    prompt = f"Delete\n{identity}?" if identity else "Delete\naccount?"
    entries = [
        IconMenuEntry(key="prompt", label=prompt, icon_name="cancel", enabled=True, selectable=False, font_size=12),
        IconMenuEntry(key="Delete", label="Delete", icon_name="cancel", enabled=True),
        IconMenuEntry(key="Cancel", label="Cancel", icon_name="undo", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=2)
    key = result.key if hasattr(result, "key") else result
    return key == "Delete"


def choose_account_type(
    menu_manager, account_type_choices: List[Tuple[str, str, str]]
) -> Optional[str]:
    """Pick an account type id, or None if cancelled.

    ``account_type_choices`` is a list of ``(type_id, label, icon)``. With a
    single type the picker is skipped and that type is returned directly (the
    common case today, where only Lichess exists); with several, a chooser menu
    is shown. A BACK/break returns None so the add flow is abandoned.
    """
    if not account_type_choices:
        return None
    if len(account_type_choices) == 1:
        return account_type_choices[0][0]
    entries = [
        IconMenuEntry(key=type_id, label=label, icon_name=icon, enabled=True)
        for type_id, label, icon in account_type_choices
    ]
    result = menu_manager.show_menu(entries)
    if is_break_result(result):
        return None
    key = result.key if hasattr(result, "key") else result
    if not key or key == "BACK":
        return None
    return key


def run_add_account_flow(
    fields: List[dict],
    capture_field: Callable[[dict], Optional[str]],
    submit: Callable[[dict], Tuple[bool, str]],
    notify: Callable[[str], None],
) -> bool:
    """Collect each field for a new account and submit it.

    Walks the type's ``fields`` in order, capturing each via ``capture_field``
    (the board's on-screen keyboard). Capturing returns None when the user
    cancels a field, which abandons the whole flow (returns False) so a partial
    account is never submitted. ``submit`` persists the collected values and
    returns ``(ok, message)``; on failure (e.g. duplicate/auth) ``notify`` shows
    the message. Returns True only when the account was saved.

    Kept free of the menu manager so the collect/abort/submit/notify contract is
    directly testable; the board wires the keyboard and account store in.
    """
    collected: dict = {}
    for field in fields:
        value = capture_field(field)
        if value is None:
            return False
        collected[field["key"]] = value
    ok, message = submit(collected)
    if not ok and message:
        notify(message)
    return ok


def handle_account_detail(
    menu_manager,
    account: AccountView,
    delete_account_fn: Callable[[str, str], None],
) -> None:
    """Detail submenu for one account: confirm-and-delete.

    Deletion is gated by :func:`confirm_delete_account`. Only an explicit,
    confirmed Delete calls ``delete_account_fn(type_id, account_id)``.
    """
    entries = [
        IconMenuEntry(
            key="header",
            label=f"{account.type_label}\n{account.identity or account.account_id}",
            icon_name=account.icon,
            enabled=True,
            selectable=False,
            font_size=12,
        ),
        IconMenuEntry(key="Delete", label="Delete\nAccount", icon_name="cancel", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=1)
    key = result.key if hasattr(result, "key") else result
    if key == "Delete" and confirm_delete_account(menu_manager, account.identity or account.account_id):
        delete_account_fn(account.type_id, account.account_id)


def handle_accounts_menu(
    menu_manager,
    list_accounts: Callable[[], List[AccountView]],
    account_type_choices: Callable[[], List[Tuple[str, str, str]]],
    add_account_fn: Callable[[str], None],
    delete_account_fn: Callable[[str, str], None],
) -> MenuSelection:
    """Multi-account Accounts menu loop.

    Args:
        menu_manager: MenuManager driving the loop (and the sub-menus).
        list_accounts: Returns the current accounts as :class:`AccountView`s,
            re-read on each redraw so add/delete reflect immediately.
        account_type_choices: Returns the selectable ``(type_id, label, icon)``
            account types for the Add flow.
        add_account_fn: Runs the board's Add flow for a chosen type id (keyboard
            field capture + save). Injected so this module holds no keyboard code.
        delete_account_fn: Removes an account by ``(type_id, account_id)``.
    """

    def build_entries():
        return account_list_entries(list_accounts())

    def handle_selection(result: MenuSelection):
        key = result.key
        if key == "ADD":
            type_id = choose_account_type(menu_manager, account_type_choices())
            if type_id:
                add_account_fn(type_id)
            return None
        parsed = parse_account_row_key(key)
        if parsed is not None:
            type_id, account_id = parsed
            account = next(
                (a for a in list_accounts() if a.type_id == type_id and a.account_id == account_id),
                None,
            )
            if account is not None:
                handle_account_detail(menu_manager, account, delete_account_fn)
            return None
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection)
