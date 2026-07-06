"""Tests for the multi-account Accounts menu (board).

Why these tests exist
---------------------
The Accounts menu must let a user manage several online accounts:

  * The list shows one row per saved account (type + resolved identity + masked
    secret) plus a trailing "Add Account" row, so multiple accounts are
    distinguishable without a network call.
  * "Add Account" picks a type (skipping the picker when only one type exists)
    and collects that type's fields in order, abandoning the flow if any field is
    cancelled so a partial account is never saved.
  * Selecting an account can delete it, gated by an explicit, Cancel-defaulted
    confirmation (mirroring the AI-agent clear-key flow).

These tests pin that behaviour and its safe-default/abort paths.
"""

from universalchess.managers.menu import MenuSelection
from universalchess.menus.accounts_menu import (
    AccountView,
    account_list_entries,
    account_row_key,
    choose_account_type,
    confirm_delete_account,
    handle_accounts_menu,
    mask_token,
    parse_account_row_key,
    run_add_account_flow,
)


TOKEN = "lip_secrettoken1234"

LICHESS = AccountView(
    type_id="lichess",
    account_id="magnusc",
    identity="MagnusC",
    type_label="Lichess",
    masked_secret=mask_token(TOKEN),
    icon="lichess",
)
SECOND = AccountView(
    type_id="lichess",
    account_id="hikaru",
    identity="Hikaru",
    type_label="Lichess",
    masked_secret=mask_token("lip_othertoken5678"),
    icon="lichess",
)


class _ScriptedMenuManager:
    """Fake MenuManager driving run_menu_loop / show_menu deterministically.

    ``run_menu_loop`` builds entries ``rebuilds`` times (captured in ``built``),
    then for each scripted selection builds once more and calls
    ``handle_selection``, returning early if the handler returns a value.
    ``show_menu`` pops queued results (used by the type picker, account detail,
    and delete confirmation), recording the initial index each time.
    """

    def __init__(self, rebuilds=1, selections=None, show_results=None):
        self.rebuilds = rebuilds
        self.selections = list(selections or [])
        self.show_results = list(show_results or [])
        self.built = []
        self.shown = []
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
        self.shown.append(entries)
        self.show_initial_indexes.append(initial_index)
        return self.show_results.pop(0)


# --------------------------------------------------------------------------- #
# account_list_entries: one row per account + an Add row
# --------------------------------------------------------------------------- #

def test_list_entries_render_each_account_then_add_row():
    """Each account becomes a row (type/identity/masked secret) and Add is last.

    How the regression manifests: an account is missing/duplicated, its identity
    or masked secret is dropped, or the Add row is absent/misplaced so the user
    cannot add an account.
    """
    entries = account_list_entries([LICHESS, SECOND])
    keys = [e.key for e in entries]
    assert keys == [
        account_row_key("lichess", "magnusc"),
        account_row_key("lichess", "hikaru"),
        "ADD",
    ]
    assert entries[0].label == f"Lichess\nMagnusC\n{mask_token(TOKEN)}"
    assert entries[-1].key == "ADD"


def test_list_entries_add_row_only_when_no_accounts():
    """With no saved accounts only the Add row shows.

    How the regression manifests: an empty/phantom account row is rendered, or
    the Add affordance disappears leaving no way to create the first account.
    """
    entries = account_list_entries([])
    assert [e.key for e in entries] == ["ADD"]


def test_row_key_round_trips():
    """A row key encodes and decodes (type, id) losslessly.

    How the regression manifests: selecting a row resolves to the wrong account
    (or none), so delete/detail acts on an unintended credential.
    """
    assert parse_account_row_key(account_row_key("lichess", "magnusc")) == ("lichess", "magnusc")
    assert parse_account_row_key("ADD") is None
    assert parse_account_row_key("account:lichess:") is None


# --------------------------------------------------------------------------- #
# handle_accounts_menu: add flow routing and account deletion
# --------------------------------------------------------------------------- #

def test_add_with_single_type_skips_picker():
    """Choosing Add with one account type runs the flow for that type directly.

    Why: forcing a one-item picker is friction; the single type is unambiguous.
    How the regression manifests: the add flow is not invoked, or is invoked with
    the wrong type id.
    """
    added = []
    manager = _ScriptedMenuManager(selections=[MenuSelection.from_key("ADD")])
    handle_accounts_menu(
        menu_manager=manager,
        list_accounts=lambda: [LICHESS],
        account_type_choices=lambda: [("lichess", "Lichess", "lichess")],
        add_account_fn=lambda type_id: added.append(type_id),
        delete_account_fn=lambda t, a: None,
    )
    assert added == ["lichess"]


def test_add_with_multiple_types_shows_picker():
    """With several account types, Add shows a picker and honours the choice.

    How the regression manifests: the picker is skipped (wrong type assumed) or
    the chosen type is not the one the add flow runs for.
    """
    added = []
    manager = _ScriptedMenuManager(
        selections=[MenuSelection.from_key("ADD")],
        show_results=[MenuSelection.from_key("chesscom")],
    )
    handle_accounts_menu(
        menu_manager=manager,
        list_accounts=lambda: [],
        account_type_choices=lambda: [
            ("lichess", "Lichess", "lichess"),
            ("chesscom", "Chess.com", "account"),
        ],
        add_account_fn=lambda type_id: added.append(type_id),
        delete_account_fn=lambda t, a: None,
    )
    assert added == ["chesscom"]


def test_selecting_account_confirmed_delete_removes_it():
    """Selecting an account then confirming Delete removes exactly that account.

    How the regression manifests: delete targets the wrong (type, id), deletes
    without confirmation, or a confirmed delete is a no-op.
    """
    deleted = []
    # First show_menu = account detail (returns Delete); second = confirmation.
    manager = _ScriptedMenuManager(
        selections=[MenuSelection.from_key(account_row_key("lichess", "magnusc"))],
        show_results=[MenuSelection.from_key("Delete"), MenuSelection.from_key("Delete")],
    )
    handle_accounts_menu(
        menu_manager=manager,
        list_accounts=lambda: [LICHESS, SECOND],
        account_type_choices=lambda: [("lichess", "Lichess", "lichess")],
        add_account_fn=lambda type_id: None,
        delete_account_fn=lambda t, a: deleted.append((t, a)),
    )
    assert deleted == [("lichess", "magnusc")]


def test_selecting_account_cancelled_delete_keeps_it():
    """Cancelling the confirmation leaves the account untouched.

    How the regression manifests: the credential is removed on Cancel/BACK,
    defeating the confirmation gate.
    """
    deleted = []
    manager = _ScriptedMenuManager(
        selections=[MenuSelection.from_key(account_row_key("lichess", "magnusc"))],
        show_results=[MenuSelection.from_key("Delete"), MenuSelection.from_key("Cancel")],
    )
    handle_accounts_menu(
        menu_manager=manager,
        list_accounts=lambda: [LICHESS],
        account_type_choices=lambda: [("lichess", "Lichess", "lichess")],
        add_account_fn=lambda type_id: None,
        delete_account_fn=lambda t, a: deleted.append((t, a)),
    )
    assert deleted == []


# --------------------------------------------------------------------------- #
# choose_account_type: single vs many, and cancel
# --------------------------------------------------------------------------- #

def test_choose_type_returns_only_type_without_prompting():
    """A single type is returned without showing a menu.

    How the regression manifests: a needless picker is shown for one option.
    """
    manager = _ScriptedMenuManager()
    assert choose_account_type(manager, [("lichess", "Lichess", "lichess")]) == "lichess"
    assert manager.shown == []


def test_choose_type_cancel_returns_none():
    """A BACK/cancel from the picker abandons the add (returns None).

    How the regression manifests: a cancelled picker still creates/points at an
    account type, launching an unwanted add flow.
    """
    manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("BACK")])
    result = choose_account_type(
        manager, [("lichess", "Lichess", "lichess"), ("chesscom", "Chess.com", "account")]
    )
    assert result is None


# --------------------------------------------------------------------------- #
# run_add_account_flow: collect / abort / submit / notify
# --------------------------------------------------------------------------- #

_FIELDS = [
    {"key": "api_token", "label": "API Token", "required": True},
    {"key": "range", "label": "Rating range", "required": False},
]


def test_add_flow_collects_all_fields_then_submits():
    """Every field is captured in order and submitted as one payload.

    How the regression manifests: a field is skipped or misordered, so the
    submitted account is missing data or maps values to the wrong keys.
    """
    submitted = {}
    captured_order = []

    def capture(field):
        captured_order.append(field["key"])
        return {"api_token": TOKEN, "range": "1600-1800"}[field["key"]]

    ok = run_add_account_flow(
        _FIELDS,
        capture_field=capture,
        submit=lambda values: (True, "") if submitted.update(values) is None else (True, ""),
        notify=lambda msg: None,
    )
    assert ok is True
    assert captured_order == ["api_token", "range"]
    assert submitted == {"api_token": TOKEN, "range": "1600-1800"}


def test_add_flow_aborts_when_a_field_is_cancelled():
    """Cancelling any field abandons the flow without submitting.

    Why: a half-entered account must never be persisted.
    How the regression manifests: submit runs with missing/blank fields, creating
    a broken account instead of a clean abort.
    """
    submit_calls = {"count": 0}

    def capture(field):
        return None if field["key"] == "api_token" else "x"

    ok = run_add_account_flow(
        _FIELDS,
        capture_field=capture,
        submit=lambda values: submit_calls.__setitem__("count", submit_calls["count"] + 1) or (True, ""),
        notify=lambda msg: None,
    )
    assert ok is False
    assert submit_calls["count"] == 0


def test_add_flow_notifies_on_submit_failure():
    """A failed submit surfaces its message and reports failure.

    How the regression manifests: a duplicate/auth error is swallowed silently,
    so the user believes the account was added when it was not.
    """
    messages = []
    ok = run_add_account_flow(
        [{"key": "api_token", "label": "API Token", "required": True}],
        capture_field=lambda field: TOKEN,
        submit=lambda values: (False, "An account named MagnusC already exists"),
        notify=lambda msg: messages.append(msg),
    )
    assert ok is False
    assert messages == ["An account named MagnusC already exists"]


# --------------------------------------------------------------------------- #
# confirm_delete_account: strict allow-list + safe default
# --------------------------------------------------------------------------- #

def test_confirm_delete_true_only_on_delete_choice():
    """Only the explicit Delete choice confirms; Cancel/BACK do not.

    How the regression manifests: a non-Delete outcome is treated as
    confirmation, deleting a credential on a stray press.
    """
    assert confirm_delete_account(_ScriptedMenuManager(show_results=[MenuSelection.from_key("Delete")])) is True
    assert confirm_delete_account(_ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])) is False
    assert confirm_delete_account(_ScriptedMenuManager(show_results=[MenuSelection.from_key("BACK")])) is False


def test_confirm_delete_defaults_highlight_to_cancel():
    """The confirmation highlights Cancel by default (index 2 of prompt/Delete/Cancel).

    Why: a destructive action must not be the default target for an accidental
    TICK.
    How the regression manifests: the initial index points at the Delete row.
    """
    manager = _ScriptedMenuManager(show_results=[MenuSelection.from_key("Cancel")])
    confirm_delete_account(manager, "MagnusC")
    assert manager.show_initial_indexes == [2]
