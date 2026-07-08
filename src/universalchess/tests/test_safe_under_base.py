"""Tests for safe_under_base, the shared path-traversal guard.

safe_under_base is the single sanitizer every request-derived filesystem path
routes through (WebDAV, static serving, the React catch-all, uploads, and
resource loading). These tests pin the containment contract directly so a
regression in the helper is caught here rather than as a traversal in a route.

The guard returns an os.path.realpath-normalized absolute path string (or None
on escape), using the os.path.realpath + startswith pattern that static analysis
recognizes as a path sanitizer.
"""

import os

import pytest

from universalchess.utils.safe_path import safe_under_base


@pytest.fixture
def base(tmp_path):
    """A real directory with in-bounds files to resolve against."""
    (tmp_path / "ok.txt").write_text("ok")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("deep")
    return tmp_path


def _real(path):
    return os.path.realpath(str(path))


def test_returns_contained_path_for_plain_name(base):
    """A simple in-bounds name resolves to a path inside the base.

    Regression: if containment were broken the result would point outside base
    and downstream open/send_file would serve the wrong file.
    """
    assert safe_under_base(base, "ok.txt") == _real(base / "ok.txt")


def test_allows_nested_subdirectory(base):
    """Multi-segment in-bounds paths (e.g. assets/app.js) stay allowed.

    Static routes pass <path:filename> which may include a subdirectory; this
    must not be mistaken for traversal. Failure would 404 legitimate assets.
    """
    assert safe_under_base(base, "sub/deep.txt") == _real(base / "sub" / "deep.txt")


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
    assert safe_under_base(base, evil) is None


@pytest.mark.parametrize("payload", ["/etc/passwd", "//etc/passwd"])
def test_absolute_payload_is_contained_not_escaped(base, payload):
    """A leading-slash payload is treated as base-relative, never an escape.

    os.path.join(base, "/etc") discards base and yields "/etc"; stripping
    leading separators maps "/etc/passwd" to base/etc/passwd, which stays
    inside base. The key property is that the result never points outside base,
    so an attacker cannot read /etc/passwd.
    """
    result = safe_under_base(base, payload)
    assert result == _real(base / "etc" / "passwd")
    assert result.startswith(_real(base) + os.sep)


@pytest.mark.parametrize("bad", [None, ""])
def test_rejects_empty_parts(base, bad):
    """Missing/empty input returns None rather than the bare base directory.

    A missing filename must not silently resolve to the base dir (which would
    let, e.g., send_file target a directory or the wrong default).
    """
    assert safe_under_base(base, bad) is None


def test_rejects_when_no_parts_given(base):
    """Calling with no parts at all returns None, not the base directory.

    Guards against a caller accidentally passing through to the base dir when it
    meant to supply a filename.
    """
    assert safe_under_base(base) is None


def test_multiple_parts_joined_under_base(base):
    """Parts are joined in order beneath base (base / 'sub' / 'deep.txt').

    Confirms the variadic form used by static routes (base, subdir, filename)
    composes the same contained path as a single joined string.
    """
    assert safe_under_base(base, "sub", "deep.txt") == _real(base / "sub" / "deep.txt")


def test_absolute_second_part_does_not_reset(base):
    """An absolute later segment cannot reset the join out of base.

    os.path.join(a, '/x') returns '/x'; safe_under_base strips the leading
    separator per part so an attacker-supplied '/deep.txt' second segment stays
    contained, never escaping base.
    """
    assert safe_under_base(base, "sub", "/deep.txt") == _real(base / "sub" / "deep.txt")


def test_sibling_prefix_is_not_treated_as_contained(tmp_path):
    """A sibling dir sharing a name prefix must not pass containment.

    base '/x/pi' must not accept '/x/pi-evil'. Without the os.sep-anchored
    check, a startswith('/x/pi') test would wrongly allow the sibling. Here the
    traversal resolves to the sibling and must return None.
    """
    base = tmp_path / "pi"
    base.mkdir()
    (tmp_path / "pi-evil").mkdir()
    assert safe_under_base(base, "../pi-evil") is None
