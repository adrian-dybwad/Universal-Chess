"""Tests that ``setup/requirements.txt`` lists what the board actually needs.

Every entry here is downloaded from PyPI and installed by the postinst, which
runs as root. An entry that nothing imports is therefore not merely untidy: it is
code fetched over the network and executed with root's privileges for no reason,
and each one drags in its own transitive closure. Restating a dependency that
another package already declares has the same cost while adding a second place
the version can be constrained from.

The requirements file is shared by the device venv and the dev/test venv (see the
``numpy`` note in it), so imports anywhere in the package -- tests included --
count as use.
"""

import re
from pathlib import Path

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REQUIREMENTS = PACKAGE_ROOT / "setup" / "requirements.txt"

# PyPI distribution name -> the module name it actually installs. Only the ones
# that differ need an entry.
DIST_TO_MODULE = {
    "pyserial": "serial",
    "python-chess": "chess",
    "python-pam": "pam",
}

# Requirements that are correct to list despite nothing importing them, with the
# reason each is unavoidable. Anything not named here must be imported somewhere.
INDIRECT_REQUIREMENTS = {
    "six": (
        "python-pam 2.0.2 imports six at runtime but declares no dependencies "
        "at all, so pip will not install it on python-pam's behalf"
    ),
}


def _declared_requirements() -> list:
    """Distribution names listed in requirements.txt, in file order."""
    names = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip any version specifier, extras or environment marker.
        name = re.split(r"[<>=!~\[;]", line)[0].strip()
        if name:
            names.append(name)
    return names


def _imported_modules() -> set:
    """Top-level modules imported anywhere in the shipped package."""
    pattern = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
    found = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        found.update(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def test_every_requirement_is_imported_or_documented_as_indirect():
    """Each requirement must be imported, or listed in INDIRECT_REQUIREMENTS.

    Why this test exists: requirements.txt had accumulated three entries the
    application never imports. ``bleak`` was used only by a dev-tools sniffer that
    tells the reader to install it by hand, and it pulls ``dbus-fast``, which has
    no armv6 wheel and so was compiled from source as root on every Pi Zero
    install. ``ndjson`` and ``casttube`` merely restate what ``berserk`` and
    ``pychromecast`` already declare, so pip would install them regardless.

    How a regression manifests: no visible failure at all -- the board installs
    and runs -- which is why this is worth pinning. The cost is silent: extra
    packages fetched from PyPI and executed as root during install, a longer
    install on the slowest hardware, and a larger supply-chain surface than the
    code justifies. The assertion names the unused distribution.
    """
    imported = _imported_modules()

    unused = []
    for dist in _declared_requirements():
        if dist in INDIRECT_REQUIREMENTS:
            continue
        module = DIST_TO_MODULE.get(dist, dist.replace("-", "_"))
        if module not in imported:
            unused.append(f"{dist} (looked for `import {module}`)")

    assert not unused, (
        "requirements.txt lists distributions nothing imports. Remove them, or "
        "add them to INDIRECT_REQUIREMENTS with the reason they cannot be "
        f"dropped: {unused}"
    )


def test_indirect_requirements_are_still_declared():
    """Everything in INDIRECT_REQUIREMENTS must still be in requirements.txt.

    Why this test exists: the entries here are exactly the ones a reader is most
    likely to delete, because nothing imports them and the reason they are needed
    lives in another package's missing metadata rather than in this repository.

    How a regression manifests: dropping ``six`` leaves python-pam importing a
    module that is not installed, so PAM authentication raises ImportError and
    the web UI rejects every login -- on the device only, since a dev machine
    usually has six already.
    """
    declared = set(_declared_requirements())
    missing = sorted(set(INDIRECT_REQUIREMENTS) - declared)
    assert not missing, (
        "requirements.txt is missing indirect requirements that nothing imports "
        f"but which are still needed: "
        f"{ {name: INDIRECT_REQUIREMENTS[name] for name in missing} }"
    )
