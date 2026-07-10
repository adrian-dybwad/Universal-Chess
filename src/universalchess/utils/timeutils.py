"""UTC datetime helpers.

Single source of truth for the app's timestamp convention:

* Every timestamp is stored in the database as **naive UTC**. SQLAlchemy's
  SQLite ``DateTime`` stores a datetime's fields verbatim (ignoring tzinfo), so a
  naive value keeps the stored string unambiguous while its fields are UTC. This
  is independent of the device's OS timezone (which the user can change), so
  stored timestamps never drift when the clock's zone changes.
* Timestamps are serialized to clients with an explicit UTC designator
  (``+00:00``). The web UI parses them with ``new Date(...)`` and renders them in
  the viewer's own browser timezone, so the wire value must be unambiguously UTC.
"""

from datetime import datetime, timezone
from typing import Optional


def utcnow_naive() -> datetime:
    """Return the current UTC time as a naive datetime (no tzinfo).

    Used as the ORM default for timestamp columns so inserts are UTC on every
    database engine, not just SQLite's UTC ``CURRENT_TIMESTAMP`` -- and so they
    stay UTC regardless of the device's configured OS timezone.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to an ISO-8601 string with a UTC (+00:00) designator.

    A naive value is assumed to be UTC (the storage convention); an aware value
    is converted to UTC first. Returns None for None so a missing timestamp stays
    missing rather than being fabricated.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def utcnow_iso() -> str:
    """Return the current time as an ISO-8601 string with a UTC (+00:00) designator.

    For timestamps that are handed straight to a client (e.g. the update "last
    checked" field). Using this instead of ``datetime.utcnow().isoformat()`` -- a
    naive string with *no* designator -- is what lets the browser parse the value
    as UTC and render it in the viewer's local timezone; a naive string is read as
    browser-local, so the displayed digits stay UTC (the bug this guards against).
    """
    return datetime.now(timezone.utc).isoformat()
