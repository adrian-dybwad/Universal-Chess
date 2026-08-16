"""Lichess hosts and credential ids are owned by the Lichess plugin.

Why these tests exist
---------------------
A Lichess player chooses a credential listed as server:user. Org and .dev (and
any later host) are rows on the plugin's host list, not a shared account type
and not a game-level toggle. These tests pin that id shape and that an unknown
host cannot be invented from a boolean.

How a regression manifests
--------------------------
``org:alice`` and ``dev:alice`` collapse to the same id; a bare ``alice`` is
not treated as org; or lichess.dev is no longer in the host list.
"""

from universalchess.services.lichess_hosts import (
    DEFAULT_HOST_ID,
    HOST_DEV,
    HOST_ORG,
    LICHESS_HOSTS,
    credential_id,
    credential_label,
    get_host,
    parse_credential_id,
)
import pytest


def test_shipped_hosts_are_org_and_dev():
    """The plugin ships lichess.org and lichess.dev as distinct hosts.

    Why: each host needs its own token. Failure: one row, or URLs swapped.
    """
    assert tuple((h.id, h.base_url) for h in LICHESS_HOSTS) == (
        (HOST_ORG, "https://lichess.org"),
        (HOST_DEV, "https://lichess.dev"),
    )


def test_credential_id_is_host_and_normalized_user():
    """Player.account stores org:alice, unique per host.

    Why: Alice on org and Alice on .dev are two credentials. Failure: both
    map to alice and the picker cannot tell them apart.
    """
    assert credential_id(HOST_ORG, "Alice") == "org:alice"
    assert credential_id(HOST_DEV, "Alice") == "dev:alice"
    assert credential_id(HOST_ORG, "Alice") != credential_id(HOST_DEV, "Alice")


def test_parse_credential_id_canonical_and_legacy_bare_username():
    """Canonical ids split on the host prefix; a bare username is org.

    Why: existing player.account=alice bindings must keep meaning org. Failure:
    alice is parsed as an unknown host or as dev.
    """
    assert parse_credential_id("org:alice") == (HOST_ORG, "alice")
    assert parse_credential_id("dev:Bob") == (HOST_DEV, "bob")
    assert parse_credential_id("alice") == (DEFAULT_HOST_ID, "alice")
    assert parse_credential_id("") == (DEFAULT_HOST_ID, "")


def test_parse_unknown_prefix_is_username_on_org():
    """A prefix that is not a shipped host is not treated as a host id.

    Why: inventing hosts from the id string would send tokens to a URL the
    plugin does not implement. Failure: 'foo:alice' selects host foo.
    """
    assert parse_credential_id("foo:alice") == (DEFAULT_HOST_ID, "foo:alice")


def test_get_host_rejects_unknown_id():
    """Runtime client construction must not invent a base URL.

    Why: a typo would silently hit lichess.org or raise later in berserk.
    Failure: get_host('sandbox') returns a host.
    """
    with pytest.raises(ValueError, match="unknown Lichess host"):
        get_host("sandbox")


def test_chooser_label_is_server_user():
    """The picker lists credentials as lichess.org:Alice.

    Why: that is the product label (server:user). Failure: the label is the
    raw id or username alone.
    """
    assert credential_label(HOST_ORG, "Alice") == "lichess.org:Alice"
    assert credential_label(HOST_DEV, "Bob") == "lichess.dev:Bob"
