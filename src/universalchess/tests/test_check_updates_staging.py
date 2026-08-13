"""Tests for the staging directory scripts/check-updates.sh hands to apt.

Every ``--install`` run printed a permission error partway through:

    N: Download is performed unsandboxed as root as file
    '/var/tmp/tmp.XXXXXXXX/universal-chess_2.0.0-nightly_all.deb' couldn't be
    accessed by user '_apt'. - pkgAcquire::Run (13: Permission denied)

apt drops privileges to the ``_apt`` user for its acquire step even when the
"download" is copying a local file into place. ``mktemp -d`` creates the
directory 0700, owned by the invoking user, so ``_apt`` cannot traverse into it;
apt reports that and redoes the copy as root. The install still succeeds, which
is what makes this worth pinning: it reads as a failure in the middle of an
operation that worked, on every update, so it invites someone to go looking for
a problem that is not there. Making the directory traversable lets apt keep its
own sandbox instead of escalating.

Only the mode is widened, never the ownership: the directory stays owned by the
invoking user, so no other local user can substitute the .deb between the
download and the install. The file itself is a published release artifact, so
readability costs nothing.

These assertions read the script rather than running it. The install path is
reachable only after a GitHub API call parsed with jq, so running it would mean
faking curl, jq, wget, sudo and apt-get -- five shims to observe one directory
mode, where the mechanism is a single line. This mirrors the postinst tests,
which read the shell they cannot practically execute.
"""

import re
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-updates.sh"

# The staging dir must be traversable and readable by another user (apt's _apt)
# for the sandboxed acquire to work: x to enter, r to read the .deb.
REQUIRED_OTHER_BITS = stat.S_IROTH | stat.S_IXOTH


@pytest.fixture
def script_text() -> str:
    """The shipped script; a missing file means no update path at all."""
    assert SCRIPT.exists(), f"check-updates.sh missing: {SCRIPT}"
    return SCRIPT.read_text()


def _line_index(text: str, pattern: str) -> int:
    """Index of the single non-comment line matching ``pattern``.

    Ordering is the whole point of this fix -- a chmod after the install would
    not help -- so the assertions need line positions, and comments mentioning a
    command must not be mistaken for the command.
    """
    matches = [
        index
        for index, line in enumerate(text.splitlines())
        if re.search(pattern, line) and not line.strip().startswith("#")
    ]
    assert len(matches) == 1, f"expected one line matching {pattern!r}, got {matches}"
    return matches[0]


def test_staging_dir_is_made_traversable_before_apt_reads_the_package(script_text):
    """The mktemp dir must be chmodded, and before apt-get is handed the path.

    Why this test exists: this is the fix. 0700 from mktemp is what denies _apt,
    and a chmod that lands after the install would leave the notice in place
    while looking correct on inspection.

    Failure: the chmod is missing, applies to something other than the staging
    dir, or sits after the apt-get install line.
    """
    created = _line_index(script_text, r"tmp_dir=\$\(mktemp -d\)")
    chmod = _line_index(script_text, r"chmod \d+ \"\$tmp_dir\"")
    install = _line_index(script_text, r"apt-get install")

    assert created < chmod < install, (
        "the staging dir must be made traversable after it is created and before "
        f"apt reads the package (mktemp line {created}, chmod {chmod}, install {install})"
    )


def test_the_staging_mode_actually_lets_another_user_read_the_package(script_text):
    """The mode must grant o+rx, and must not grant o+w.

    Why this test exists: a chmod is only the fix if the mode is right. Without
    o+x apt still cannot enter the directory and the notice returns; with o+w any
    local user could replace the .deb between download and install, which would
    turn a cosmetic fix into a way to get arbitrary code installed as root.

    Failure: a mode like 0750 (notice returns) or 0757/0777 (writable by others).
    """
    match = re.search(r"chmod (\d+) \"\$tmp_dir\"", script_text)
    assert match, "no chmod of the staging dir found"
    mode = int(match.group(1), 8)

    assert mode & REQUIRED_OTHER_BITS == REQUIRED_OTHER_BITS, (
        f"mode {match.group(1)} does not let another user read the package; "
        "apt's acquire step needs o+rx on the containing directory"
    )
    assert not mode & stat.S_IWOTH, (
        f"mode {match.group(1)} is world-writable: another local user could "
        "substitute the .deb before apt installs it as root"
    )


def test_the_staging_dir_is_not_handed_to_another_owner(script_text):
    """Ownership of the staging dir must not be changed.

    Why this test exists: the obvious alternative fix -- chowning the directory
    to ``_apt`` -- would hand write access to a system account for a path that a
    root apt process then installs from. Widening the mode keeps the invoking
    user as the only writer.

    Failure: a chown/chgrp of the staging dir appears, e.g. as a "better" fix for
    the same notice.
    """
    offenders = [
        line.strip()
        for line in script_text.splitlines()
        if re.search(r"\b(chown|chgrp)\b", line)
        and "tmp_dir" in line
        and not line.strip().startswith("#")
    ]
    assert offenders == [], f"staging dir ownership must not change: {offenders}"
