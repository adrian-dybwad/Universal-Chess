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


def test_resolve_falls_back_to_default_when_bound_account_missing(config_files):
    """A slot bound to a deleted account must not fail; it uses the default.

    If the bound account was removed, failing outright would break a saved game
    setup. Falling back to the default keeps play working. A regression shows as
    an empty token despite an available default account.
    """
    _seed_two_accounts()
    player = LichessPlayer(LichessPlayerConfig(account_id="ghost"))
    token, _ = player._resolve_account()
    assert token == "lip_alice"


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

    Back-compat consumers (accounts display, get_lichess_client) route through
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
