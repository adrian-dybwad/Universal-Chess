"""Lichess credentials are host:user rows owned by the Lichess plugin.

Why these tests exist
---------------------
Adding a token must authenticate against the chosen host and store
``org:alice`` (not a second catalog type). Org and .dev Alice are both
kept. Legacy ``lichess_dev`` sections and bare ``alice`` ids migrate.

How a regression manifests
--------------------------
Dev add overwrites org; migration drops a token; a ``dev:bob`` lookup
returns org Bob's token.
"""

import configparser

import pytest

from universalchess.board.settings import Settings
from universalchess.services.account_store import ResolvedIdentity, get_account, list_accounts
from universalchess.players.lichess.accounts import (
    add_lichess_credential,
    default_lichess_credential,
    get_lichess_credential,
    label_of,
    list_lichess_credentials,
    migrate_lichess_layout,
)
from universalchess.players.lichess.hosts import HOST_DEV, HOST_ORG


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


def _seed_section(cfg, section, values):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    if not parser.has_section(section):
        parser.add_section(section)
    for key, value in values.items():
        parser.set(section, key, value)
    with open(cfg, "w", encoding="utf-8") as handle:
        parser.write(handle)


def test_add_lichess_credential_keys_by_host_and_user(config_files):
    """A resolved token is stored as org:alice with host=org.

    Why: the player slot stores that id. Failure: section is account:lichess:alice
    and org/dev collide.
    """
    result = add_lichess_credential(
        {"api_token": "lip_org", "range": "800-1200"},
        resolver=_resolver("Alice"),
    )
    assert result.error is None
    assert result.account is not None
    assert result.account.id == "org:alice"
    assert result.account.get("host") == HOST_ORG
    assert result.account.get("api_token") == "lip_org"
    assert get_account("lichess", "org:alice") is not None


def test_org_and_dev_same_username_are_two_credentials(config_files):
    """The same Lichess username may exist on two hosts.

    Why: tokens are host-specific. Failure: the second add is duplicate.
    """
    org = add_lichess_credential(
        {"api_token": "lip_org", "host": HOST_ORG},
        resolver=_resolver("Alice"),
    )
    dev = add_lichess_credential(
        {"api_token": "lip_dev", "host": HOST_DEV},
        resolver=_resolver("Alice"),
    )
    assert org.error is None and dev.error is None
    ids = [a.id for a in list_lichess_credentials()]
    assert ids == ["org:alice", "dev:alice"]
    assert get_lichess_credential("dev:alice").get("api_token") == "lip_dev"
    assert get_lichess_credential("org:alice").get("api_token") == "lip_org"


def test_duplicate_on_same_host_is_rejected(config_files):
    """Two tokens that resolve to Alice on org are a duplicate.

    Why: uniqueness is (host, username). Failure: two org:alice sections.
    """
    add_lichess_credential({"api_token": "lip_a", "host": HOST_ORG}, resolver=_resolver("Alice"))
    dup = add_lichess_credential(
        {"api_token": "lip_b", "host": HOST_ORG}, resolver=_resolver("Alice")
    )
    assert dup.error == "duplicate"
    assert len(list_lichess_credentials()) == 1


def test_get_lichess_credential_dev_does_not_return_org(config_files):
    """Looking up dev:bob must not yield org Bob.

    Why: that is how an org token would be sent to lichess.dev. Failure: org
    token comes back.
    """
    add_lichess_credential({"api_token": "lip_org", "host": HOST_ORG}, resolver=_resolver("Bob"))
    assert get_lichess_credential("dev:bob") is None
    assert get_lichess_credential("bob").get("api_token") == "lip_org"


def test_get_lichess_credential_accepts_legacy_username_id(config_files):
    """A stored account:lichess:alice section is found as alice or org:alice.

    Why: unmigrated boards still have username-only ids. Failure: play finds
    no token.
    """
    _seed_section(
        config_files[0],
        "account:lichess:alice",
        {"api_token": "lip_legacy", "username": "Alice"},
    )
    found = get_lichess_credential("alice")
    assert found is not None
    assert found.get("api_token") == "lip_legacy"
    assert get_lichess_credential("org:alice").get("api_token") == "lip_legacy"


def test_list_orders_org_before_dev(config_files):
    """Default / chooser order is org then .dev, not lexicographic id.

    Why: 'dev:alice' sorts before 'org:alice' as strings, which would make
    Default a sandbox account. Failure: first credential is the dev one.
    """
    add_lichess_credential({"api_token": "lip_d", "host": HOST_DEV}, resolver=_resolver("Zed"))
    add_lichess_credential({"api_token": "lip_o", "host": HOST_ORG}, resolver=_resolver("Amy"))
    listed = list_lichess_credentials()
    assert [a.id for a in listed] == ["org:amy", "dev:zed"]
    assert default_lichess_credential().id == "org:amy"


def test_label_is_server_user(config_files):
    """Chooser/list label is lichess.org:Alice.

    Why: product listing is server:user. Failure: label is the raw id.
    """
    add_lichess_credential({"api_token": "lip", "host": HOST_ORG}, resolver=_resolver("Alice"))
    assert label_of(list_lichess_credentials()[0]) == "lichess.org:Alice"


def test_migrate_lichess_dev_section_to_dev_credential(config_files):
    """account:lichess_dev:bob becomes account:lichess:dev:bob.

    Why: the old second type is folded into the plugin's host:user id.
    Failure: the dev token is dropped or stays under lichess_dev.
    """
    cfg, _ = config_files
    _seed_section(
        cfg,
        "account:lichess_dev:bob",
        {"api_token": "lip_dev", "username": "Bob", "range": "800-1200"},
    )
    migrate_lichess_layout()
    assert list_accounts("lichess_dev") == []
    cred = get_account("lichess", "dev:bob")
    assert cred is not None
    assert cred.get("api_token") == "lip_dev"
    assert cred.get("host") == HOST_DEV


def test_migrate_bare_lichess_id_to_org(config_files):
    """account:lichess:alice becomes account:lichess:org:alice.

    Why: player.account=org:alice is the canonical pointer. Failure: the
    org token is left under a username-only section the picker does not list
    as server:user.
    """
    cfg, _ = config_files
    _seed_section(
        cfg,
        "account:lichess:alice",
        {"api_token": "lip_org", "username": "Alice"},
    )
    migrate_lichess_layout()
    assert get_account("lichess", "alice") is None
    cred = get_account("lichess", "org:alice")
    assert cred is not None
    assert cred.get("host") == HOST_ORG
    assert cred.get("api_token") == "lip_org"


def test_migrate_player_binding_uses_game_use_dev_once(config_files):
    """A Lichess slot with account=alice and lichess_use_dev becomes dev:alice.

    Why: the game toggle used to select the host; after this it must not.
    Failure: the slot stays bound to alice and plays on org.
    """
    cfg, _ = config_files
    _seed_section(cfg, "game", {"lichess_use_dev": "true"})
    _seed_section(cfg, "PlayerTwo", {"type": "lichess", "account": "alice"})
    _seed_section(
        cfg,
        "account:lichess_dev:alice",
        {"api_token": "lip_dev", "username": "Alice"},
    )
    migrate_lichess_layout()
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    assert parser.get("PlayerTwo", "account") == "dev:alice"
    assert get_lichess_credential("dev:alice").get("api_token") == "lip_dev"


def test_unknown_host_is_rejected(config_files):
    """Add with host=sandbox does not persist.

    Why: only shipped hosts are valid. Failure: a section is written for an
    API the plugin cannot call.
    """
    result = add_lichess_credential(
        {"api_token": "lip", "host": "sandbox"},
        resolver=_resolver("Alice"),
    )
    assert result.error == "unknown_host"
    assert list_lichess_credentials() == []
