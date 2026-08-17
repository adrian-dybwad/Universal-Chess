"""Tests for UTC serialization of game timestamps in the web layer.

Games are stored with naive-UTC timestamps. The web layer must hand them to the
browser with an explicit UTC designator (so the browser renders them in the
viewer's local timezone) and must emit standard PGN date/time tags. These guard
the exact wire/PGN shapes that the frontend and PGN consumers depend on.
"""

import calendar
import datetime
import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open


# gamedata tuple layout consumed by build_gameitem_from_gamedata:
# (created_at, source, event, site, round, white, black, result, id)
def _gamedata(created_at):
    return (created_at, "local.py", "Casual", "Board", "1", "White", "Black", "1-0", 42)


def test_gameitem_created_at_is_utc_iso():
    """created_at is serialized as ISO-8601 with a +00:00 UTC designator.

    Why: the stored value is naive UTC; the browser must parse it as UTC to show
    the right local time. How a regression manifests: the field is the old raw
    'YYYY-MM-DD HH:MM:SS' (no designator), so the browser reads it as local time
    and the games list shows a time shifted by the viewer's offset.
    """
    item = webapp.build_gameitem_from_gamedata(_gamedata(datetime.datetime(2026, 7, 10, 1, 22, 33)))
    assert item["created_at"] == "2026-07-10T01:22:33+00:00"


def test_gameitem_created_at_empty_when_missing():
    """A missing created_at serializes to "" (not the string "None").

    Why: the UI omits the timestamp when empty; the literal "None" would render
    as text. How a regression manifests: item["created_at"] == "None".
    """
    item = webapp.build_gameitem_from_gamedata(_gamedata(None))
    assert item["created_at"] == ""


def test_webdav_pgn_properties_use_iso_creationdate():
    """WebDAV creationdate/lastmodified use the ISO UTC string as-is.

    Why: created_at is already ISO UTC; the previous code appended a stray 'Z'
    to a space-separated string. How a regression manifests: a malformed
    '...+00:00Z' creationdate or a doubled/naive lastmodified.
    """
    item = webapp.build_gameitem_from_gamedata(_gamedata(datetime.datetime(2026, 7, 10, 1, 22, 33)))
    xml = webapp.build_pgn_properties_xml(item)
    assert "<D:creationdate>2026-07-10T01:22:33+00:00</D:creationdate>" in xml
    assert "<D:lastmodified>2026-07-10T01:22:33+00:00</D:lastmodified>" in xml
    assert "+00:00Z" not in xml


def test_format_date_iso_is_utc_not_local():
    """format_date_iso renders a Unix timestamp as UTC, matching its 'Z' label.

    Why: the function appends a 'Z' (UTC) designator, so it must format the
    instant in UTC (gmtime), not local time. How a regression manifests: a
    localtime-based implementation labels local wall-clock as 'Z', so a WebDAV
    client parsing creationdate gets an instant shifted by the machine's offset.
    We assert against the timezone-independent gmtime expectation for a fixed
    epoch so the test does not depend on the runner's zone.
    """
    import time as _time
    # 2026-07-10T01:22:33Z as a Unix timestamp (calendar_timegm is UTC-based).
    ts = calendar.timegm((2026, 7, 10, 1, 22, 33, 0, 0, 0))
    assert webapp.format_date_iso(ts) == _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts))
    assert webapp.format_date_iso(ts) == "2026-07-10T01:22:33Z"


def test_format_date_rfc_is_gmt():
    """format_date_rfc renders a Unix timestamp as an RFC 1123 GMT string.

    Why: HTTP dates must be GMT. How a regression manifests: a localtime/%Z
    implementation emits a local zone abbreviation and wall-clock, off by the
    device offset and non-conformant for Last-Modified.
    """
    ts = calendar.timegm((2026, 7, 10, 1, 22, 33, 0, 0, 0))
    assert webapp.format_date_rfc(ts) == "Fri, 10 Jul 2026 01:22:33 GMT"


def test_pgn_headers_have_standard_utc_date_tags(private_db_session):
    """build_chess_game_from_id emits [Date]/[UTCDate]/[UTCTime] in UTC.

    Why: PGN readers expect [Date "YYYY.MM.DD"], not a raw datetime string; the
    UTC tags record the exact instant unambiguously. How a regression manifests:
    [Date] carries the old 'YYYY-MM-DD HH:MM:SS...' string or the UTC tags are
    absent.

    Uses ``private_db_session`` rather than ``webapp.get_db_session()``: the
    shared engine is an in-memory SQLite whose schema lives only as long as the
    connection that built it, so a preceding test that opened connections from
    threads could evict it and fail this test with "no such table: game" for
    reasons that have nothing to do with PGN headers.
    """
    from universalchess.db import models

    game = models.Game(source="local.py", white="W", black="B", result="1-0",
                        created_at=datetime.datetime(2026, 7, 10, 1, 22, 33))
    private_db_session.add(game)
    private_db_session.commit()

    g = webapp.build_chess_game_from_id(private_db_session, game.id)
    assert g.headers["Date"] == "2026.07.10"
    assert g.headers["UTCDate"] == "2026.07.10"
    assert g.headers["UTCTime"] == "01:22:33"
