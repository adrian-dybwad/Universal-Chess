"""Tests that release signing passes the passphrase to gpg safely.

Boards refuse an unsigned release, so a signing step that cannot run is a release
that cannot ship. It is also the one step that handles the private key material,
which makes how the passphrase reaches gpg worth asserting rather than assuming.

``--passphrase VALUE`` fails outright when the value begins with a hyphen: gpg's
parser reads the next token as an option and exits with ``invalid option``, with
the value masked in the log, which makes the cause hard to see. It additionally
puts the passphrase in the process's argument list, readable by anything that can
list processes on the runner. Feeding it on a pipe avoids both.
"""

import re
from pathlib import Path

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Matches the passphrase supplied as a command-line argument, in either the
# separated or the "=" spelling, while leaving --passphrase-fd and
# --passphrase-file alone.
PASSPHRASE_ARGUMENT = re.compile(r"--passphrase(?!-fd|-file)[ =]")


def _signing_workflows():
    """Workflow files that create a detached signature."""
    return [p for p in sorted(WORKFLOWS_DIR.glob("*.yml")) if "--detach-sign" in p.read_text()]


def test_there_is_at_least_one_signing_workflow():
    """Guards the other tests in this module against passing vacuously.

    Why this test exists: every other assertion here iterates over the signing
    workflows. If the detection string changes or the steps are renamed, that
    iteration becomes empty and the suite reports success while checking nothing.

    How a regression manifests: silently, and in the worst possible place --
    signing would be unverified precisely when someone had just restructured it.
    """
    assert _signing_workflows(), (
        "no workflow contains --detach-sign; either release signing was removed, "
        "or this module no longer finds it and its other tests are vacuous"
    )


def test_signing_never_passes_the_passphrase_as_a_command_line_argument():
    """The passphrase must reach gpg on a file descriptor, not in argv.

    Why this test exists: a passphrase beginning with a hyphen is parsed as an
    option, so gpg exits with ``invalid option`` and the nightly fails at the
    signing step. The value is masked in the log, so the message names no
    recognisable culprit and the failure looks like a broken gpg invocation
    rather than a rejected input. Passing it in argv also exposes it to anything
    that can read the process list on the runner.

    How a regression manifests: every release and nightly fails at signing, with
    a log line that points at gpg rather than at the passphrase, for as long as
    the secret happens to start with a hyphen. A secret that does not start with
    one hides the bug entirely until the key is rotated.
    """
    offenders = []
    for workflow in _signing_workflows():
        for number, line in enumerate(workflow.read_text().splitlines(), start=1):
            if PASSPHRASE_ARGUMENT.search(line):
                offenders.append(f"{workflow.name}:{number}: {line.strip()}")
    assert not offenders, (
        "release signing must feed the passphrase to gpg on a descriptor "
        "(--passphrase-fd 0) rather than as an argument, which breaks on any "
        f"passphrase starting with a hyphen and leaks it into argv: {offenders}"
    )


def test_signing_feeds_the_passphrase_on_a_descriptor():
    """Each signing workflow must actually use the descriptor form.

    Why this test exists: removing the argument form satisfies the test above on
    its own, including by dropping the passphrase entirely. That would leave gpg
    waiting on a pinentry that does not exist in CI, so this asserts the
    replacement is present rather than only that the mistake is absent.

    How a regression manifests: signing hangs or fails with an inability to
    obtain the passphrase, again blocking every release.
    """
    missing = [
        workflow.name
        for workflow in _signing_workflows()
        if "--passphrase-fd" not in workflow.read_text()
    ]
    assert not missing, (
        f"these signing workflows do not supply the passphrase on a descriptor: {missing}"
    )
