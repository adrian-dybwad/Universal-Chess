"""Tests for the nightly release-notes install instructions.

Root cause these guard
----------------------
Every nightly ships its asset under the identical filename
``universal-chess_2.0.0-nightly_all.deb`` (nightly.yml builds the version as
``${BASE_VERSION}-nightly`` with no date or commit in it), while only the tag
in the download URL changes. The published instructions used a bare
``wget <url>`` followed by ``apt-get install ./universal-chess_..._all.deb``.

``wget`` never overwrites: on the second run it saved the new build to
``...deb.1``, on the third to ``...deb.2``, and apt kept installing the
original file still sitting in the home directory. Users following the release
notes repeatedly reinstalled the *first* nightly they ever downloaded while
believing they were on the latest, and the constant dpkg version
(``2.0.0-nightly``) gave them no way to notice.

The invariant: the instructions must download to an explicit destination inside
a freshly created directory, and must install exactly the path just downloaded.

The tests read the shipped template so the instructions published to users
cannot silently drift back to the colliding form.
"""

import re
from pathlib import Path

import pytest

# Repo layout: .../src/universalchess/tests/<this file>
# -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / ".github" / "release-notes" / "nightly-body.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# The file the update checker reads to identify the installed build. It holds
# the release tag (build.sh createVersionFile), which is the only thing that
# distinguishes one nightly from another.
VERSION_FILE = "/opt/universalchess/VERSION"


@pytest.fixture
def template_text() -> str:
    """The template must ship in the source tree; nightly.yml reads it to build
    the release body, so a missing file breaks every nightly release.
    """
    assert TEMPLATE.exists(), f"nightly release-notes template missing: {TEMPLATE}"
    return TEMPLATE.read_text()


def _bash_blocks(markdown: str) -> list[str]:
    """Every fenced ```bash block in the template, fence lines excluded."""
    return re.findall(r"```bash\n(.*?)```", markdown, re.DOTALL)


@pytest.fixture
def install_block(template_text) -> str:
    """The bash block that downloads and installs a nightly .deb.

    Identified by its download step rather than by position or by
    "apt-get install" (the "Switch to Stable" snippet installs from the apt
    repository and matches that too), so adding further snippets cannot
    silently retarget these assertions at the wrong block.

    The download is matched on ".deb" as well as "wget" because the notes carry
    a second download snippet (the SD card setup tool). Matching a download
    command alone leaves the selection ambiguous, and an ambiguous fixture
    either aborts the suite or silently asserts against the wrong block.
    """
    blocks = [b for b in _bash_blocks(template_text) if "wget" in b and ".deb" in b]
    assert len(blocks) == 1, f"expected exactly one .deb download block, found {len(blocks)}"
    return blocks[0]


def _command_lines(block: str) -> list[str]:
    """The block's executable lines, with comments and blanks dropped.

    The comments deliberately describe the wget-collision bug, so assertions
    about what the instructions *run* must not match the prose explaining it.
    """
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _line_containing(block: str, needle: str) -> str:
    matches = [line for line in _command_lines(block) if needle in line]
    assert len(matches) == 1, f"expected one command containing {needle!r}, got {matches}"
    return matches[0]


def test_download_names_an_explicit_destination(install_block):
    """wget must be given an explicit output path with -O.

    How the regression manifests: a bare ``wget <url>`` derives the filename
    from the URL, and since every nightly uses the same filename, wget refuses
    to overwrite and writes ``...deb.1`` instead -- leaving the stale original
    in place under the name the install step then uses.
    """
    wget = _line_containing(install_block, "wget")
    assert " -O " in wget, f"wget must write to an explicit destination: {wget}"


def test_download_target_is_a_freshly_created_directory(install_block):
    """The destination directory must be created fresh for this download.

    How the regression manifests: downloading into a fixed directory (the home
    directory, or any path reused across nightlies) reintroduces the filename
    collision the -O flag alone does not prevent -- -O overwrites, but a
    partially written or interrupted earlier file under a reused path is still
    a stale-install hazard, and a fresh directory removes the class entirely.
    """
    assert "mktemp -d" in install_block, (
        "the .deb must be downloaded into a freshly created temp directory"
    )


def test_install_uses_exactly_the_downloaded_path(install_block):
    """apt must install the file wget just wrote, not a same-named neighbour.

    How the regression manifests: this is the shipped bug in its purest form --
    the download went to one path and the install read another, so apt happily
    reinstalled a months-old build and reported success. Comparing the two
    tokens catches any future divergence between them.
    """
    wget = _line_containing(install_block, "wget")
    downloaded = wget.split(" -O ", 1)[1].split()[0]

    installed = _line_containing(install_block, "apt-get install").split()[-1]

    assert installed == downloaded, (
        f"install target {installed!r} differs from download target {downloaded!r}"
    )


def test_install_target_is_not_a_bare_working_directory_filename(install_block):
    """The install must not reference a .deb relative to the current directory.

    How the regression manifests: ``./universal-chess_..._all.deb`` resolves to
    whatever copy already sits in the user's cwd, which is precisely the stale
    file wget declined to overwrite.
    """
    apt = _line_containing(install_block, "apt-get install")
    assert "./universal-chess" not in apt, (
        f"install must not target a cwd-relative .deb: {apt}"
    )


def test_install_keeps_reinstall_flag(install_block):
    """--reinstall must survive any rewrite of these instructions.

    How the regression manifests: every nightly carries the dpkg version
    ``2.0.0-nightly``, so without --reinstall apt considers the package already
    at the newest version and installs nothing at all.
    """
    assert "--reinstall" in _line_containing(install_block, "apt-get install")


def test_instructions_show_how_to_confirm_the_installed_build(template_text):
    """The notes must tell users how to read the installed build identity.

    How the regression manifests: because the dpkg version never changes,
    ``dpkg -l`` and apt output look identical for every nightly. Without
    pointing at the VERSION file (which build.sh fills with the release tag),
    a user has no way to detect that a stale build got installed -- the reason
    the original bug went unnoticed for so long.
    """
    assert VERSION_FILE in template_text


def test_every_placeholder_is_substituted_by_the_workflow(template_text):
    """Each __PLACEHOLDER__ in the template must be replaced by nightly.yml.

    How the regression manifests: an unsubstituted placeholder is published
    verbatim, so users copy a literal ``__TAG_NAME__`` into wget and the
    download 404s. The workflow substitutes a fixed set with sed; adding a
    placeholder without extending that set is the failure this catches.
    """
    assert WORKFLOW.exists(), f"nightly workflow missing: {WORKFLOW}"
    workflow_text = WORKFLOW.read_text()

    # The name is captured rather than matched with the delimiters included:
    # the .deb filename embeds a placeholder between literal underscores
    # (universal-chess___NIGHTLY_VERSION___all.deb), and a pattern that ate the
    # surrounding underscores would report a placeholder that does not exist.
    names = set(re.findall(r"__([A-Z]+(?:_[A-Z]+)*)__", template_text))
    placeholders = {f"__{name}__" for name in names}
    assert placeholders, "template lost its placeholders"

    unsubstituted = {
        p for p in placeholders if f's|{p}|' not in workflow_text
    }
    assert not unsubstituted, (
        f"placeholders never substituted by nightly.yml: {sorted(unsubstituted)}"
    )
