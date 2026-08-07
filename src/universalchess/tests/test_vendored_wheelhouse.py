"""Tests that the package carries the Python wheels it installs.

The postinst installs the venv's dependencies as root. Fetching them from PyPI at
that moment means the code root runs is whatever the index serves that day, which
no signature covers -- the package is verified end to end and then pulls
unverified code into itself. Shipping the wheels inside the ``.deb`` moves them
under the signature that already exists, so no second integrity mechanism is
needed, and it removes a network dependency from a step that runs on first boot.

The wheelhouse holds only what Debian does not already supply. Everything in
``setup/system-provided.txt`` arrives through ``Depends`` and is visible via
``--system-site-packages``, so pip treats it as satisfied and never looks for a
wheel. That split is what keeps the wheelhouse pure-Python and therefore valid
for every board: the compiled dependencies are exactly the ones Debian ships, and
vendoring those would mean per-architecture and per-Python-version wheels.
"""

import re
from pathlib import Path

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
REQUIREMENTS = PACKAGE_ROOT / "setup" / "requirements.txt"
WHEELS_LOCK = PACKAGE_ROOT / "setup" / "wheels.lock"
SYSTEM_PROVIDED = PACKAGE_ROOT / "setup" / "system-provided.txt"
BUILD_SH = REPO_ROOT / "scripts" / "build.sh"
DEBIAN_DIR = REPO_ROOT / "packaging" / "deb-root" / "DEBIAN"
POSTINST = DEBIAN_DIR / "postinst"
CONTROL = DEBIAN_DIR / "control"


def _system_provided() -> dict:
    """Map PyPI distribution -> Debian binary package, from system-provided.txt.

    Read from the file rather than duplicated here so the build tooling and these
    tests cannot disagree about which requirements are deliberately absent from
    the wheelhouse.
    """
    mapping = {}
    for raw in SYSTEM_PROVIDED.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        dist, _, package = line.partition(" ")
        mapping[dist.strip()] = package.strip()
    return mapping


def _normalize(name: str) -> str:
    """PEP 503 normalization: names differing only in case or -/_/. are equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_requirements() -> list:
    names = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;]", line)[0].strip()
        if name:
            names.append(name)
    return names


def _lock_entries() -> dict:
    """Map normalized distribution name -> (version, [hashes]) from wheels.lock.

    Parses pip's requirements syntax: backslash-continued lines, ``#`` comments
    and ``--hash=`` options.
    """
    assert WHEELS_LOCK.exists(), (
        f"{WHEELS_LOCK} is missing; the package would ship no wheels to install from"
    )
    text = WHEELS_LOCK.read_text()
    text = re.sub(r"\\\s*\n", " ", text)

    entries = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("--"):
            continue
        tokens = line.split()
        spec = tokens[0]
        hashes = [t.split("=", 1)[1] for t in tokens[1:] if t.startswith("--hash=")]
        name, _, version = spec.partition("==")
        entries[_normalize(name)] = (version, hashes)
    return entries


def _declared_depends() -> set:
    text = CONTROL.read_text()
    match = re.search(r"^Depends:(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no Depends field in {CONTROL}"
    names = set()
    for clause in match.group(1).split(","):
        for alternative in clause.split("|"):
            name = alternative.split("(")[0].strip()
            if name:
                names.add(name)
    return names


def test_system_provided_packages_are_all_declared_in_depends():
    """Every Debian package in system-provided.txt must really be in ``Depends``.

    Why this test exists: the map is what licenses leaving a requirement out of
    the wheelhouse. If it claims Debian supplies something the package does not
    actually depend on, the wheel is omitted and nothing provides the module, so
    the install fails on the board with no index to fall back to.

    How a regression manifests: removing a python3-* entry from Depends while
    leaving it here produces a package whose postinst cannot resolve that
    requirement offline -- a failed install on every board, discovered only after
    release. This fails in CI instead.
    """
    depends = _declared_depends()
    missing = sorted(
        f"{dist} -> {package}"
        for dist, package in _system_provided().items()
        if package not in depends
    )
    assert not missing, (
        "system-provided.txt claims these come from Debian, but the control file "
        f"does not depend on them: {missing}"
    )


def test_wheels_lock_covers_every_requirement_not_supplied_by_debian():
    """Each requirement must be vendored, or explicitly supplied by Debian.

    Why this test exists: the postinst installs with ``--no-index``, so a
    requirement that is neither in the wheelhouse nor provided by a Debian
    package cannot be resolved at all. Adding a dependency without deciding where
    it comes from is the easy mistake, and its cost lands at install time on a
    device rather than here.

    How a regression manifests: pip reports "No matching distribution found" and
    the postinst fails, leaving the package half-configured on every board that
    installs it.

    This checks direct requirements only. A missing *transitive* still resolves
    here and fails on the board, which is why the build workflow also resolves
    requirements offline against the real wheelhouse.
    """
    locked = _lock_entries()
    provided = {_normalize(name) for name in _system_provided()}

    unresolvable = [
        dist
        for dist in _declared_requirements()
        if _normalize(dist) not in locked and _normalize(dist) not in provided
    ]
    assert not unresolvable, (
        "these requirements are neither vendored in wheels.lock nor listed in "
        f"DEBIAN_PROVIDED, so an offline install cannot resolve them: {unresolvable}"
    )


def test_every_wheels_lock_entry_is_pinned_to_a_version_and_a_hash():
    """Lock entries must carry both ``==version`` and at least one sha256 hash.

    Why this test exists: the wheelhouse is built by CI from this file, so it is
    the point where an unverified artifact would enter a package that boards
    trust because it is signed. A version without a hash means CI accepts
    whatever the index returns for that version; a range means it accepts a
    different version entirely.

    How a regression manifests: silently. The build succeeds and the package is
    signed, so the artifact inherits trust it was never checked for -- the exact
    gap vendoring is meant to close.
    """
    unpinned = []
    for name, (version, hashes) in _lock_entries().items():
        if not version:
            unpinned.append(f"{name}: no ==version")
        elif not hashes:
            unpinned.append(f"{name}=={version}: no --hash")
        elif not all(h.startswith("sha256:") for h in hashes):
            unpinned.append(f"{name}=={version}: non-sha256 hash {hashes}")
    assert not unpinned, f"wheels.lock entries are not fully pinned: {unpinned}"


def test_build_refuses_to_produce_a_package_without_a_wheelhouse():
    """``build.sh`` must abort when no wheels were collected.

    Why this test exists: the postinst installs with ``--no-index`` and fails
    when the wheelhouse is absent, so a package built without one is a package
    that cannot install at all. That belongs in the build, next to the equivalent
    guard for the signing keyring, rather than being discovered by whoever
    installs the release.

    How a regression manifests: a wheel-collection step that quietly no-ops (a
    network blip, a renamed path) yields a package that fails during postinst on
    every board, after the release has already been published.
    """
    text = BUILD_SH.read_text()
    assert "requireVendoredWheels" in text, (
        "build.sh must define and call a guard that refuses to build without the "
        "vendored wheelhouse"
    )
    assert re.search(r"requireVendoredWheels\b", text.split("function stage")[1]), (
        "requireVendoredWheels must be invoked during staging, like "
        "requireSigningKeyring"
    )


def test_postinst_installs_only_from_the_vendored_wheelhouse():
    """The postinst must install with ``--no-index`` and no PyPI fallback.

    Why this test exists: the point of vendoring is that root never runs code
    fetched at install time. A fallback branch that reaches PyPI when the
    wheelhouse looks absent restores exactly that exposure, and would do so
    silently on precisely the boards where something is already wrong.

    How a regression manifests: no visible failure -- installs keep working by
    quietly downloading from the index again, so the guarantee is gone while
    every symptom that would reveal it is absent.
    """
    text = POSTINST.read_text()

    assert '--no-index --find-links "$WHEELS_DIR"' in text, (
        "postinst must install requirements from the vendored wheelhouse"
    )

    network_installs = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"pip['\"]?\s+install", line)
        and "--no-index" not in line
        and not line.strip().startswith("#")
    ]
    assert network_installs == [], (
        f"postinst still has pip installs that can reach the network: {network_installs}"
    )
