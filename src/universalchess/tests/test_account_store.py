"""Tests for the multi-account store.

The account store persists one INI section per saved online account
(``[account:<type>:<id>]`` in ``centaur.ini``) and enforces the invariants the
Add Account flow depends on: identity resolution, per-type uniqueness (no two
accounts sharing a player name), and migration of the legacy single ``[lichess]``
section into an account. Tests drive the real read/write path against a temp
config so a persisted write is read back exactly as the board would see it; the
network-bound identity resolver is injected as a fake (the only external
boundary) so no test authenticates against Lichess.
"""

import configparser

import pytest

from universalchess.board.settings import Settings
from universalchess.menus.catalog import load_catalog
from universalchess.services import account_store
from universalchess.services.account_store import (
    Account,
    ResolvedIdentity,
    accounts_conflict,
    add_account,
    default_account,
    delete_account,
    get_account,
    list_accounts,
    migrate_legacy_lichess,
    parse_section,
    resolve_account_id,
    section_name,
    selectable_accounts_for_slot,
)


LICHESS_TYPE = load_catalog().account_type("lichess")


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """Point Settings at a writable temp centaur.ini + empty defaults file.

    Isolates the store's read/write from the real on-disk config so tests can
    seed exact sections and read back what a save persisted.
    """
    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    return cfg, defcfg


def _resolver(username, error=None, message=""):
    """A fake identity resolver standing in for the Lichess auth call.

    Returns the given username (as the resolved display identity) or an error
    code, without any network access.
    """

    def resolve(fields):
        if error:
            return ResolvedIdentity(error=error, message=message)
        return ResolvedIdentity(identity=username)

    return resolve


def _seed_section(cfg, section, values):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    if not parser.has_section(section):
        parser.add_section(section)
    for key, value in values.items():
        parser.set(section, key, value)
    with open(cfg, "w", encoding="utf-8") as handle:
        parser.write(handle)


def _read_section(cfg, section):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    return dict(parser.items(section)) if parser.has_section(section) else {}


def test_section_name_and_parse_roundtrip():
    """section_name/parse_section must be exact inverses for a normalized id.

    The section name is the only place the (type, id) pair is stored, so a
    mismatch between building and parsing it would orphan every account. This
    pins the ``account:<type>:<id>`` shape and that non-account sections parse
    to None (so listing never mistakes ``[lichess]`` or ``[game]`` for accounts).
    """
    assert section_name("lichess", "magnusc") == "account:lichess:magnusc"
    assert parse_section("account:lichess:magnusc") == ("lichess", "magnusc")
    assert parse_section("lichess") is None
    assert parse_section("game") is None
    assert parse_section("account:lichess:") is None


def test_add_account_persists_section_with_resolved_identity(config_files):
    """A resolved-identity add must store all fields keyed by the normalized id.

    Guards the core Add Account path: the token+range are stored, the identity
    (username) is resolved via the injected resolver and stored under the
    identity field, and the section id is the lowercased username. A regression
    (wrong section name, dropped field, unstored identity) shows as a missing key
    or missing/renamed section here.
    """
    cfg, _ = config_files
    result = add_account(
        LICHESS_TYPE,
        {"api_token": "lip_secret1", "range": "1000-1600"},
        resolver=_resolver("MagnusC"),
    )
    assert result.error is None
    assert result.account is not None
    assert result.account.id == "magnusc"

    stored = _read_section(cfg, "account:lichess:magnusc")
    assert stored["api_token"] == "lip_secret1"
    assert stored["range"] == "1000-1600"
    # Identity stored under the definition's identityField, preserving display case.
    assert stored["username"] == "MagnusC"

    listed = list_accounts("lichess")
    assert len(listed) == 1
    assert listed[0].id == "magnusc"
    assert listed[0].get("username") == "MagnusC"


def test_add_account_rejects_duplicate_identity_case_insensitively(config_files):
    """Two accounts resolving to the same username (any case) must be rejected.

    This is the "not for the same player name" rule. The second add resolves to
    'magnusc' (same normalized id as 'MagnusC'), so it must fail as a duplicate
    and must not create or overwrite a section. A regression shows as a second
    section, an overwrite of the first token, or a success result.
    """
    cfg, _ = config_files
    first = add_account(LICHESS_TYPE, {"api_token": "lip_a"}, resolver=_resolver("MagnusC"))
    assert first.error is None

    dup = add_account(LICHESS_TYPE, {"api_token": "lip_b"}, resolver=_resolver("magnusc"))
    assert dup.error == "duplicate"
    assert dup.account is None
    # Only one account, and the original token is untouched.
    assert len(list_accounts("lichess")) == 1
    assert _read_section(cfg, "account:lichess:magnusc")["api_token"] == "lip_a"


def test_add_account_missing_required_field(config_files):
    """An empty required field must be rejected before any resolution/persist.

    api_token is required; an empty value must return a missing_field error and
    persist nothing. A regression shows as a resolver call or a stored section
    despite the missing token.
    """
    resolver_calls = []

    def tracking_resolver(fields):
        resolver_calls.append(fields)
        return ResolvedIdentity(identity="x")

    result = add_account(LICHESS_TYPE, {"api_token": "  ", "range": "1"}, resolver=tracking_resolver)
    assert result.error == "missing_field"
    assert result.account is None
    assert resolver_calls == []  # never reached resolution
    assert list_accounts("lichess") == []


def test_add_account_auth_failure_propagates_and_persists_nothing(config_files):
    """A resolver error (e.g. bad token) must abort the add with that error.

    When the token cannot be authenticated the identity is unknown, so no account
    can be keyed. The error code must propagate and nothing may be stored. A
    regression shows as a persisted section or a success despite auth failure.
    """
    result = add_account(
        LICHESS_TYPE,
        {"api_token": "lip_bad"},
        resolver=_resolver("", error="auth_failed", message="Invalid token"),
    )
    assert result.error == "auth_failed"
    assert result.account is None
    assert list_accounts("lichess") == []


def test_list_get_and_delete_multiple_accounts(config_files):
    """list/get/delete must operate per (type,id) without touching siblings.

    Two Lichess accounts are added; list returns both sorted by id, get resolves
    each, and deleting one leaves the other intact. A regression shows as the
    wrong count, a cross-account overwrite, or delete removing/returning the
    wrong account.
    """
    add_account(LICHESS_TYPE, {"api_token": "lip_a"}, resolver=_resolver("Alice"))
    add_account(LICHESS_TYPE, {"api_token": "lip_b"}, resolver=_resolver("Bob"))

    listed = list_accounts("lichess")
    assert [a.id for a in listed] == ["alice", "bob"]

    assert get_account("lichess", "bob").get("api_token") == "lip_b"
    assert get_account("lichess", "nobody") is None

    assert delete_account("lichess", "alice") is True
    assert [a.id for a in list_accounts("lichess")] == ["bob"]
    assert delete_account("lichess", "alice") is False  # already gone


def test_list_accounts_ignores_non_account_sections(config_files):
    """Only ``account:*`` sections are accounts; others must be ignored.

    The config holds unrelated sections (game, lichess, PlayerOne). Listing must
    not surface any of them as accounts. A regression (loose prefix match) shows
    as a phantom account here.
    """
    cfg, _ = config_files
    _seed_section(cfg, "game", {"time_control": "5"})
    _seed_section(cfg, "lichess", {"api_token": "legacy", "range": "0-3000"})
    _seed_section(cfg, "PlayerOne", {"type": "human"})
    assert list_accounts() == []


def test_default_account_returns_first_of_type(config_files):
    """default_account returns the first account of a type, or None when empty.

    Back-compat readers (the existing single-token Lichess flow) resolve the
    "default" account through this. A regression shows as the wrong account or a
    crash when none exist.
    """
    assert default_account("lichess") is None
    add_account(LICHESS_TYPE, {"api_token": "lip_a"}, resolver=_resolver("Alice"))
    add_account(LICHESS_TYPE, {"api_token": "lip_b"}, resolver=_resolver("Bob"))
    first = default_account("lichess")
    assert first is not None and first.id == "alice"


def test_migrate_legacy_lichess_moves_section_and_clears_legacy(config_files):
    """A legacy [lichess] token+username must migrate into one account, once.

    Guards the upgrade path: the single global credential becomes a normal
    account, and the legacy token/username are cleared so the account is the sole
    source (no drift). Migration is idempotent: a second call finds the account
    already present and does nothing. A regression shows as no account created,
    the legacy token left populated, or a duplicate on the second call.
    """
    cfg, _ = config_files
    _seed_section(cfg, "lichess", {"api_token": "lip_legacy", "username": "LegacyUser", "range": "800-1200"})

    account = migrate_legacy_lichess(LICHESS_TYPE)
    assert account is not None
    assert account.id == "legacyuser"

    stored = _read_section(cfg, "account:lichess:legacyuser")
    assert stored["api_token"] == "lip_legacy"
    assert stored["username"] == "LegacyUser"
    assert stored["range"] == "800-1200"
    # Legacy credential cleared to avoid two sources of truth.
    legacy = _read_section(cfg, "lichess")
    assert legacy.get("api_token", "") == ""
    assert legacy.get("username", "") == ""

    # Idempotent: nothing to migrate now that an account exists.
    assert migrate_legacy_lichess(LICHESS_TYPE) is None
    assert len(list_accounts("lichess")) == 1


def test_migrate_skips_when_identity_unknown_and_no_resolver(config_files):
    """Migration must not fabricate an id when the username is unknown offline.

    A legacy token with no cached username and no resolver cannot be keyed by a
    real player name; migrating under a guessed id would create a bogus account.
    The legacy section must be left untouched for the back-compat reader. A
    regression shows as an account created from an unknown identity.
    """
    cfg, _ = config_files
    _seed_section(cfg, "lichess", {"api_token": "lip_legacy", "username": "", "range": "0-3000"})
    assert migrate_legacy_lichess(LICHESS_TYPE, resolver=None) is None
    assert list_accounts("lichess") == []
    assert _read_section(cfg, "lichess")["api_token"] == "lip_legacy"


def test_migrate_resolves_username_when_absent(config_files):
    """When the cached username is absent, migration may resolve it via the resolver.

    A legacy token with no cached username still migrates if the injected
    resolver can authenticate and return the username. A regression shows as a
    skipped migration despite a working resolver.
    """
    cfg, _ = config_files
    _seed_section(cfg, "lichess", {"api_token": "lip_legacy", "username": "", "range": "0-3000"})
    account = migrate_legacy_lichess(LICHESS_TYPE, resolver=_resolver("Resolved"))
    assert account is not None and account.id == "resolved"
    assert list_accounts("lichess")[0].get("username") == "Resolved"


def test_migrate_skips_placeholder_token(config_files):
    """The 'tokenhere' placeholder must not be migrated as a real credential.

    The default config ships api_token as the 'tokenhere' sentinel meaning
    "unset"; migrating it would create a broken account. A regression shows as an
    account created from the placeholder.
    """
    cfg, _ = config_files
    _seed_section(cfg, "lichess", {"api_token": "tokenhere", "username": "X"})
    assert migrate_legacy_lichess(LICHESS_TYPE) is None
    assert list_accounts("lichess") == []


def test_ensure_lichess_migrated_uses_packaged_definition(config_files):
    """ensure_lichess_migrated wires the real catalog definition to migration.

    Guards the startup/first-read entry point: given a legacy [lichess] token
    with a cached username, it must migrate using the shipped Lichess account
    type (no explicit definition passed in). A regression shows as no migration
    despite a legacy credential, or a wrong section name from the wrong identity
    field.
    """
    cfg, _ = config_files
    _seed_section(cfg, "lichess", {"api_token": "lip_legacy", "username": "Cached", "range": "0-3000"})
    from universalchess.services.account_store import ensure_lichess_migrated

    account = ensure_lichess_migrated()
    assert account is not None and account.id == "cached"
    assert list_accounts("lichess")[0].get("api_token") == "lip_legacy"


def test_add_account_entered_identity_uses_field_value(config_files):
    """An 'entered'-identity type keys the account on a typed field, no resolver.

    Guards the generic path for future account types whose identity is user
    entered rather than resolved by auth. The account id comes straight from the
    identity field. A regression shows as a resolver requirement or a missing id.
    """
    entered_type = {
        "id": "lichess",  # reuse a valid type id; identity behavior is what's under test
        "label": "Entered",
        "icon": "lichess",
        "identityField": "handle",
        "identitySource": "entered",
        "fields": [
            {"key": "handle", "label": "Handle", "type": "text", "required": True},
            {"key": "url", "label": "URL", "type": "text"},
        ],
    }
    result = add_account(entered_type, {"handle": "PlayerX", "url": "https://x"}, resolver=None)
    assert result.error is None
    assert result.account.id == "playerx"
    assert get_account("lichess", "playerx").get("url") == "https://x"


def _seed_two_lichess(cfg):
    """Seed two Lichess accounts (ids 'alice', 'bob' by sort order) for pair tests."""
    add_account(LICHESS_TYPE, {"api_token": "lip_a"}, resolver=_resolver("Alice"))
    add_account(LICHESS_TYPE, {"api_token": "lip_b"}, resolver=_resolver("Bob"))


def test_resolve_account_id_falls_back_to_default_when_unbound(config_files):
    """An empty binding resolves to the default (first) account, matching the player.

    resolve_account_id must mirror LichessPlayer._resolve_account: an unbound slot
    (empty account id) plays as the first account of the type. If this diverged
    from the player, the collision rule would judge a different account than the
    one actually used at game time. A regression shows as None/'bob' instead of
    the first account 'alice'.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert resolve_account_id("lichess", "") == "alice"


def test_resolve_account_id_uses_explicit_binding_when_present(config_files):
    """An explicit, existing binding resolves to itself, not the default.

    A slot bound to 'bob' must resolve to 'bob'. A regression (ignoring the
    explicit id and returning the default) shows as 'alice' here.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert resolve_account_id("lichess", "bob") == "bob"


def test_resolve_account_id_dangling_binding_falls_back_to_default(config_files):
    """A binding to a deleted account falls back to the default, like the player.

    LichessPlayer keeps a stale-id setup playable by using the default account;
    resolve_account_id must agree so the collision rule uses the same effective
    account. A regression shows as 'ghost'/None instead of 'alice'.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert resolve_account_id("lichess", "ghost") == "alice"


def test_resolve_account_id_none_when_type_has_no_accounts(config_files):
    """With no accounts of the type, resolution is None (nothing to bind).

    An online slot with no saved account binds to nothing and so can never
    collide. A regression (returning '' or raising) shows here.
    """
    config_files  # fixture isolates config; intentionally no accounts seeded
    assert resolve_account_id("lichess", "") is None
    assert resolve_account_id("lichess", "whoever") is None


def test_accounts_conflict_both_default_collide_on_first_account(config_files):
    """Two same-type slots both on 'Default' collide on the shared first account.

    This is the subtle case the rule targets: 'Default account' resolves to the
    first account, so two unbound Lichess slots would both drive 'alice' -- one
    account on both sides. accounts_conflict must report 'alice'. A regression
    that only compares explicit ids (treating '' as distinct) returns None and
    lets one account play itself.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert accounts_conflict("lichess", "", "lichess", "") == "alice"


def test_accounts_conflict_explicit_vs_default_that_resolve_equal(config_files):
    """An explicit binding conflicts with a 'Default' that resolves to the same id.

    Slot A bound to 'alice' and slot B on 'Default' (-> 'alice') are the same
    account. The rule must catch this mixed explicit/default collision, returning
    'alice'. A regression comparing only raw stored values ('alice' vs '') misses
    it.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert accounts_conflict("lichess", "alice", "lichess", "") == "alice"


def test_accounts_conflict_distinct_accounts_do_not_collide(config_files):
    """Two different accounts of the same type never collide.

    Slot A on 'alice' and slot B on 'bob' are distinct online identities and may
    play each other. A regression (over-broad match on type alone) shows as a
    spurious non-None here.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert accounts_conflict("lichess", "alice", "lichess", "bob") is None


def test_accounts_conflict_different_types_never_collide(config_files):
    """Slots of different types cannot share an online account.

    A human vs a Lichess slot have no shared account space. The type guard must
    short-circuit to None even though the human's empty account and the Lichess
    default might look alike. A regression shows as a false conflict here.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    assert accounts_conflict("human", "", "lichess", "") is None


def test_accounts_conflict_none_when_no_accounts_exist(config_files):
    """Same-type slots with no accounts of that type do not collide.

    Both resolve to None (nothing bound), so there is no shared account to guard.
    A regression that treats two Nones as equal would wrongly report a conflict.
    """
    config_files  # no accounts seeded
    assert accounts_conflict("lichess", "", "lichess", "") is None


def test_selectable_accounts_offline_other_excludes_nothing(config_files):
    """When the other slot is offline (different type), every account stays selectable.

    A human opponent shares no account space with a Lichess slot, so nothing is
    removed and 'Default' remains offered. A regression (excluding on type
    mismatch) shows as a dropped account or default_allowed False here.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    choices = selectable_accounts_for_slot("lichess", "human", "")
    assert choices.default_allowed is True
    assert [a.id for a in choices.accounts] == ["alice", "bob"]


def test_selectable_accounts_excludes_other_explicit_binding(config_files):
    """The account the other slot explicitly bound is removed from this slot's list.

    Other slot on 'bob' -> this slot may pick anything but 'bob', and 'Default'
    (which resolves to 'alice', not 'bob') stays available. A regression shows as
    'bob' still listed or 'Default' wrongly dropped.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    choices = selectable_accounts_for_slot("lichess", "lichess", "bob")
    assert choices.default_allowed is True
    assert [a.id for a in choices.accounts] == ["alice"]


def test_selectable_accounts_drops_default_when_it_would_collide(config_files):
    """A 'Default' other-slot binding removes the first account AND this slot's Default.

    Both matters: the other slot on 'Default' resolves to the first account
    ('alice'), so 'alice' is removed here, and this slot's own 'Default' would
    also resolve to 'alice' -- offering it would silently recreate the collision,
    so default_allowed is False. A regression (comparing only raw '' bindings)
    leaves 'alice' selectable and default_allowed True.
    """
    cfg, _ = config_files
    _seed_two_lichess(cfg)
    choices = selectable_accounts_for_slot("lichess", "lichess", "")
    assert choices.default_allowed is False
    assert [a.id for a in choices.accounts] == ["bob"]


def test_selectable_accounts_no_accounts_allows_default_empty_list(config_files):
    """With no accounts of the type, nothing is taken: Default allowed, empty list.

    An online slot whose type has no saved accounts cannot collide and cannot
    offer any concrete account. A regression (crash on empty, or default_allowed
    False) shows here.
    """
    config_files  # no accounts seeded
    choices = selectable_accounts_for_slot("lichess", "lichess", "")
    assert choices.default_allowed is True
    assert choices.accounts == []
