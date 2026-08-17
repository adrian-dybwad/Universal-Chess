"""Multi-account store for online player accounts.

Each saved online account (e.g. a Lichess login) is persisted as one INI section
``[account:<type>:<id>]`` in ``centaur.ini``. ``<type>`` is an account type id
from the catalog's ``accountTypes`` definition; ``<id>`` is the account's
normalized identity (for Lichess, the lowercased username). This replaces the
former single global ``[lichess]`` credential so a board can hold several
accounts and play as whichever one the lobby names.

Design:
- Reads are pure over a ``configparser.ConfigParser`` (injectable for tests);
  writes go through the atomic :class:`Settings` persistence boundary.
- Identity resolution (authenticating a Lichess token to learn its username) is
  a network side effect and is therefore *injected* as a resolver callable, so
  this module stays free of HTTP and is unit-testable with a fake.

Uniqueness is enforced on the normalized id: two accounts of the same type may
not share a player name (case-insensitively), which is the rule the Add Account
flow surfaces to the user.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from universalchess.board.settings import Settings

# INI section prefix marking a stored account. The delimiter is ':' because
# account type ids and normalized ids (Lichess usernames are [A-Za-z0-9_-]) never
# contain a colon, so ``account:<type>:<id>`` parses unambiguously.
SECTION_PREFIX = "account:"

# Lichess ships a 'tokenhere' placeholder meaning "unset"; it is never a real
# credential and must not be migrated or treated as a connected account.
_TOKEN_PLACEHOLDER = "tokenhere"  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret


@dataclass
class Account:
    """A single stored online account.

    ``id`` is the normalized identity used as the section id and uniqueness key.
    ``values`` holds every persisted field (including secrets like the token and
    the identity field itself), exactly as read from / written to the INI.
    """

    type: str
    id: str
    values: Dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        """Return a stored field value, or ``default`` when absent."""
        return self.values.get(key, default)


@dataclass
class ResolvedIdentity:
    """Outcome of resolving an account's identity from its input fields.

    For a ``resolved`` identity type, a resolver authenticates the credential and
    returns the canonical identity (e.g. the Lichess username). ``error`` is a
    short machine code (``auth_failed``/``network``/...) when resolution fails, in
    which case no account should be created.
    """

    identity: str = ""
    extra_values: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    message: str = ""


@dataclass
class AddAccountResult:
    """Result of :func:`add_account`: the created account or an error code.

    ``error`` is None on success. Known codes: ``missing_field`` (required field
    blank), ``no_resolver`` (resolved-identity type with no resolver supplied),
    ``missing_identity`` (entered identity blank), ``duplicate`` (id already
    exists), or any code returned by the resolver (e.g. ``auth_failed``).
    """

    account: Optional[Account]
    error: Optional[str]
    message: str = ""


# Resolver: given the submitted field values, return the resolved identity.
Resolver = Callable[[Dict[str, str]], ResolvedIdentity]


def section_name(type_id: str, account_id: str) -> str:
    """Return the INI section name for an account of ``type_id`` with ``account_id``."""
    return f"{SECTION_PREFIX}{type_id}:{account_id}"


def parse_section(section: str) -> Optional[Tuple[str, str]]:
    """Return ``(type_id, account_id)`` for an account section, else None.

    Non-account sections (``game``, ``lichess``, ``PlayerOne``, ...) and
    malformed account sections (empty type or id) return None so listing never
    mistakes them for accounts.
    """
    if not section.startswith(SECTION_PREFIX):
        return None
    rest = section[len(SECTION_PREFIX):]
    type_id, sep, account_id = rest.partition(":")
    if not sep or not type_id or not account_id:
        return None
    return type_id, account_id


def normalize_account_id(identity: str) -> str:
    """Normalize an identity into a stable, comparable account id.

    Lowercased and stripped so that identities differing only in case/whitespace
    (e.g. Lichess is case-insensitive) map to one account. Returns '' for an
    empty/whitespace identity, which callers treat as "no identity".
    """
    return (identity or "").strip().lower()


def list_accounts(type_id: Optional[str] = None, *, config=None) -> List[Account]:
    """Return stored accounts, optionally filtered to one type, sorted by (type, id).

    Reads the live config unless ``config`` is supplied (tests pass a prepared
    parser). Only ``account:*`` sections are returned.
    """
    config = config if config is not None else Settings.get_config()
    accounts: List[Account] = []
    for section in config.sections():
        parsed = parse_section(section)
        if parsed is None:
            continue
        sec_type, sec_id = parsed
        if type_id is not None and sec_type != type_id:
            continue
        accounts.append(Account(type=sec_type, id=sec_id, values=dict(config.items(section))))
    return sorted(accounts, key=lambda a: (a.type, a.id))


def get_account(type_id: str, account_id: str, *, config=None) -> Optional[Account]:
    """Return the account with ``(type_id, account_id)``, or None if absent."""
    config = config if config is not None else Settings.get_config()
    section = section_name(type_id, account_id)
    if not config.has_section(section):
        return None
    return Account(type=type_id, id=account_id, values=dict(config.items(section)))


def default_account(type_id: str, *, config=None) -> Optional[Account]:
    """Return the first account of ``type_id`` (used by back-compat single-token reads)."""
    accounts = list_accounts(type_id, config=config)
    return accounts[0] if accounts else None


def save_account(account: Account) -> None:
    """Persist an account, replacing its section's keys atomically.

    Reads the live config, rewrites only this account's section (clearing stale
    keys first so a removed field does not linger), and writes atomically so
    other sections are never clobbered.
    """
    config = Settings.get_config()
    section = section_name(account.type, account.id)
    if not config.has_section(section):
        config.add_section(section)
    else:
        for stale_key in list(config[section].keys()):
            config.remove_option(section, stale_key)
    for key, value in account.values.items():
        config.set(section, key, str(value))
    Settings.write_config(config)


def delete_account(type_id: str, account_id: str) -> bool:
    """Delete an account's section. Return True if it existed, False otherwise."""
    config = Settings.get_config()
    section = section_name(type_id, account_id)
    if not config.has_section(section):
        return False
    config.remove_section(section)
    Settings.write_config(config)
    return True


def add_account(
    account_type: dict,
    fields: Dict[str, str],
    *,
    resolver: Optional[Resolver] = None,
    config=None,
) -> AddAccountResult:
    """Validate, resolve, and persist a new account from submitted fields.

    Steps (each maps to an :class:`AddAccountResult` error code on failure):
    1. Every field marked ``required`` must be non-blank (``missing_field``).
    2. Determine identity: for ``resolved`` types call ``resolver`` (``no_resolver``
       if none supplied; the resolver's own code, e.g. ``auth_failed``, on
       failure); for ``entered`` types read the identity field (``missing_identity``).
    3. Reject a normalized id that already exists (``duplicate``) - this is the
       "no two accounts share a player name" rule.
    4. Persist only the declared fields plus the identity (and any resolver extras).
    """
    type_id = account_type["id"]

    for fld in account_type["fields"]:
        if fld.get("required") and not (fields.get(fld["key"]) or "").strip():
            return AddAccountResult(None, "missing_field", f"{fld['label']} is required")

    if account_type["identitySource"] == "resolved":
        if resolver is None:
            return AddAccountResult(None, "no_resolver", "Cannot verify this account type")
        resolved = resolver(fields)
        if resolved.error:
            return AddAccountResult(None, resolved.error, resolved.message or "Could not verify account")
        identity = resolved.identity
        extra_values = resolved.extra_values
    else:
        identity = (fields.get(account_type["identityField"]) or "").strip()
        extra_values = {}

    account_id = normalize_account_id(identity)
    if not account_id:
        return AddAccountResult(None, "missing_identity", "Account identifier is required")

    if get_account(type_id, account_id, config=config) is not None:
        return AddAccountResult(None, "duplicate", f"An account named {identity} already exists")

    values: Dict[str, str] = {}
    for fld in account_type["fields"]:
        value = fields.get(fld["key"])
        if value is not None:
            values[fld["key"]] = str(value)
    values[account_type["identityField"]] = identity
    for key, value in extra_values.items():
        values[key] = str(value)

    account = Account(type=type_id, id=account_id, values=values)
    save_account(account)
    return AddAccountResult(account, None, "")


def migrate_legacy_lichess(
    account_type: dict,
    *,
    resolver: Optional[Resolver] = None,
) -> Optional[Account]:
    """Move a legacy single ``[lichess]`` credential into an account, once.

    The former model stored one token/username/range in ``[lichess]``. This
    promotes it to a normal ``account:lichess:<host>:<username>`` and clears the
    legacy token/username so the account becomes the single source of truth
    (preventing two-source drift). Host is org, or ``dev`` when leftover
    ``game.lichess_use_dev`` is set (the same choice player-binding migration
    uses). It is a no-op when:
    - there is no legacy token, or it is the ``tokenhere`` placeholder;
    - any Lichess account already exists (already migrated); or
    - the identity is unknown offline (no cached username and no working
      resolver) - migrating under a guessed id would create a bogus account, so
      the legacy section is left for the back-compat reader instead.

    Idempotent and safe to call on every startup.
    """
    config = Settings.get_config()
    if not config.has_section("lichess"):
        return None
    token = config.get("lichess", "api_token", fallback="").strip()
    if not token or token == _TOKEN_PLACEHOLDER:
        return None
    if list_accounts("lichess", config=config):
        return None

    from universalchess.players.lichess.accounts import legacy_lichess_host_id
    from universalchess.players.lichess.hosts import credential_id

    host_id = legacy_lichess_host_id(config)
    identity = config.get("lichess", "username", fallback="").strip()
    if not identity and resolver is not None:
        resolved = resolver({"api_token": token, "host": host_id})
        if not resolved.error:
            identity = resolved.identity
    if not identity:
        return None

    rating_range = config.get("lichess", "range", fallback="")
    values = {
        "api_token": token,
        account_type["identityField"]: identity,
        "host": host_id,
    }
    if rating_range:
        values["range"] = rating_range
    account = Account(
        type="lichess",
        id=credential_id(host_id, identity),
        values=values,
    )
    save_account(account)

    # Clear the legacy credential so the new account is the only source.
    config = Settings.get_config()
    if config.has_section("lichess"):
        config.set("lichess", "api_token", "")
        config.set("lichess", "username", "")
        Settings.write_config(config)
    return account


def ensure_lichess_migrated(resolver: Optional[Resolver] = None) -> Optional[Account]:
    """Migrate a legacy ``[lichess]`` credential using the packaged definition.

    Convenience entry point for startup / first account read: looks up the
    Lichess account type from the catalog and runs :func:`migrate_legacy_lichess`.
    A no-op (returns None) when the catalog declares no Lichess type or there is
    nothing to migrate. Idempotent, so callers may invoke it freely.
    """
    from universalchess.menus.catalog import get_catalog

    catalog = get_catalog()
    if not catalog.has_account_type("lichess"):
        return None
    account = migrate_legacy_lichess(catalog.account_type("lichess"), resolver=resolver)
    from universalchess.players.lichess.accounts import migrate_lichess_layout

    migrate_lichess_layout()
    return account
