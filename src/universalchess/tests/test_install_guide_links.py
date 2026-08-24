"""Tests for the links between the README, the install guide, and release notes.

Root cause these guard
----------------------
The install procedure used to live in the README, and two things pointed into
it: the release-notes templates linked to its headings by anchor, and the
README itself is bundled into the web app and rendered as the device's home
page (``web-app/vite.config.ts`` reads it at build time; ``About.tsx`` renders
it). Moving the procedure to ``docs/install.md`` breaks both classes of link
silently -- a stale anchor still renders as a working link and only fails when
a user clicks it, and a repo-relative path resolves against the device's own
host, where nothing serves it.

The invariants:
 - every documentation link naming ``docs/install.md`` points at a file that
   exists, and any fragment it carries names a heading in that file;
 - the install guide's own relative links resolve on disk;
 - the README links only to absolute URLs, because it is rendered off-repo.
"""

import re
from pathlib import Path

import pytest

# Repo layout: .../src/universalchess/tests/<this file> -> root is three up.
REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
INSTALL_GUIDE = REPO_ROOT / "docs" / "install.md"
RELEASE_NOTES = REPO_ROOT / ".github" / "release-notes"

INSTALL_GUIDE_REPO_PATH = "docs/install.md"

# Markdown inline links: the target is everything up to the closing paren, with
# any optional link title dropped.
_LINK = re.compile(r"\]\(([^)\s]+)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)


def _link_targets(markdown: str) -> list[str]:
    return _LINK.findall(markdown)


def _github_slug(heading: str) -> str:
    """The anchor GitHub derives from a heading: lowercased, punctuation
    dropped, spaces hyphenated. Markdown emphasis and inline code markers are
    stripped first because GitHub slugs the rendered text, not the source.
    """
    text = re.sub(r"[`*_]", "", heading).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _headings(markdown: str) -> set[str]:
    return {_github_slug(h) for h in _HEADING.findall(markdown)}


def _documentation_files() -> list[Path]:
    """The markdown that links to the install guide: the README and the two
    release-notes templates published with every build.
    """
    return [README, *sorted(RELEASE_NOTES.glob("*.md"))]


@pytest.fixture
def install_guide_text() -> str:
    assert INSTALL_GUIDE.exists(), f"install guide missing: {INSTALL_GUIDE}"
    return INSTALL_GUIDE.read_text()


def test_readme_links_to_the_install_guide():
    """The README must keep a route to the procedure it no longer contains.

    How the regression manifests: the README's install section is a pointer
    only, so losing the link leaves a reader -- on GitHub or on the device home
    page -- with no way to reach any install instructions at all.
    """
    targets = _link_targets(README.read_text())
    assert any(INSTALL_GUIDE_REPO_PATH in target for target in targets), (
        f"README links nowhere to {INSTALL_GUIDE_REPO_PATH}: {targets}"
    )


@pytest.mark.parametrize("path", _documentation_files(), ids=lambda p: p.name)
def test_links_to_the_install_guide_resolve(path, install_guide_text):
    """Every link naming the install guide must reach a real heading.

    How the regression manifests: renaming the file, or renaming a heading
    inside it, leaves the link rendering normally while GitHub answers with a
    404 or drops the reader at the top of the page instead of the section the
    text promised.
    """
    headings = _headings(install_guide_text)
    for target in _link_targets(path.read_text()):
        if INSTALL_GUIDE_REPO_PATH not in target:
            continue
        _, _, fragment = target.partition("#")
        if not fragment:
            continue
        assert fragment in headings, (
            f"{path.name} links to #{fragment}, which is not a heading in "
            f"{INSTALL_GUIDE_REPO_PATH}: {sorted(headings)}"
        )


def test_install_guide_relative_links_resolve(install_guide_text):
    """Relative links in the guide must resolve from its own directory.

    How the regression manifests: the guide moved down one directory level, so
    a path that was correct in the README (``tools/...``) now points at
    ``docs/tools/...`` and 404s -- a break that only shows on click.
    """
    for target in _link_targets(install_guide_text):
        if target.startswith(("http://", "https://", "#")):
            continue
        resolved = (INSTALL_GUIDE.parent / target.partition("#")[0]).resolve()
        assert resolved.exists(), f"install guide links to missing path: {target}"


def test_readme_links_are_absolute():
    """The README must not carry repo-relative links.

    It is bundled into the web app and rendered as the device home page, where
    a relative target resolves against the device's own host. How the
    regression manifests: the link renders and clicks, then lands on the SPA's
    404 rather than the document -- on GitHub the same link works, so the break
    is invisible to whoever wrote it.
    """
    relative = [
        target
        for target in _link_targets(README.read_text())
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]
    assert not relative, f"README links must be absolute URLs, found: {relative}"
