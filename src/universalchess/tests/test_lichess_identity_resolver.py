"""Tests for resolve_lichess_identity, the token-verification boundary.

resolve_lichess_identity is the only place the account store touches the network:
it authenticates a not-yet-saved Lichess token and returns the username the
account is keyed on. The berserk client is the external boundary and is faked via
sys.modules so these tests exercise the real classification logic (success vs the
distinct error codes) without any network call.
"""

import sys
import types

import pytest

from universalchess.services.lichess_service import resolve_lichess_identity


@pytest.fixture
def fake_berserk(monkeypatch):
    """Install a controllable fake ``berserk`` module.

    Returns a setter so each test decides what ``client.account.get()`` yields or
    raises, mirroring berserk's real surface (TokenSession + Client.account.get).
    """
    state = {"username": "MagnusC", "raises": None}

    class FakeAccount:
        def get(self):
            if state["raises"] is not None:
                raise state["raises"]
            return {"username": state["username"]}

    class FakeClient:
        def __init__(self, session=None):
            self.account = FakeAccount()

    module = types.ModuleType("berserk")
    module.TokenSession = lambda token: ("session", token)
    module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "berserk", module)
    return state


def test_resolves_username_for_valid_token(fake_berserk):
    """A valid token must resolve to its Lichess username with no error.

    This is the success path the Add Account flow relies on to key the account.
    A regression manifests as a missing identity or a spurious error code.
    """
    result = resolve_lichess_identity("lip_valid")
    assert result.error is None
    assert result.identity == "MagnusC"


def test_placeholder_and_empty_token_return_no_token(fake_berserk):
    """The 'tokenhere' placeholder and empty string must short-circuit as no_token.

    These mean "unset" and must never reach the network. A regression shows as an
    auth attempt or a different error code for an obviously-unset token.
    """
    assert resolve_lichess_identity("").error == "no_token"
    assert resolve_lichess_identity("tokenhere").error == "no_token"


def test_empty_username_is_auth_failed(fake_berserk):
    """A response with no username must be classified as auth_failed.

    A token that authenticates but yields no account name cannot key an account;
    the flow must reject it rather than store a blank identity.
    """
    fake_berserk["username"] = ""
    result = resolve_lichess_identity("lip_valid")
    assert result.error == "auth_failed"
    assert result.identity == ""


def test_network_exception_is_auth_failed(fake_berserk):
    """A berserk exception must be caught and classified, not propagated.

    The caller (Add Account) shows a friendly message; an uncaught exception
    would 500 the endpoint instead. A regression shows as the exception escaping
    or a wrong code.
    """
    fake_berserk["raises"] = RuntimeError("boom")
    result = resolve_lichess_identity("lip_valid")
    assert result.error == "auth_failed"
