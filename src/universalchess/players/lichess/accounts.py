"""Lichess credential persistence: tokens keyed by host:user.

The generic account store holds INI sections. This module is the Lichess
plugin's use of that store: type is always ``lichess``, id is
``org:alice`` / ``dev:bob``, and host/token/username/range live in the section.

Identity is resolved against the credential's host (the token is fetched from
that server). Org Alice and .dev Alice are two rows.
"""

from typing import Dict, List, Optional

from universalchess.board.settings import Settings
from universalchess.services import account_store
from universalchess.services.account_store import (
    Account,
    AddAccountResult,
    Resolver,
    get_account,
    list_accounts,
    save_account,
)
from .hosts import (
    ACCOUNT_TYPE_LICHESS,
    DEFAULT_HOST_ID,
    HOST_BY_ID,
    HOST_DEV,
    HOST_ORG,
    LICHESS_HOSTS,
    credential_id,
    credential_label,
    get_host,
    parse_credential_id,
)

_PLAYER_SECTIONS = ("PlayerOne", "PlayerTwo")


def host_id_of(account: Account) -> str:
    """Host this credential authenticates against (stored, else parsed from id)."""
    stored = (account.get("host") or "").strip()
    if stored in HOST_BY_ID:
        return stored
    host_id, _username = parse_credential_id(account.id)
    return host_id


def username_of(account: Account) -> str:
    """Display username; falls back to the id's user part."""
    identity = account.get("username") or ""
    if identity:
        return identity
    _host_id, username = parse_credential_id(account.id)
    return username


def label_of(account: Account) -> str:
    """Chooser label ``lichess.org:Alice``."""
    return credential_label(host_id_of(account), username_of(account) or account.id)


def list_lichess_credentials(*, config=None) -> List[Account]:
    """Lichess credentials, org first then other hosts, then username."""
    host_order = {host.id: index for index, host in enumerate(LICHESS_HOSTS)}

    def sort_key(account: Account):
        host_id = host_id_of(account)
        return (host_order.get(host_id, len(host_order)), username_of(account).lower())

    return sorted(list_accounts(ACCOUNT_TYPE_LICHESS, config=config), key=sort_key)


def default_lichess_credential(*, config=None) -> Optional[Account]:
    """First credential in :func:`list_lichess_credentials` order (org before dev)."""
    accounts = list_lichess_credentials(config=config)
    return accounts[0] if accounts else None


def get_lichess_credential(account_id: str, *, config=None) -> Optional[Account]:
    """Find a credential by player-slot id.

    Accepts canonical ``org:alice`` and legacy username-only ``alice`` (org).
    A ``dev:bob`` binding never returns an org Bob section: tokens are host-
    specific.
    """
    raw = (account_id or "").strip()
    if not raw:
        return None
    found = get_account(ACCOUNT_TYPE_LICHESS, raw, config=config)
    if found is not None:
        return found
    host_id, username = parse_credential_id(raw)
    canonical = credential_id(host_id, username)
    if canonical != raw:
        found = get_account(ACCOUNT_TYPE_LICHESS, canonical, config=config)
        if found is not None:
            return found
    if host_id == DEFAULT_HOST_ID and username and username != canonical:
        return get_account(ACCOUNT_TYPE_LICHESS, username, config=config)
    return None


def add_lichess_credential(
    fields: Dict[str, str],
    *,
    resolver: Optional[Resolver],
    config=None,
) -> AddAccountResult:
    """Validate, resolve against the chosen host, and persist a Lichess credential.

    ``fields`` must include ``api_token`` and may include ``host`` (default org)
    and ``range``. The resolver authenticates on that host and returns the
    username. Duplicate is ``host`` + username, so the same person may exist
    on org and .dev.
    """
    host_id = (fields.get("host") or DEFAULT_HOST_ID).strip().lower()
    if host_id not in HOST_BY_ID:
        return AddAccountResult(None, "unknown_host", f"Unknown Lichess host {host_id}")
    token = (fields.get("api_token") or "").strip()
    if not token:
        return AddAccountResult(None, "missing_field", "API Token is required")
    if resolver is None:
        return AddAccountResult(None, "no_resolver", "Cannot verify this account type")
    resolved = resolver({**fields, "host": host_id})
    if resolved.error:
        return AddAccountResult(
            None, resolved.error, resolved.message or "Could not verify account"
        )
    identity = (resolved.identity or "").strip()
    if not identity:
        return AddAccountResult(None, "missing_identity", "Account identifier is required")

    account_id = credential_id(host_id, identity)
    if get_account(ACCOUNT_TYPE_LICHESS, account_id, config=config) is not None:
        return AddAccountResult(
            None, "duplicate", f"An account named {identity} already exists on {get_host(host_id).label}"
        )

    values: Dict[str, str] = {
        "api_token": token,
        "username": identity,
        "host": host_id,
    }
    rating_range = fields.get("range")
    if rating_range is not None and str(rating_range).strip():
        values["range"] = str(rating_range).strip()
    for key, extra in resolved.extra_values.items():
        values[key] = str(extra)

    account = Account(type=ACCOUNT_TYPE_LICHESS, id=account_id, values=values)
    save_account(account)
    return AddAccountResult(account, None, "")


def _copy_section(config, old_section: str, new_section: str, values: Dict[str, str]) -> None:
    if not config.has_section(new_section):
        config.add_section(new_section)
    else:
        for stale in list(config[new_section].keys()):
            config.remove_option(new_section, stale)
    for key, value in values.items():
        config.set(new_section, key, str(value))
    if old_section != new_section and config.has_section(old_section):
        config.remove_section(old_section)


def migrate_lichess_layout() -> None:
    """Rewrite legacy Lichess sections and player bindings to host:user ids.

    - ``[account:lichess_dev:<user>]`` → ``[account:lichess:dev:<user>]``
    - ``[account:lichess:<user>]`` (no host prefix) → ``[account:lichess:org:<user>]``
    - Player ``account=<user>`` → ``org:<user>``, or ``dev:<user>`` when the
      leftover ``game.lichess_use_dev`` flag is true (one-time; the flag is
      no longer the host selector).

    Idempotent. Skips a rename when the target section already exists (keeps
    the target, drops the legacy section).
    """
    config = Settings.get_config()
    changed = False

    for section in list(config.sections()):
        parsed = account_store.parse_section(section)
        if parsed is None:
            continue
        type_id, account_id = parsed
        if type_id != "lichess_dev":
            continue
        new_id = credential_id(HOST_DEV, account_id)
        new_section = account_store.section_name(ACCOUNT_TYPE_LICHESS, new_id)
        values = dict(config.items(section))
        values["host"] = HOST_DEV
        if not values.get("username"):
            values["username"] = account_id
        if config.has_section(new_section) and section != new_section:
            config.remove_section(section)
        else:
            _copy_section(config, section, new_section, values)
        changed = True

    for section in list(config.sections()):
        parsed = account_store.parse_section(section)
        if parsed is None:
            continue
        type_id, account_id = parsed
        if type_id != ACCOUNT_TYPE_LICHESS:
            continue
        host_id, username = parse_credential_id(account_id)
        canonical = credential_id(host_id, username)
        values = dict(config.items(section))
        if account_id == canonical:
            if not (values.get("host") or "").strip():
                config.set(section, "host", host_id)
                changed = True
            continue
        # Bare username (or unknown prefix treated as username on org).
        new_section = account_store.section_name(ACCOUNT_TYPE_LICHESS, canonical)
        values["host"] = host_id
        if not values.get("username"):
            values["username"] = account_id
        if config.has_section(new_section) and section != new_section:
            config.remove_section(section)
        else:
            _copy_section(config, section, new_section, values)
        changed = True

    use_dev = False
    if config.has_section("game"):
        use_dev = config.get("game", "lichess_use_dev", fallback="").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    default_host = HOST_DEV if use_dev else HOST_ORG
    for player_section in _PLAYER_SECTIONS:
        if not config.has_section(player_section):
            continue
        if config.get(player_section, "type", fallback="") != "lichess":
            continue
        account = config.get(player_section, "account", fallback="").strip()
        if not account:
            continue
        host_id, username = parse_credential_id(account)
        if account.lower() == username:
            host_id = default_host
        new_id = credential_id(host_id, username)
        if new_id != account:
            config.set(player_section, "account", new_id)
            changed = True

    if changed:
        Settings.write_config(config)
