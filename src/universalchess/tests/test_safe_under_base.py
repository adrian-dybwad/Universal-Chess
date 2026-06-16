"""Tests for safe_under_base, the shared path-traversal guard in web/app.py.

safe_under_base is the single sanitizer every request-derived filesystem path
routes through (WebDAV, static asset/icon/stockfish serving, the React
catch-all, and uploads). These tests pin the containment contract directly so a
regression in the helper is caught here rather than as a traversal in a route.
"""

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


@pytest.fixture
def base(tmp_path):
    """A real directory with one in-bounds file to resolve against."""
    (tmp_path / "ok.txt").write_text("ok")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("deep")
    return tmp_path


def test_returns_contained_path_for_plain_name(base):
    """A simple in-bounds name resolves to a path inside the base.

    Regression: if containment were broken the result would point outside base
    and downstream open/send_file would serve the wrong file.
    """
    result = webapp.safe_under_base(base, "ok.txt")
    assert result == (base / "ok.txt").resolve()


def test_allows_nested_subdirectory(base):
    """Multi-segment in-bounds paths (e.g. assets/app.js) stay allowed.

    Static routes pass <path:filename> which may include a subdirectory; this
    must not be mistaken for traversal. Failure would 404 legitimate assets.
    """
    result = webapp.safe_under_base(base, "sub/deep.txt")
    assert result == (base / "sub" / "deep.txt").resolve()


@pytest.mark.parametrize(
    "evil",
    [
        "../secret",
        "../../etc/passwd",
        "sub/../../escape",
    ],
)
def test_rejects_traversal(base, evil):
    """'..' payloads that resolve outside base return None (no escape).

    Manifestation if broken: '..' would resolve outside base and the caller
    would read/write/delete an arbitrary file. None forces the route to
    403/404 instead.
    """
    assert webapp.safe_under_base(base, evil) is None


@pytest.mark.parametrize("payload", ["/etc/passwd", "//etc/passwd"])
def test_absolute_payload_is_contained_not_escaped(base, payload):
    """A leading-slash payload is treated as base-relative, never an escape.

    pathlib resets the join on an absolute component (Path('/home') / '/etc'
    -> '/etc'); stripping leading slashes maps '/etc/passwd' to
    base/etc/passwd, which stays inside base. The key property is that the
    result never points outside base, so an attacker cannot read /etc/passwd.
    """
    result = webapp.safe_under_base(base, payload)
    assert result == (base / "etc" / "passwd").resolve()
    assert result.is_relative_to(base.resolve())


@pytest.mark.parametrize("bad", [None, ""])
def test_rejects_empty_parts(base, bad):
    """Missing/empty input returns None rather than the bare base directory.

    A missing filename must not silently resolve to the base dir (which would
    let, e.g., send_file target a directory or the wrong default).
    """
    assert webapp.safe_under_base(base, bad) is None


def test_multiple_parts_joined_under_base(base):
    """Parts are joined in order beneath base (base / 'sub' / 'deep.txt').

    Confirms the variadic form used by static routes (base, subdir, filename)
    composes the same contained path as a single joined string.
    """
    result = webapp.safe_under_base(base, "sub", "deep.txt")
    assert result == (base / "sub" / "deep.txt").resolve()


def test_absolute_second_part_does_not_reset(base):
    """An absolute later segment cannot reset the join out of base.

    pathlib treats Path(a) / '/x' as '/x'; safe_under_base strips the leading
    slash per part so an attacker-supplied '/etc/passwd' second segment stays
    contained or is rejected, never escaping base.
    """
    result = webapp.safe_under_base(base, "sub", "/deep.txt")
    assert result == (base / "sub" / "deep.txt").resolve()
