"""Tests for the Accounts menu Lichess row: label, live lookup, and delete.

Why these tests exist
---------------------
The Accounts menu must let a user with more than one Lichess account tell which
account the stored token belongs to, and manage that credential:

  * The row shows the account name above the masked token.
  * The name is seeded from the config cache (instant) and refreshed by a live
    network lookup that runs on a background worker so it never blocks the UI.
  * The token can be deleted, mirroring the AI-agent "clear saved key" flow: the
    Delete option only appears when a token exists and it confirms first.

These tests pin that behaviour and its failure-degradation paths.
"""

from universalchess.managers.menu import MenuSelection
from universalchess.menus.accounts_menu import (
    confirm_delete_token,
    handle_accounts_menu,
    handle_lichess_account_menu,
    lichess_row_detail,
    mask_token,
)


TOKEN = "lip_secrettoken1234"


class _ScriptedMenuManager:
    """Fake MenuManager that drives run_menu_loop / show_menu deterministically.

    ``run_menu_loop`` builds entries ``rebuilds`` times (capturing each build in
    ``built``), then for each scripted selection builds once more and calls
    ``handle_selection``, returning early if the handler returns a value.
    ``show_menu`` pops queued results (used by the delete confirmation).
    """

    def __init__(self, rebuilds=1, selections=None, show_results=None):
        self.rebuilds = rebuilds
        self.selections = list(selections or [])
        self.show_results = list(show_results or [])
        self.built = []
        self.refresh_calls = 0
        self.show_initial_indexes = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0):
        for _ in range(self.rebuilds):
            self.built.append(build_entries())
        for selection in self.selections:
            self.built.append(build_entries())
            handler_result = handle_selection(selection)
            if handler_result is not None:
                return handler_result
        return None

    def show_menu(self, entries, initial_index=0, on_index_change=None):
        self.show_initial_indexes.append(initial_index)
        return self.show_results.pop(0)

    def refresh_menu(self):
        self.refresh_calls += 1


def _run_now(worker):
    """Synchronous stand-in for the background thread runner."""
    worker()


def _lichess_entry(entries):
    return next(entry for entry in entries if entry.key == "Lichess")


# --------------------------------------------------------------------------- #
# lichess_row_detail: name-above-token composition and fallbacks
# --------------------------------------------------------------------------- #

def test_row_detail_shows_name_above_masked_token():
    """The name is shown on the line above the masked token.

    How the regression manifests: the two are swapped, joined, or the name is
    dropped, so a multi-account user cannot read which account is configured.
    """
    assert lichess_row_detail("MagnusC", TOKEN) == f"MagnusC\n{mask_token(TOKEN)}"


def test_row_detail_falls_back_to_masked_token_without_name():
    """Without a known name the detail is the masked token alone.

    How the regression manifests: a stray blank line or empty name appears above
    the token.
    """
    assert lichess_row_detail(None, TOKEN) == mask_token(TOKEN)


def test_row_detail_not_set_without_token():
    """No token yields "Not set" regardless of any stale name.

    How the regression manifests: a masked empty token or leftover name implies
    an account is configured when none is.
    """
    assert lichess_row_detail("MagnusC", "") == "Not set"


# --------------------------------------------------------------------------- #
# handle_accounts_menu: background live lookup + caching + refresh
# --------------------------------------------------------------------------- #

def test_live_lookup_runs_once_caches_name_and_refreshes():
    """The live lookup runs once per token, is cached, and refreshes the menu.

    Why: ``build_entries`` runs on every redraw and the lookup hits the network,
    so it must be deduplicated per token and its result reused.
    How the regression manifests: the fetch call count exceeds one across three
    rebuilds, or the resolved name never appears in the row label.
    """
    calls = {"fetch": 0}

    def fetch():
        calls["fetch"] += 1
        return "LiveName"

    manager = _ScriptedMenuManager(rebuilds=3)
    handle_accounts_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        get_lichess_username=lambda: None,
        fetch_lichess_username=fetch,
        run_in_background=_run_now,
    )

    assert calls["fetch"] == 1, "network lookup must be deduplicated per token"
    assert manager.refresh_calls == 1, "a resolved name must refresh the menu once"
    label = _lichess_entry(manager.built[0]).label
    assert label == f"Lichess\nLiveName\n{mask_token(TOKEN)}"


def test_live_name_takes_priority_over_cached_name():
    """A freshly fetched name overrides the config-cached name.

    How the regression manifests: the stale cached name is shown after a live
    lookup resolved a different (current) name.
    """
    manager = _ScriptedMenuManager(rebuilds=1)
    handle_accounts_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        get_lichess_username=lambda: "CachedName",
        fetch_lichess_username=lambda: "LiveName",
        run_in_background=_run_now,
    )
    assert _lichess_entry(manager.built[0]).label == f"Lichess\nLiveName\n{mask_token(TOKEN)}"


def test_offline_live_lookup_falls_back_to_cached_name():
    """When the live lookup yields nothing, the cached name is used.

    Why: the device may be offline; the last-known (config) name should still
    identify the account.
    How the regression manifests: the row drops to the masked token even though a
    cached name is available, or the menu is needlessly refreshed for a None.
    """
    manager = _ScriptedMenuManager(rebuilds=1)
    handle_accounts_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        get_lichess_username=lambda: "CachedName",
        fetch_lichess_username=lambda: None,
        run_in_background=_run_now,
    )
    assert _lichess_entry(manager.built[0]).label == f"Lichess\nCachedName\n{mask_token(TOKEN)}"
    assert manager.refresh_calls == 0, "a None result must not trigger a refresh"


def test_no_live_lookup_when_no_token():
    """With no token stored, no live lookup is attempted and the row reads Not set.

    How the regression manifests: a pointless network worker is started for an
    empty token, or the label is not "Not set".
    """
    calls = {"fetch": 0}

    def fetch():
        calls["fetch"] += 1
        return "LiveName"

    manager = _ScriptedMenuManager(rebuilds=1)
    handle_accounts_menu(
        menu_manager=manager,
        get_lichess_api=lambda: "",
        handle_lichess_token_fn=lambda: None,
        get_lichess_username=lambda: None,
        fetch_lichess_username=fetch,
        run_in_background=_run_now,
    )
    assert calls["fetch"] == 0
    assert _lichess_entry(manager.built[0]).label == "Lichess\nNot set"


# --------------------------------------------------------------------------- #
# handle_lichess_account_menu: edit / delete submenu
# --------------------------------------------------------------------------- #

def test_submenu_offers_delete_only_when_token_present():
    """Delete is offered only when a token is stored.

    How the regression manifests: a Delete row appears with no token (nothing to
    delete) or is missing when a token exists.
    """
    with_token = _ScriptedMenuManager(rebuilds=1)
    handle_lichess_account_menu(
        menu_manager=with_token,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        delete_lichess_token_fn=lambda: None,
    )
    keys_with_token = [entry.key for entry in with_token.built[0]]
    assert "Delete" in keys_with_token

    without_token = _ScriptedMenuManager(rebuilds=1)
    handle_lichess_account_menu(
        menu_manager=without_token,
        get_lichess_api=lambda: "",
        handle_lichess_token_fn=lambda: None,
        delete_lichess_token_fn=lambda: None,
    )
    keys_without_token = [entry.key for entry in without_token.built[0]]
    assert "Delete" not in keys_without_token
    assert keys_without_token == ["Edit"]


def test_delete_confirmed_clears_token():
    """Confirming the delete calls the delete callback exactly once.

    How the regression manifests: the token is deleted without confirmation, or a
    confirmed delete does not clear it.
    """
    deleted = {"count": 0}
    # show_menu returns the "Delete" confirmation choice.
    manager = _ScriptedMenuManager(
        selections=[MenuSelection.from_key("Delete")],
        show_results=[MenuSelection.from_key("Delete")],
    )
    handle_lichess_account_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        delete_lichess_token_fn=lambda: deleted.__setitem__("count", deleted["count"] + 1),
    )
    assert deleted["count"] == 1


def test_delete_cancelled_keeps_token():
    """Cancelling the delete confirmation must not call the delete callback.

    How the regression manifests: the credential is removed on a Cancel/BACK,
    defeating the confirmation gate.
    """
    deleted = {"count": 0}
    manager = _ScriptedMenuManager(
        selections=[MenuSelection.from_key("Delete")],
        show_results=[MenuSelection.from_key("Cancel")],
    )
    handle_lichess_account_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: None,
        delete_lichess_token_fn=lambda: deleted.__setitem__("count", deleted["count"] + 1),
    )
    assert deleted["count"] == 0


def test_edit_selection_opens_token_entry():
    """Selecting Edit opens the token-entry keyboard.

    How the regression manifests: editing no longer routes to the keyboard, so a
    token cannot be set or changed.
    """
    opened = {"count": 0}
    manager = _ScriptedMenuManager(selections=[MenuSelection.from_key("Edit")])
    handle_lichess_account_menu(
        menu_manager=manager,
        get_lichess_api=lambda: TOKEN,
        handle_lichess_token_fn=lambda: opened.__setitem__("count", opened["count"] + 1),
        delete_lichess_token_fn=lambda: None,
    )
    assert opened["count"] == 1


# --------------------------------------------------------------------------- #
# confirm_delete_token: strict allow-list + safe default
# --------------------------------------------------------------------------- #

def test_confirm_delete_true_only_on_delete_choice():
    """Only the explicit Delete choice confirms; Cancel/BACK do not.

    How the regression manifests: a non-Delete outcome is treated as confirmation,
    deleting the credential on a stray press.
    """
    delete_manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Delete")])
    assert confirm_delete_token(delete_manager) is True

    cancel_manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    assert confirm_delete_token(cancel_manager) is False

    back_manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("BACK")])
    assert confirm_delete_token(back_manager) is False


def test_confirm_delete_defaults_highlight_to_cancel():
    """The confirmation highlights Cancel by default (index 2 of prompt/Delete/Cancel).

    Why: a destructive action must not be the default target for an accidental
    TICK.
    How the regression manifests: the initial index points at the Delete row.
    """
    manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    confirm_delete_token(manager)
    assert manager.show_initial_indexes == [2]
