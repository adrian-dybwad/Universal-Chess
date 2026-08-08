"""Tests that release signing is configured the way boards expect.

Boards refuse an unsigned release, so a signing step that cannot run is a release
that cannot ship, and a step that signs with the wrong key ships a release every
board rejects. Both failures land after publication, which is why they are
asserted here.

The CI signing key deliberately has no passphrase. Storing an encrypted key
alongside its passphrase in the same secret store protects against nothing --
whoever can read one can read the other -- while adding a way for the release to
fail. Two consecutive nightlies failed on it: first because gpg parses a
passphrase beginning with a hyphen as an option, then because the stored value
was not the passphrase at all.
"""

from pathlib import Path

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SHIPPED_KEYRING = (
    REPO_ROOT / "packaging" / "deb-root" / "opt" / "universalchess"
    / "keys" / "release-signing.gpg"
)


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


def test_signing_uses_a_key_that_needs_no_passphrase():
    """No signing workflow may reference a passphrase at all.

    Why this test exists: the CI key is deliberately unencrypted, because an
    encrypted key stored beside its passphrase in the same secret store is no
    better protected than an unencrypted one, and the passphrase broke two
    releases in a row. Reintroducing one brings back a failure mode whose log
    output masks the offending value, making it unusually hard to diagnose.

    How a regression manifests: signing fails with "Bad passphrase" or
    "invalid option", after the package has already been built, on every release
    and nightly until someone corrects a repository secret.
    """
    offenders = []
    for workflow in _signing_workflows():
        for number, line in enumerate(workflow.read_text().splitlines(), start=1):
            if "passphrase" in line.lower() and not line.strip().startswith("#"):
                offenders.append(f"{workflow.name}:{number}: {line.strip()}")
    assert not offenders, (
        "the CI signing key is unencrypted by design; a passphrase adds no "
        "protection when it lives in the same secret store as the key, and has "
        f"twice blocked releases: {offenders}"
    )


def test_signing_reports_when_the_key_secret_is_missing():
    """An absent key must fail with a message naming the secret.

    Why this test exists: without the check, gpg fails further down with an
    error about having no secret key. That is accurate but points at the key
    material rather than at the repository setting that is actually missing, in
    a step where every value is masked and so offers no other clue.

    How a regression manifests: a cleared secret stops every release with an
    error that sends whoever investigates in the wrong direction.
    """
    unguarded = [
        workflow.name
        for workflow in _signing_workflows()
        if '-z "${RELEASE_SIGNING_KEY}"' not in workflow.read_text()
    ]
    assert not unguarded, (
        "signing must check the key secret is present and say so plainly rather "
        f"than letting gpg fail on an empty import: {unguarded}"
    )


def test_signing_verifies_the_key_matches_the_keyring_shipped_to_boards():
    """The signing key's fingerprint must be checked against the keyring.

    Why this test exists: boards verify releases against the keyring inside the
    package and trust nothing else. If the CI secret ever holds a different key
    than that keyring carries, signing succeeds, the release publishes, and every
    board refuses the update -- while CI stays green. Nothing else in the system
    compares the two, so the mismatch would surface as boards silently ceasing to
    update.

    How a regression manifests: a published release that no board will install,
    discovered only from user reports, and unfixable by another release because
    the same mismatch rejects that one too.
    """
    unchecked = [
        workflow.name
        for workflow in _signing_workflows()
        if SHIPPED_KEYRING.name not in workflow.read_text()
    ]
    assert not unchecked, (
        "signing must compare the imported key against "
        f"{SHIPPED_KEYRING.name}, the only key boards will accept: {unchecked}"
    )


def test_shipped_keyring_is_a_certificate_stream_not_a_keybox():
    """The keyring must be in the format both verifiers can read.

    Why this test exists: building the file with ``--keyring <file> --import``
    produces a GnuPG keybox, which ``gpgv`` reads and ``sqv`` cannot. Bookworm
    uses gpgv and trixie uses sqv, so a keybox verifies correctly on every board
    used to test it and fails on the newer ones -- the kind of split that gets
    discovered after release. ``gpg --export`` writes the plain certificate
    stream both accept.

    How a regression manifests: trixie boards refuse every update with a
    signature error while bookworm boards update normally, so the releases look
    fine from anywhere the format happens to be supported.
    """
    assert SHIPPED_KEYRING.exists(), (
        f"{SHIPPED_KEYRING} is missing; build.sh refuses to package without it"
    )
    data = SHIPPED_KEYRING.read_bytes()
    assert b"KBXf" not in data[:16], (
        "the keyring is a GnuPG keybox, which sqv cannot parse: trixie boards "
        "would reject every update. Rebuild it with `gpg --export`."
    )
    # First octet of an OpenPGP packet has bit 7 set; --export leads with a
    # public-key packet (tag 6), old or new format.
    assert data and data[0] in (0x98, 0x99, 0xC6), (
        "the keyring does not begin with an OpenPGP public-key packet "
        f"(first byte {data[0]:#04x}); it was not produced by `gpg --export`"
    )
