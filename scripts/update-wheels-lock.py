#!/usr/bin/env python3
"""Regenerate ``src/universalchess/setup/pinned/requirements.txt``.

The lock names every Python distribution the package must carry so the postinst
can install the venv offline, pinned to an exact version and sha256. CI builds
the wheelhouse from it with ``pip wheel --require-hashes``.

The set is the dependency closure of ``requirements.txt`` minus everything listed
in ``system-provided.txt``, which Debian supplies. The subtraction is done by
resolving in a scratch environment that already has the system-provided
distributions installed: pip then prunes their subtrees for us, which is more
reliable than reimplementing marker evaluation to walk the graph by hand.

Run after changing requirements.txt or system-provided.txt::

    ./scripts/update-wheels-lock.py

Requires network access, and must run under Python 3.11 -- the interpreter
bookworm ships, and the oldest the package supports. Markers are evaluated
against the running interpreter, so this is checked at startup rather than left
to whoever invokes it; see ``RESOLUTION_PYTHON``.

Known limitation: resolving under the oldest interpreter catches backports that
bookworm needs and trixie does not, since carrying an unnecessary pure-Python
wheel is harmless. It would not catch a distribution required *only* under
trixie's 3.13. No current requirement has such a marker, and the build
workflow's offline resolution would fail if one appeared.
"""

import argparse
import json
import re
# Used only to run pip with a fixed argv list; never a shell, never a string.
import subprocess  # nosec B404
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# Bookworm, the oldest board OS the package supports, ships Python 3.11. pip
# evaluates environment markers against the interpreter doing the resolving, so
# resolving under a newer one silently drops any distribution gated on an older
# version. The result installs fine on trixie and fails only on bookworm, with
# --no-index leaving no way to recover on the device, so this is enforced rather
# than documented.
RESOLUTION_PYTHON = (3, 11)

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = REPO_ROOT / "src" / "universalchess" / "setup"
REQUIREMENTS = SETUP_DIR / "requirements.txt"
SYSTEM_PROVIDED = SETUP_DIR / "system-provided.txt"
# Named requirements.txt, in its own directory, so GitHub's dependency graph
# parses it: that parsing is what produces vulnerability alerts for these pins,
# and it matches by exact filename rather than by pattern.
WHEELS_LOCK = SETUP_DIR / "pinned" / "requirements.txt"

HEADER = """\
# Python wheels vendored into the .deb. GENERATED -- do not edit by hand.
#
# Regenerate with ./scripts/update-wheels-lock.py after changing requirements.txt
# or system-provided.txt. Editing a single pin here by hand, or letting a bot do
# it, leaves that distribution's transitives at their old versions and hashes,
# which fails a --require-hashes install; only a full re-resolve is coherent.
#
# The name and location are load-bearing. GitHub's dependency graph matches pip
# manifests by the exact filename "requirements.txt" and skips directories named
# like vendored code, so renaming or moving this file switches off Dependabot
# alerts for every pin below without breaking anything visible.
#
# The postinst installs the venv from these with --no-index, so root never runs
# code fetched at install time; the wheels travel inside the signed package
# instead. CI builds the wheelhouse with `pip wheel --require-hashes`, so an
# index that served a different artifact for the same version would fail the
# build rather than reach a board.
#
# This is the closure of requirements.txt minus the distributions in
# system-provided.txt, which Debian supplies through the package's Depends.
"""


def normalize(name):
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_requirements(path):
    names = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[;]", line)[0].strip()
        if name:
            names.append(name)
    return names


def read_system_provided(path):
    names = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line.split(" ", 1)[0].strip())
    return names


def run(argv, **kwargs):
    """Run ``argv`` and raise if it fails.

    Always a list, never a shell. The executable is this interpreter or a pip
    inside a scratch venv this script just created, and the arguments come from
    repo-tracked files.
    """
    return subprocess.run(argv, check=True, **kwargs)  # noqa: S603  # nosec


# PEP 508 distribution name. Validated before going into a URL so a malformed
# entry in system-provided.txt cannot steer the request somewhere else in the
# path; the scheme and host are literals, so only the path is in question.
DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def latest_version(dist):
    """Latest version of ``dist`` on PyPI."""
    if not DISTRIBUTION_NAME.match(dist):
        raise SystemExit(f"not a valid distribution name: {dist!r}")
    url = f"https://pypi.org/pypi/{urllib.parse.quote(dist)}/json"
    # Scheme and host are literals and the name is validated above, so the
    # file:/ and custom-scheme cases B310 warns about cannot arise.
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310  # nosec B310
        return json.load(response)["info"]["version"]


def mark_installed(venv, dist):
    """Record ``dist`` in ``venv`` as installed, without installing it.

    Needed for the Linux-only extensions (spidev, evdev): the board has them from
    Debian, but they cannot be built on a maintainer's macOS machine, and the
    lock must come out the same wherever it is generated. Writing the dist-info
    pip looks for models the board faithfully -- these really are present there.

    The version recorded is PyPI's latest, so a root carrying an upper bound
    would still be evaluated against a realistic number rather than one invented
    to make the constraint pass.
    """
    site = next((venv / "lib").glob("python*/site-packages"))
    version = latest_version(dist)
    info = site / f"{normalize(dist).replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    )
    (info / "INSTALLER").write_text("universal-chess-lock-generator\n")
    return version


def resolve(roots, system_provided, workdir):
    """Return [(name, version, sha256)] for everything not already provided.

    Builds a scratch venv holding the system-provided distributions, then asks
    pip what installing ``roots`` into it would additionally require. pip's
    install report carries the sha256 of each artifact it would fetch, so the
    hashes come from the same resolution rather than a second lookup that could
    disagree with it.
    """
    venv = workdir / "resolve-venv"
    run([sys.executable, "-m", "venv", str(venv)])
    pip = venv / "bin" / "pip"

    run([str(pip), "install", "--quiet", "--upgrade", "pip"])
    # Stand in for what Debian provides on the board, so pip stops descending
    # into their subtrees. Installed for real where possible, because that also
    # brings in their own dependencies (requests -> charset-normalizer, and so
    # on), which the board likewise gets from Debian.
    for dist in system_provided:
        try:
            run([str(pip), "install", "--quiet", dist],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            version = mark_installed(venv, dist)
            print(f":::   {dist} cannot be built here; recorded as {version} "
                  "(Debian supplies it on the board)")

    report = workdir / "report.json"
    run([
        str(pip), "install", "--dry-run", "--quiet",
        "--report", str(report),
        *roots,
    ])

    resolved = []
    for item in json.loads(report.read_text())["install"]:
        metadata = item["metadata"]
        download = item.get("download_info", {})
        digest = download.get("archive_info", {}).get("hashes", {}).get("sha256")
        if not digest:
            raise SystemExit(
                f"no sha256 in pip's report for {metadata['name']}; refusing to "
                "write a lock entry that cannot be verified"
            )
        resolved.append((normalize(metadata["name"]), metadata["version"], digest))
    return sorted(resolved)


def verify(roots, system_provided, workdir):
    """Resolve ``roots`` offline against a wheelhouse built from the lock.

    This is the only check that catches a missing *transitive*. The unit tests
    compare the lock against requirements.txt, so they see a dependency the
    package names directly; a dependency of a dependency is absent from both and
    shows up nowhere until pip fails on a board, mid-install, with no index to
    fall back to.

    Builds the wheelhouse the way the package build does, then asks pip to
    resolve the full requirements with --no-index, exactly as the postinst will.
    """
    wheelhouse = workdir / "wheelhouse"
    wheelhouse.mkdir()
    print("::: building wheelhouse from the lock")
    run([
        sys.executable, "-m", "pip", "wheel",
        "--require-hashes", "--no-deps",
        "--requirement", str(WHEELS_LOCK),
        "--wheel-dir", str(wheelhouse),
    ])

    built = sorted(p.name for p in wheelhouse.glob("*.whl"))
    impure = [name for name in built if not re.search(r"-(py2\.)?py3-none-any\.whl$", name)]
    if impure:
        print(f"::: ERROR: wheelhouse contains non-universal wheels: {impure}",
              file=sys.stderr)
        return 1
    print(f"::: {len(built)} universal wheels built")

    venv = workdir / "verify-venv"
    run([sys.executable, "-m", "venv", str(venv)])
    pip = venv / "bin" / "pip"
    run([str(pip), "install", "--quiet", "--upgrade", "pip"])
    for dist in system_provided:
        try:
            run([str(pip), "install", "--quiet", dist],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            mark_installed(venv, dist)

    print("::: resolving requirements offline, as the postinst does")
    try:
        run([
            str(pip), "install", "--dry-run", "--no-index",
            "--find-links", str(wheelhouse),
            *roots,
        ])
    except subprocess.CalledProcessError:
        print("::: ERROR: requirements cannot be resolved from the wheelhouse. A "
              "transitive dependency is probably missing; regenerate the lock.",
              file=sys.stderr)
        return 1
    print("::: wheelhouse satisfies every requirement offline")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the lock is out of date, without rewriting it",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="build a wheelhouse from the lock and resolve requirements offline",
    )
    args = parser.parse_args()

    if sys.version_info[:2] != RESOLUTION_PYTHON:
        wanted = ".".join(str(part) for part in RESOLUTION_PYTHON)
        running = ".".join(str(part) for part in sys.version_info[:2])
        print(
            f"::: ERROR: this must run under Python {wanted}, not {running}.\n"
            ":::\n"
            f"::: Environment markers resolve against the running interpreter, so\n"
            f"::: a closure resolved under {running} can omit a distribution that a\n"
            f"::: bookworm board needs, and the omission only ever shows up as a\n"
            "::: failed install there.\n"
            ":::\n"
            ":::   gh workflow run refresh-pinned-requirements.yml\n"
            ":::\n"
            f"::: regenerates it under {wanted} and opens a pull request. Locally,\n"
            f"::: run this script with a Python {wanted} interpreter.",
            file=sys.stderr,
        )
        return 1

    provided = read_system_provided(SYSTEM_PROVIDED)
    provided_normalized = {normalize(name) for name in provided}
    roots = [
        name
        for name in read_requirements(REQUIREMENTS)
        if normalize(name) not in provided_normalized
    ]

    if args.verify:
        with tempfile.TemporaryDirectory() as tmp:
            return verify(roots, provided, Path(tmp))

    print(f"::: resolving {len(roots)} root requirements against "
          f"{len(provided)} system-provided distributions")

    with tempfile.TemporaryDirectory() as tmp:
        resolved = resolve(roots, provided, Path(tmp))

    lines = [HEADER]
    for name, version, digest in resolved:
        lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}")
    content = "\n".join(lines) + "\n"

    if args.check:
        current = WHEELS_LOCK.read_text() if WHEELS_LOCK.exists() else ""
        if current != content:
            print(f"::: ERROR: {WHEELS_LOCK} is out of date; run "
                  "./scripts/update-wheels-lock.py", file=sys.stderr)
            return 1
        print(f"::: {WHEELS_LOCK} is up to date")
        return 0

    WHEELS_LOCK.write_text(content)
    print(f"::: wrote {len(resolved)} pinned distributions to {WHEELS_LOCK}")
    for name, version, _ in resolved:
        print(f":::   {name}=={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
