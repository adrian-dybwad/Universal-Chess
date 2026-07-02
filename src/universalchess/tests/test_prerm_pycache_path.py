"""Tests for the package prerm pycache-cleanup path.

Root cause these guard
----------------------
The maintainer scripts were inherited from DGTCentaurMods, where the Debian
package name equalled the install dir (/opt/DGTCentaurMods), so the idiom
``DGTCM_PATH="/opt/${PACKAGE}"`` was correct. On the fork the package became
``universal-chess`` while the tree is installed under ``/opt/universalchess``
(no hyphen; see scripts/build.sh OPT_DIR_NAME). prerm kept the package-name idiom
and therefore composed ``/opt/universal-chess`` -- a path that never existed --
so its ``__pycache__``/``.pyc``/``epaper.jpg`` cleanup silently did nothing and
``find`` logged "No such file or directory" on every upgrade/remove (a real user
found that line in their logs).

The tests read the actual shipped prerm so the invariant cannot silently drift
from the script that runs on the board.
"""

from pathlib import Path

import pytest

import universalchess.services.update_service as us

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/prerm.
PRERM = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "prerm"
)

# The canonical on-disk install directory (must match scripts/build.sh
# OPT_DIR_NAME="universalchess"). The Debian package name (universal-chess) is
# deliberately different and must not be used to build this path.
INSTALL_DIR = "/opt/universalchess"
WRONG_HYPHENATED_DIR = "/opt/universal-chess"


@pytest.fixture
def prerm_text() -> str:
    """The prerm must ship in the source tree; a missing file means the package
    has no pre-removal cleanup at all.
    """
    assert PRERM.exists(), f"prerm missing: {PRERM}"
    return PRERM.read_text()


def _executable_lines(text: str) -> str:
    """The script with full-line comments stripped.

    The wrong-path assertion targets what the script *runs*, not what it
    documents: prerm's comment deliberately names the historical
    /opt/universal-chess bug, so a raw substring check would match the
    explanation. Dropping ``#``-comment lines checks the executable body only.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_prerm_targets_the_real_install_dir(prerm_text):
    """prerm must clean the real /opt/universalchess tree.

    How the regression manifests: if the cleanup path reverts to being derived
    from the package name, this literal install dir disappears from the script
    and the pycache prune stops touching the tree that actually exists.
    """
    assert INSTALL_DIR in prerm_text


def test_prerm_never_references_the_hyphenated_nonexistent_path(prerm_text):
    """prerm must not reference /opt/universal-chess.

    How the regression manifests: composing the path from ${PACKAGE}
    ("universal-chess") yields /opt/universal-chess, which does not exist -- the
    exact cause of the "No such file or directory" log line and the silently
    skipped cleanup. Matching the hyphenated literal catches both the literal and
    the resolved-idiom form.
    """
    assert WRONG_HYPHENATED_DIR not in _executable_lines(prerm_text)


def test_prerm_cleans_pycache_and_debug_image(prerm_text):
    """The cleanup must still target bytecode caches and the debug frame image.

    These are runtime-generated and not shipped in the .deb, so dpkg never
    removes them; prerm is the only mechanism. Guards against the fix dropping a
    category while correcting the path.
    """
    for pattern in ("__pycache__", "*.pyc", "*.pyo", "epaper.jpg"):
        assert pattern in prerm_text, f"prerm no longer prunes {pattern}"
