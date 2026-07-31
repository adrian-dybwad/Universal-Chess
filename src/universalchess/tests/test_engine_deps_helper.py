"""Tests for the pinned root helper that installs engine build dependencies.

Engines that need more than the toolchain declared in the package's ``Depends``
-- Zahak needs ``golang``, Arasan needs ``clang`` -- have to obtain those
packages at install time. The previous code ran ``sudo apt-get install -y`` from
the web service, which has no sudoers grant for apt, so on any board without
blanket passwordless sudo the install died with "sudo: a terminal is required to
read the password".

Granting NOPASSWD on apt is not the fix: ``scripts/centaur-armhf-setup`` records
why (apt-get install is a general primitive, so the grant would be equivalent to
unrestricted root), and the postinst follows that rule for all seven other
privileged actions. This helper takes the same shape -- one pinned script behind
one narrow grant -- with a hard-coded package allow-list as the boundary. It
accepts package arguments (the set varies per engine, unlike centaur-armhf-setup)
but refuses anything the engine catalog does not actually declare, so the grant
cannot become a general "install any package as root" primitive.
"""

import re
import subprocess
from pathlib import Path

import universalchess.managers.engine_manager as engine_manager_module
from universalchess.managers.engine_manager import ENGINES, engine_deps_command

REPO_ROOT = Path(engine_manager_module.__file__).resolve().parent.parent.parent.parent
HELPER = REPO_ROOT / "src" / "universalchess" / "scripts" / "uc-engine-deps"
POSTINST = REPO_ROOT / "packaging" / "deb-root" / "DEBIAN" / "postinst"

# Absolute path so the interpreter is not resolved through PATH.
SHELL = "/bin/sh"

# A name that must never be installable through the grant. Chosen to look like a
# plausible package so the test fails loudly if the allow-list check is dropped
# and the argument is passed through to apt verbatim.
DISALLOWED_PACKAGE = "netcat-openbsd"


def _catalog_packages() -> set:
    """Every apt package the engine catalog can ask the helper to install.

    The union of source-build ``dependencies`` and the ``package_name`` of
    system-package engines -- i.e. exactly the set of names any install path can
    pass to the helper.
    """
    names = set()
    for engine in ENGINES.values():
        names.update(engine.dependencies or [])
        if engine.is_system_package and engine.package_name:
            names.add(engine.package_name)
    return names


def _helper_allow_list() -> set:
    """The package names hard-coded in the helper's ``ALLOWED_PACKAGES``."""
    text = HELPER.read_text()
    match = re.search(r'^ALLOWED_PACKAGES="([^"]*)"', text, re.MULTILINE)
    assert match, f"no ALLOWED_PACKAGES assignment in {HELPER}"
    return set(match.group(1).split())


def _run_helper(*args) -> subprocess.CompletedProcess:
    """Invoke the helper with ``args``, capturing output.

    Run through an explicit absolute ``/bin/sh`` rather than executing the file
    directly, so the test does not depend on the executable bit surviving a
    checkout or export (the postinst chmods it on the device).
    """
    return subprocess.run(  # noqa: S603  # nosec B603  # fixed interpreter, repo-local script path, literal test arguments
        [SHELL, str(HELPER), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_helper_ships_with_the_package():
    """The helper must exist in the source tree.

    Why this test exists: the postinst points a sudoers grant at this path. A
    missing file means the grant authorizes nothing and every engine needing
    golang or clang fails to install.

    How a regression manifests: deleting or renaming the script leaves the grant
    dangling, and the install fails with "sudo: a password is required" exactly
    as it did before the helper existed.
    """
    assert HELPER.exists(), f"engine deps helper missing: {HELPER}"
    assert HELPER.read_text().startswith("#!"), "helper must be a script with a shebang"


def test_allow_list_covers_every_package_the_catalog_can_request():
    """Every catalog package must be installable through the helper.

    Why this test exists: the allow-list is hard-coded in shell while the
    requests come from the Python catalog, so the two can drift. If an engine
    declares a dependency the helper refuses, that engine becomes uninstallable
    on every board -- a worse failure than the one this replaces, because it
    fails even where passwordless sudo exists.

    How a regression manifests: adding an engine with a new dependency (or a new
    system package) without extending the helper leaves it out of the allow-list,
    and this assertion fails naming the missing package.
    """
    missing = _catalog_packages() - _helper_allow_list()
    assert not missing, f"helper allow-list does not cover catalog packages: {sorted(missing)}"


def test_allow_list_grants_nothing_the_catalog_does_not_need():
    """The allow-list must not exceed what the catalog actually requests.

    Why this test exists: the allow-list IS the privilege boundary. Every extra
    name widens what the service user can install as root, so an entry that no
    engine needs is unearned privilege -- the same objection that rules out a
    blanket apt grant, only smaller.

    How a regression manifests: dropping an engine from the catalog, or pasting a
    speculative package into the helper, leaves a name that grants root install
    rights for no reason; this assertion fails naming it.
    """
    extra = _helper_allow_list() - _catalog_packages()
    assert not extra, f"helper allow-list grants unrequested packages: {sorted(extra)}"


def test_helper_refuses_a_package_outside_the_allow_list():
    """A package the catalog never declares must be rejected.

    Why this test exists: this is the security boundary that makes the sudoers
    grant narrower than blanket apt. Without it the grant would let the web
    service install any package as root, which is what the project's own
    centaur-armhf-setup comment rules out.

    How a regression manifests: removing the validation loop (or reordering it
    after the apt call) makes the helper install the argument, so this returns 0
    instead of failing and the rejection message is absent.
    """
    result = _run_helper(DISALLOWED_PACKAGE)

    assert result.returncode != 0, (
        f"helper accepted a disallowed package (stdout={result.stdout!r})"
    )
    assert DISALLOWED_PACKAGE in result.stdout + result.stderr, (
        "rejection must name the offending package so the failure is diagnosable"
    )


def test_helper_refuses_option_like_arguments():
    """Arguments that look like apt options must be rejected.

    Why this test exists: the helper forwards its arguments to apt-get. An
    argument such as ``-o`` would let a caller set arbitrary apt configuration as
    root (changing sources, disabling signature checks), turning the narrow grant
    back into a general primitive. The allow-list must reject it as a name, not
    treat it as a flag.

    How a regression manifests: switching validation to "skip anything starting
    with a dash" or forwarding "$@" unchecked makes this exit 0 and pass the
    option through to apt.
    """
    result = _run_helper("-o", "APT::Get::AllowUnauthenticated=true")

    assert result.returncode != 0, "helper accepted an apt option as a package"


def test_helper_refuses_an_empty_request():
    """Calling the helper with no packages must be an error, not a silent no-op.

    Why this test exists: an empty argument list means the caller computed an
    empty dependency set, which is a bug at the call site. Running ``apt-get
    install`` with no operands would exit 0 and report success, so the caller
    would believe dependencies were provisioned when nothing happened.

    How a regression manifests: dropping the arity check makes this return 0, and
    a miscomputed dependency list is reported as a successful install.
    """
    result = _run_helper()

    assert result.returncode != 0, "helper treated an empty request as success"


def test_dependency_install_runs_the_helper_non_interactively():
    """The install command must be ``sudo -n <helper> <packages>``.

    Why this test exists: ``-n`` is the project-wide convention (see
    connectivity/bluetooth.py and test_update_install.py) precisely so a missing
    grant fails immediately instead of blocking on a password prompt with no TTY
    -- the confusing "a terminal is required to read the password" error. Calling
    apt-get directly, or omitting -n, reproduces the original failure.

    How a regression manifests: reverting to ``sudo apt-get install`` means the
    command no longer names the helper, and the sudoers grant (pinned to the
    helper path) does not authorize it.
    """
    command = engine_deps_command(["golang", "git"])

    assert command[:2] == ["sudo", "-n"]
    assert Path(command[2]).name == HELPER.name
    assert command[3:] == ["golang", "git"]


def test_postinst_grants_passwordless_sudo_to_the_helper():
    """The postinst must install a NOPASSWD grant pinned to this helper.

    Why this test exists: without the grant the helper is unreachable and Zahak
    and Arasan stay uninstallable on boards lacking blanket passwordless sudo --
    the failure this whole change exists to fix. Pinned to PRIMARY_USER so it
    follows a non-`pi` install, and to the helper path so it is not a general
    apt grant.

    How a regression manifests: removing the stanza, or widening it to apt-get,
    either breaks the install or reintroduces unrestricted root.
    """
    text = POSTINST.read_text()

    assert "uc-engine-deps" in text, "postinst does not reference the engine deps helper"
    assert "/etc/sudoers.d/universal-chess-engine-deps" in text
    assert "$PRIMARY_USER ALL=(root) NOPASSWD: $ENGINE_DEPS_HELPER" in text


def test_postinst_validates_the_grant_before_it_takes_effect():
    """The drop-in must be syntax-checked with visudo and removed if invalid.

    Why this test exists: a malformed file in /etc/sudoers.d can break sudo for
    the whole system. Every other Universal Chess drop-in validates and removes
    itself on failure; this one must too, so a bad edit degrades to "engine deps
    need a password" rather than "sudo is bricked".

    How a regression manifests: omitting the visudo check lets a malformed grant
    persist, and the next sudo call from any user fails.
    """
    text = POSTINST.read_text()
    marker = "Configuring sudoers for engine dependencies"
    assert marker in text, "engine-deps sudoers stanza missing from postinst"

    start = text.index(marker)
    end = text.index('\necho -e "::: ', start)
    block = text[start:end]

    assert "visudo -cf" in block
    assert "rm -f" in block
