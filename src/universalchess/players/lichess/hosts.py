"""Lichess hosts: the plugin's list of API servers.

Lichess is a provider with its own protocol (berserk, board stream, seek). It
owns this list. A credential is valid on one host; identity is fetched from
that host with that token. A future extra Lichess API is another row here, not
a boolean and not a generic catalog account type.

Chess.com (or any other provider) does not use this module.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from universalchess.services.account_store import normalize_account_id

HOST_ORG = "org"
HOST_DEV = "dev"
DEFAULT_HOST_ID = HOST_ORG
# Player type / account_store type id for this plugin. Not a per-host type.
ACCOUNT_TYPE_LICHESS = "lichess"


@dataclass(frozen=True)
class LichessHost:
    """One Lichess API server the plugin can talk to."""

    id: str
    label: str
    base_url: str


LICHESS_HOSTS: Tuple[LichessHost, ...] = (
    LichessHost(id=HOST_ORG, label="lichess.org", base_url="https://lichess.org"),
    LichessHost(id=HOST_DEV, label="lichess.dev", base_url="https://lichess.dev"),
)

HOST_BY_ID: Dict[str, LichessHost] = {host.id: host for host in LICHESS_HOSTS}


def get_host(host_id: str) -> LichessHost:
    """Return the host with ``host_id``.

    Raises:
        ValueError: if ``host_id`` is not in :data:`LICHESS_HOSTS`.
    """
    try:
        return HOST_BY_ID[host_id]
    except KeyError as exc:
        raise ValueError(f"unknown Lichess host {host_id!r}") from exc


def hosts_as_dicts() -> List[dict]:
    """JSON shape for the web Add Account host picker (id, label, baseUrl)."""
    return [
        {"id": host.id, "label": host.label, "baseUrl": host.base_url}
        for host in LICHESS_HOSTS
    ]


def credential_id(host_id: str, username: str) -> str:
    """Opaque player-slot id: ``org:alice``. Unique across Lichess hosts."""
    return f"{host_id}:{normalize_account_id(username)}"


def parse_credential_id(account_id: str) -> Tuple[str, str]:
    """Return ``(host_id, username)`` for a Lichess credential id.

    Canonical ids are ``{host}:{username}`` (``org:alice``). A bare username
    (legacy store or player binding) is the default host (org). An unknown
    host prefix is treated as a username on org so a typo cannot select a
    host the plugin does not implement.
    """
    raw = (account_id or "").strip().lower()
    if not raw:
        return DEFAULT_HOST_ID, ""
    host_id, sep, username = raw.partition(":")
    if sep and host_id in HOST_BY_ID and username:
        return host_id, username
    return DEFAULT_HOST_ID, raw


def credential_label(host_id: str, username: str) -> str:
    """Chooser text: ``lichess.org:Alice`` (server:user)."""
    host = HOST_BY_ID.get(host_id)
    server = host.label if host is not None else host_id
    return f"{server}:{username}"
