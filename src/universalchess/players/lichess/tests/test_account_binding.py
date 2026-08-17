"""Tests for binding a Lichess player slot to a specific saved account.

With multiple accounts, a Lichess player must use the credential of the account
bound to its slot, not a single global token. These tests drive the real account
resolver against a temp config, covering the bound account, the unbound default,
a missing bound account, and the legacy single-token fallback for a board that
has not migrated.
"""

import configparser

import pytest

from universalchess.board.settings import Settings
from universalchess.menus.catalog import load_catalog
from universalchess.players.lichess import LichessPlayer, LichessPlayerConfig
from universalchess.services import account_store
from universalchess.services.account_store import ResolvedIdentity, add_account

LICHESS_TYPE = load_catalog().account_type("lichess")


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    return cfg, defcfg


def _resolver(username):
    return lambda fields: ResolvedIdentity(identity=username)


def _seed_two_accounts():
    add_account(LICHESS_TYPE, {"api_token": "lip_alice", "range": "800-1200"}, resolver=_resolver("Alice"))
    add_account(LICHESS_TYPE, {"api_token": "lip_bob", "range": "1500-2000"}, resolver=_resolver("Bob"))


def _seed_legacy(cfg, token):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    parser.add_section("lichess")
    parser.set("lichess", "api_token", token)
    with open(cfg, "w", encoding="utf-8") as handle:
        parser.write(handle)


def test_resolve_prefers_bound_account(config_files):
    """A slot bound to an account id must use that account's token and range.

    This is the core of multi-account play: two accounts exist, and the slot
    bound to 'bob' must yield Bob's token/range, never Alice's. A regression
    (ignoring the binding, using the default) yields Alice's credential here.
    """
    _seed_two_accounts()
    player = LichessPlayer(LichessPlayerConfig(account_id="bob"))
    token, rating_range = player._resolve_account()
    assert token == "lip_bob"
    assert rating_range == "1500-2000"


def test_resolve_falls_back_to_default_when_unbound(config_files):
    """An unbound slot uses the default (first) account for back-compat.

    A player configured before account binding existed has no account id; it must
    still play using the default account. A regression shows as an empty token
    when accounts exist.
    """
    _seed_two_accounts()
    player = LichessPlayer(LichessPlayerConfig(account_id=""))
    token, _ = player._resolve_account()
    assert token == "lip_alice"  # default_account is first by sorted id


def test_resolve_bound_missing_on_active_host_is_unbound(config_files):
    """A bound id that is not on this host must not use another account.

    Why: switching to lichess.dev with an org account id (or a deleted id) must
    not silently authenticate as a different user. Failure: ghost yields Alice.
    """
    _seed_two_accounts()
    player = LichessPlayer(LichessPlayerConfig(account_id="ghost"))
    token, rating_range = player._resolve_account()
    assert token == ""
    assert rating_range == ""


def test_resolve_dev_credential_does_not_read_org_accounts(config_files):
    """A dev:bob binding never returns an org token.

    Why: org and .dev are separate credentials. Failure: Bob's org token is used
    when the slot is bound to dev:bob.
    """
    _seed_two_accounts()
    player = LichessPlayer(LichessPlayerConfig(account_id="dev:bob"))
    token, rating_range = player._resolve_account()
    assert token == ""
    assert rating_range == ""


def test_resolve_falls_back_to_legacy_token_when_no_accounts(config_files):
    """With no saved accounts, resolution uses the legacy [lichess] token.

    Guards a board that has a legacy token but has not migrated to accounts yet:
    play must still work off the old single token. A regression shows as an empty
    token when only the legacy credential exists.
    """
    cfg, _ = config_files
    _seed_legacy(cfg, "lip_legacy")
    player = LichessPlayer(LichessPlayerConfig(account_id=""))
    token, _ = player._resolve_account()
    assert token == "lip_legacy"


def test_get_lichess_api_prefers_account_then_legacy(config_files):
    """centaur.get_lichess_api is account-aware: default account first, else legacy.

    Back-compat consumers (accounts display, get_lichess_connection) route through
    this, so after migration it must return the account token, and before
    migration the legacy token. A regression returns the stale legacy value even
    when an account exists.
    """
    cfg, _ = config_files
    from universalchess.board import centaur

    _seed_legacy(cfg, "lip_legacy")
    assert centaur.get_lichess_api() == "lip_legacy"
    add_account(LICHESS_TYPE, {"api_token": "lip_account"}, resolver=_resolver("Alice"))
    assert centaur.get_lichess_api() == "lip_account"


def test_picker_choices_unbound_selects_default_and_lists_every_account(config_files):
    """The lobby picker offers Default plus each credential.

    Why: the lobby's Account row opens this list. Regression: Default missing, a
    credential dropped, or Default not marked selected when nothing is chosen.
    """
    from universalchess.players.lichess.accounts import (
        label_of,
        lichess_account_picker_choices,
        list_lichess_credentials,
    )

    _seed_two_accounts()
    accounts = list_lichess_credentials()
    assert lichess_account_picker_choices("") == [
        ("", "Default account", True),
        *[(account.id, label_of(account), False) for account in accounts],
    ]


def _settings_with(lichess_account, *, player1_type="human", player2_type="lichess"):
    """AllSettings-like object: the lobby account plus two player slots."""
    from types import SimpleNamespace

    return SimpleNamespace(
        player1=SimpleNamespace(type=player1_type, account="org:stale"),
        player2=SimpleNamespace(type=player2_type, account="org:stale"),
        game=SimpleNamespace(lichess_account=lichess_account),
    )


def test_the_picker_marks_the_lobby_account_whatever_the_slots_hold(config_files):
    """The selected row is the lobby's account, not a slot's leftover binding.

    Why this test exists: the picker used to read the account off whichever slot
    was Lichess, so with no such slot nothing was ever marked and the pick had
    nowhere to be stored. The list must now reflect the one account the lobby
    holds, for every pairing -- including two engines.

    How a regression manifests: no row is selected, or the row selected is the
    one named by a slot's stale ``account`` value.
    """
    from universalchess.players.lichess.accounts import (
        lichess_play_account_choices,
        list_lichess_credentials,
    )

    _seed_two_accounts()
    chosen = list_lichess_credentials()[1].id

    for pairing in (("human", "lichess"), ("engine", "engine")):
        choices = lichess_play_account_choices(
            _settings_with(chosen, player1_type=pairing[0], player2_type=pairing[1])
        )
        selected = [key for key, _label, is_selected in choices if is_selected]
        assert selected == [chosen]
        # Every credential stays on offer: with one account for the board there
        # is no other slot whose account has to be held back.
        assert len(choices) == len(list_lichess_credentials()) + 1


def test_the_active_account_is_the_lobby_account(config_files):
    """Games and lobby lists authenticate as the account the lobby names.

    Why this test exists: this resolves the credential the seek, the ongoing
    list, and the challenge list all use. Reading it from a player slot is what
    made a lobby seek go out on the first credential rather than the chosen one.

    How a regression manifests: the returned credential is the default while the
    lobby names another, or a slot's stale id is honoured over an unset lobby
    account.
    """
    from universalchess.players.lichess.accounts import list_lichess_credentials
    from universalchess.players.lichess.lobby import active_lichess_account

    _seed_two_accounts()
    accounts = list_lichess_credentials()

    assert active_lichess_account(_settings_with(accounts[1].id)).id == accounts[1].id
    # Unset means Default, even though both slots still carry an old binding.
    assert active_lichess_account(_settings_with("")).id == accounts[0].id
