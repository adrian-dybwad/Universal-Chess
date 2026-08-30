"""Tests for the two token-authenticating entry points of the lobby.

resolve_lichess_identity is the only place the account store touches the network:
it authenticates a not-yet-saved Lichess token and returns the username the
account is keyed on. get_lichess_connection does the same for a stored token and
hands the caller the connection to use. Both open an HTTP session, so both are
tested here against one fake: the berserk client is the external boundary and is
faked via sys.modules so these tests exercise the real classification logic
(success vs the distinct error codes) without any network call.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from universalchess.players.lichess.lobby import (
    get_lichess_connection,
    resolve_lichess_identity,
)


@pytest.fixture
def fake_berserk(monkeypatch):
    """Install a controllable fake ``berserk`` module.

    Returns a setter so each test decides what ``client.account.get()`` yields or
    raises, mirroring berserk's real surface (TokenSession + Client.account.get).
    """
    state = {"username": "MagnusC", "raises": None, "sessions": []}

    class FakeAccount:
        def get(self):
            if state["raises"] is not None:
                raise state["raises"]
            return {"username": state["username"]}

    class FakeTokenSession:
        """A class, not a lambda: the plugin subclasses TokenSession to make its
        streams abortable, which a factory function cannot support."""

        def __init__(self, token):
            self.token = token
            self.closed = False
            state["sessions"].append(self)

        def close(self):
            self.closed = True

    class FakeClient:
        def __init__(self, session=None, **kwargs):
            self.session = session
            self.account = FakeAccount()

    module = types.ModuleType("berserk")
    module.TokenSession = FakeTokenSession
    module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "berserk", module)
    return state


def test_resolves_username_for_valid_token(fake_berserk):
    """A valid token must resolve to its Lichess username with no error.

    This is the success path the Add Account flow relies on to key the account.
    A regression manifests as a missing identity or a spurious error code.
    """
    result = resolve_lichess_identity("lip_valid", host_id="org")
    assert result.error is None
    assert result.identity == "MagnusC"


def test_placeholder_and_empty_token_return_no_token(fake_berserk):
    """The 'tokenhere' placeholder and empty string must short-circuit as no_token.

    These mean "unset" and must never reach the network. A regression shows as an
    auth attempt or a different error code for an obviously-unset token.
    """
    assert resolve_lichess_identity("").error == "no_token"
    assert resolve_lichess_identity("tokenhere").error == "no_token"


def test_resolve_does_not_assume_org_when_host_is_omitted(fake_berserk):
    """A token without a host must not authenticate against lichess.org.

    Why: host is stored with the credential. Defaulting the resolver to org
    sent a .dev token to the wrong server.

    How the regression manifests: a session is opened, or error is None.
    """
    result = resolve_lichess_identity("lip_valid", host_id="")
    assert result.error == "unknown_host"
    assert fake_berserk["sessions"] == []


def test_empty_username_is_auth_failed(fake_berserk):
    """A response with no username must be classified as auth_failed.

    A token that authenticates but yields no account name cannot key an account;
    the flow must reject it rather than store a blank identity.
    """
    fake_berserk["username"] = ""
    result = resolve_lichess_identity("lip_valid", host_id="org")
    assert result.error == "auth_failed"
    assert result.identity == ""


@pytest.mark.parametrize(
    "raises,expected_error",
    [(None, None), (RuntimeError("boom"), "auth_failed")],
    ids=["verified", "failed"],
)
def test_identity_resolution_closes_its_http_session(fake_berserk, raises, expected_error):
    """Verifying a token must not leave a connection to Lichess behind.

    This asks one question and keeps no client, so the session it opened has no
    reader after the answer -- on the failure path as much as the success path,
    since Add Account is where a wrong token is retried repeatedly. A regression
    manifests as a session that is never closed, holding a socket to lichess.org
    until the collector runs.
    """
    fake_berserk["raises"] = raises

    result = resolve_lichess_identity("lip_valid", host_id="org")

    assert result.error == expected_error
    assert [session.closed for session in fake_berserk["sessions"]] == [True]


def test_a_failed_lobby_sign_in_closes_the_session_it_opened(fake_berserk):
    """A sign-in that fails after connecting must not strand its session.

    get_lichess_connection returns None in place of the connection when it
    fails, so the caller has nothing to close and the session it opened is
    reachable only from inside. A regression manifests as one unclosed session
    per failed sign-in -- the case that repeats when a token has been revoked
    and the lobby is reopened.
    """
    fake_berserk["raises"] = RuntimeError("boom")

    connection, username, error = get_lichess_connection(
        "lip_valid", MagicMock(), host_id="org"
    )

    assert (connection, username, error) == (None, None, "network")
    assert [session.closed for session in fake_berserk["sessions"]] == [True]


def test_a_successful_lobby_sign_in_hands_over_an_open_connection(fake_berserk):
    """The caller owns the connection, so it must arrive usable.

    Closing eagerly here would break the Ongoing and Challenges lists, which
    query through this client after it is returned. A regression manifests as a
    closed session, and on the board as lobby lists that cannot load.
    """
    connection, username, error = get_lichess_connection(
        "lip_valid", MagicMock(), host_id="org"
    )

    assert (username, error) == ("MagnusC", None)
    assert connection is not None
    assert [session.closed for session in fake_berserk["sessions"]] == [False]

    connection.close()
    assert fake_berserk["sessions"][0].closed is True


def test_network_exception_is_auth_failed(fake_berserk):
    """A berserk exception must be caught and classified, not propagated.

    The caller (Add Account) shows a friendly message; an uncaught exception
    would 500 the endpoint instead. A regression shows as the exception escaping
    or a wrong code.
    """
    fake_berserk["raises"] = RuntimeError("boom")
    result = resolve_lichess_identity("lip_valid", host_id="org")
    assert result.error == "auth_failed"
