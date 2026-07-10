"""Tests for UTC datetime helpers.

The app stores all timestamps in the database as naive UTC and must serialize
them to clients with an explicit UTC designator so the browser (which parses the
string with ``new Date(...)``) interprets them as UTC and renders them in the
viewer's local timezone. These helpers are the single place that convention
lives, so they are tested directly.
"""

from datetime import datetime, timezone, timedelta

import pytest

from universalchess.utils.timeutils import to_utc_iso, utcnow_iso, utcnow_naive


def test_utcnow_naive_is_naive_and_utc():
    """utcnow_naive() returns a tzinfo-free datetime holding the UTC wall clock.

    Why: it is used as the ORM column default, and SQLAlchemy's SQLite DateTime
    stores a datetime's fields verbatim (ignoring tzinfo). A naive value keeps
    storage unambiguous while its fields must be UTC, not local. How a regression
    manifests: the value carries tzinfo (offset leaks into stored strings) or
    reflects local time (stored timestamps drift by the machine's offset).
    """
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    value = utcnow_naive()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    assert value.tzinfo is None
    # The wall-clock fields must sit within the UTC "now" window measured around
    # the call; a local-time implementation would fall outside it by the offset
    # (except in UTC zones).
    assert before <= value <= after


def test_to_utc_iso_marks_naive_utc_with_offset():
    """A naive datetime (assumed UTC) serializes with a +00:00 UTC designator.

    Why: the stored value is naive UTC; without a designator the browser parses
    it as local time and shows the wrong clock. How a regression manifests: the
    output lacks +00:00/Z, so round-tripping through ``new Date(...)`` shifts the
    displayed time by the viewer's offset.
    """
    dt = datetime(2026, 7, 10, 1, 22, 33)
    assert to_utc_iso(dt) == "2026-07-10T01:22:33+00:00"


def test_to_utc_iso_converts_aware_datetime_to_utc():
    """An aware datetime is converted to UTC before formatting.

    Why: some sources may hand in an offset-aware value; the serialized wire
    format must always be UTC so the client's local-time conversion is correct.
    How a regression manifests: the original offset leaks through, so the client
    double-applies an offset.
    """
    dt = datetime(2026, 7, 10, 3, 22, 33, tzinfo=timezone(timedelta(hours=2)))
    # 03:22:33+02:00 == 01:22:33 UTC
    assert to_utc_iso(dt) == "2026-07-10T01:22:33+00:00"


def test_utcnow_iso_has_explicit_utc_designator():
    """utcnow_iso() ends with a +00:00 (or Z) UTC designator, not a naive string.

    Why: this feeds fields handed straight to the browser (e.g. update
    "last checked"). ``datetime.utcnow().isoformat()`` produces a *naive* string
    with no designator, which the browser reads as local time -- so the displayed
    digits stay UTC instead of converting. How a regression manifests: the output
    has no trailing offset/Z, reproducing that off-by-offset display bug.
    """
    value = utcnow_iso()
    assert value.endswith("+00:00") or value.endswith("Z")
    # Must parse back to an aware UTC instant (proves the designator is real).
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_to_utc_iso_returns_none_for_none():
    """None in -> None out (no fabricated timestamp).

    Why: a missing timestamp must stay missing so the UI can omit it, rather than
    inventing a plausible-but-wrong value. How a regression manifests: the helper
    returns a string like the epoch or 'None', which the UI would render as a
    real date.
    """
    assert to_utc_iso(None) is None
