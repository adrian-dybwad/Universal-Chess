"""Pure helpers for locating and validating a Centaur app inside a mounted image.

No I/O side effects beyond reading the already-mounted filesystem, so these are
unit-tested directly against fixture trees.
"""

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Any of the three execute bits marks a file as a program.
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

# The entries that make a directory the Centaur app. ``centaur`` is the
# executable DGT ships; ``engines`` holds the UCI engine it launches; ``fonts``
# holds the e-paper fonts it renders with. All three must be present for the
# app to run, so all three are validated and reported individually when missing.
REQUIRED_APP_ENTRIES = ("centaur", "engines", "fonts")

# Debug/runtime artifacts that must never be carried into the managed copy.
# core dumps and *.log are runtime droppings; the _trace dir and _dbg*.sh
# scripts are this project's own shim-debugging scaffolding left in the SD app.
_CRUFT_PATTERNS = ("core.*", "*.log", "_trace", "_dbg*.sh")


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of checking a candidate app directory.

    ``ok`` is True only when every entry in ``REQUIRED_APP_ENTRIES`` is present
    with the right type; ``missing`` names the absent/wrong-type entries so the
    caller can report exactly what the uploaded image lacks.
    """

    ok: bool
    missing: tuple[str, ...]
    app_dir: str


def _is_executable_file(path: Path) -> bool:
    """Whether ``path`` is a regular file carrying any execute mode bit.

    The check reads the inode's mode bits (``stat``) rather than
    ``os.access(X_OK)`` on purpose: the image is loop-mounted ``noexec`` for
    safety, and on Linux access(X_OK) is denied on a noexec mount even for a
    0755 file. Using os.access here made detection find zero executables and
    report "could not find the Centaur software" on valid images. Mode bits are
    independent of the mount flags and of the caller's uid/gid, which is exactly
    the "is this file marked as a program" question we need to answer.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & _EXEC_BITS)


def detect_app_dir(root) -> Optional[Path]:
    """Find the Centaur app directory anywhere under ``root``.

    The app's location varies by image (``/home/pi/centaur``, ``/opt/centaur``,
    ...), so the tree is searched rather than a fixed path assumed. The anchor is
    an *executable* file named ``centaur`` -- the unique, unambiguous marker; a
    plain data file of the same name is ignored. When several match (unexpected),
    the shallowest wins so a nested backup copy can't shadow the real install.

    Returns the containing directory, or None if no such executable is found.
    """
    root = Path(root)
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if "centaur" in filenames:
            candidate = Path(dirpath) / "centaur"
            if _is_executable_file(candidate):
                matches.append(Path(dirpath))
    if not matches:
        return None
    return min(matches, key=lambda p: len(p.parts))


def validate_app_dir(app_dir) -> ValidationResult:
    """Check a candidate directory holds every required Centaur entry.

    ``centaur`` must be a file; ``engines`` and ``fonts`` must be directories.
    Returns a result naming each missing/wrong-type entry rather than failing on
    the first, so the user sees the complete list of what the image lacks.
    """
    app_dir = Path(app_dir)
    missing = []
    for entry in REQUIRED_APP_ENTRIES:
        target = app_dir / entry
        if entry == "centaur":
            present = target.is_file()
        else:
            present = target.is_dir()
        if not present:
            missing.append(entry)
    return ValidationResult(ok=not missing, missing=tuple(missing), app_dir=str(app_dir))


def ignore_cruft(directory: str, names) -> set:
    """``shutil.copytree`` ignore callback: select debug cruft to skip.

    Matches the ``shutil.copytree(ignore=...)`` contract (called per directory
    with its child names; returns the names to exclude). Centralizes the
    cruft-pattern policy so the copy and any future cleanup agree.
    """
    ignored = set()
    for name in names:
        if any(fnmatch.fnmatch(name, pattern) for pattern in _CRUFT_PATTERNS):
            ignored.add(name)
    return ignored
